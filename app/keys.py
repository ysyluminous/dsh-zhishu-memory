"""Personal API keys for the MCP endpoint: create, list, revoke.

A key can only be minted for a real signed-in account: the ``zhishu_session``
cookie is checked against the web tier's ``/api/auth/verify`` endpoint, the
same gate every page uses. Plain-text keys are returned exactly once and only
the sha256 digest is stored, matching ``tools/make_key.py`` and the lookup in
``service.resolve_user``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.request
from typing import Any

import psycopg

from .config import MEM_DSN

KEY_PREFIX = "zm_"
MAX_KEYS = 20
SESSION_COOKIE = "zhishu_session"
# 部署时通过环境变量覆盖；仓库里只放占位符，不放真实域名。
AUTH_VERIFY_URL = os.getenv(
    "AUTH_VERIFY_URL", "http://127.0.0.1:8080/api/auth/verify"
)
PUBLIC_MCP_URL = os.getenv("PUBLIC_MCP_URL", "http://127.0.0.1:8770/mcp")


def _connect() -> psycopg.Connection:
    return psycopg.connect(MEM_DSN, connect_timeout=8)


def fingerprint(key_hash: str) -> str:
    """Stable non-secret label for a stored key: zm_ + first 8 hex chars."""
    return KEY_PREFIX + key_hash[:8]


def clean_name(name: str | None) -> str:
    value = " ".join((name or "").split())[:40]
    return value or "默认设备"


def list_keys(user_id: str) -> list[dict[str, Any]]:
    with _connect() as c:
        with c.cursor() as cur:
            cur.execute(
                """select key_hash, name, created_at, last_used_at
                     from mem.api_key
                    where user_id = %s
                    order by created_at desc""",
                (user_id,),
            )
            rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "fingerprint": fingerprint(row[0]),
            "name": row[1] or "",
            "created_at": row[2].isoformat() if row[2] else None,
            "last_used_at": row[3].isoformat() if row[3] else None,
        }
        for row in rows
    ]


def create_key(user_id: str, name: str = "", limit: int = MAX_KEYS) -> dict[str, Any]:
    label = clean_name(name)
    with _connect() as c:
        with c.cursor() as cur:
            cur.execute(
                "select count(*) from mem.api_key where user_id = %s", (user_id,)
            )
            if int(cur.fetchone()[0]) >= limit:
                raise ValueError(f"Key 数量到上限（{limit} 个），先吊销不用的再生成")
            key = KEY_PREFIX + secrets.token_urlsafe(24)
            digest = hashlib.sha256(key.encode()).hexdigest()
            cur.execute(
                "insert into mem.app_user (id,name) values (%s,%s) on conflict (id) do nothing",
                (user_id, user_id),
            )
            cur.execute(
                """insert into mem.api_key (key_hash,user_id,name)
                   values (%s,%s,%s) returning created_at""",
                (digest, user_id, label),
            )
            created = cur.fetchone()[0]
        c.commit()
    return {
        "id": digest,
        "key": key,
        "fingerprint": fingerprint(digest),
        "name": label,
        "created_at": created.isoformat() if created else None,
    }


def revoke_key(user_id: str, key_id: str) -> bool:
    target = (key_id or "").strip()
    if len(target) != 64:
        return False
    with _connect() as c:
        with c.cursor() as cur:
            cur.execute(
                "delete from mem.api_key where user_id = %s and key_hash = %s",
                (user_id, target),
            )
            removed = cur.rowcount
        c.commit()
    return removed > 0


def _verify_cookie(cookie: str, path: str, method: str) -> dict[str, Any] | None:
    """POST the cookie to the web tier's verify route. None means "not a session"."""
    body = json.dumps({"path": path, "method": method, "page": False}).encode()
    request = urllib.request.Request(
        AUTH_VERIFY_URL,
        data=body,
        method="POST",
        headers={"content-type": "application/json", "cookie": cookie},
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return None
    except Exception as exc:  # network / timeout / bad JSON
        raise RuntimeError(str(exc)) from exc
    return payload if isinstance(payload, dict) else None


async def session_user(request: Any) -> dict[str, Any]:
    """Resolve the signed-in account behind the request's session cookie.

    Returns ``{"id", "label"}`` on success, otherwise ``{"error", "status"}``.
    The cookie is verified by the web tier, never decoded here.
    """
    cookie = request.headers.get("cookie", "")
    if f"{SESSION_COOKIE}=" not in cookie:
        return {"error": "未登录：请先在主站登录后再访问", "status": 401}
    path = str(getattr(getattr(request, "url", None), "path", "/keys"))
    try:
        payload = await asyncio.to_thread(_verify_cookie, cookie, path, request.method)
    except RuntimeError:
        return {"error": "登录校验服务暂时不可用，稍后再试", "status": 503}
    if not payload or not payload.get("ok"):
        return {"error": "登录已过期，请重新登录", "status": 401}
    user = payload.get("user") or {}
    uid = str(user.get("userId") or "").strip()
    if not uid:
        return {"error": "会话里没有用户标识", "status": 401}
    label = str(user.get("nickName") or user.get("email") or uid)
    return {"id": uid, "label": label}
