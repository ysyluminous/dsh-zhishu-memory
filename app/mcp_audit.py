"""MCP 服务审计：谁连上来、调了什么、成没成。

只写本地 JSONL，不碰 mem 库。理由：
- 连接/调用失败常常就是库不可用；审计不能依赖库，否则最该看的时候没记录。
- 个人服务的量级按天算，为它加两张表 + 迁移不值当。
- 密钥只落 sha256 前 8 位（zm_xxxxxxxx），原文永不进日志。

控制台「MCP 服务」页读同一份文件；轮转 8MB × 3，超出的旧事件自然淘汰。
环境变量 ``MEM_MCP_AUDIT=0`` 可整体关掉（排查噪音时用）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

from . import service
from .config import BASE, DEFAULT_USER, REQUIRE_KEY

_ENABLED = os.getenv("MEM_MCP_AUDIT", "1") != "0"
AUDIT_DIR = Path(os.getenv("MEM_AUDIT_DIR") or (BASE / "logs"))
AUDIT_FILE = AUDIT_DIR / "mcp-audit.jsonl"
MAX_BYTES = 8 * 1024 * 1024
KEEP = 3
MAX_EVENTS = 40000
CST = timezone(timedelta(hours=8))

_write_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()
MAX_SESSIONS = 500

_users: dict[str, tuple[float, dict[str, Any] | None, bool]] = {}
_users_lock = threading.Lock()


# ── 基础格式 ────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(CST)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


def _short(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _fingerprint(key: str) -> str:
    if not key:
        return ""
    return "zm_" + hashlib.sha256(key.encode()).hexdigest()[:8]


def _key_of(request: Any) -> str:
    try:
        return (request.headers.get("x-api-key") or "").strip()
    except Exception:
        return ""


def _request():
    """当前 HTTP 请求；stdio 或非 HTTP 上下文里返回 None。"""
    try:
        return get_http_request()
    except Exception:
        return None


def _client_of(context: MiddlewareContext[Any]) -> tuple[str, str, str]:
    """initialize 里的 clientInfo / protocolVersion；其他方法没有这三个字段。"""
    try:
        params = context.message.params  # type: ignore[attr-defined]
        info = params.client_info  # type: ignore[attr-defined]
        return (
            _short(getattr(info, "name", ""), 80),
            _short(getattr(info, "version", ""), 40),
            _short(getattr(params, "protocol_version", ""), 20),
        )
    except Exception:
        return "", "", ""


def _session_of(context: MiddlewareContext[Any], request: Any) -> str:
    sid = ""
    try:
        sid = context.fastmcp_context.session_id or ""  # type: ignore[union-attr]
    except Exception:
        pass
    if not sid:
        try:
            sid = request.headers.get("mcp-session-id") or ""
        except Exception:
            pass
    return sid


def _ip_of(request: Any) -> str:
    try:
        forwarded = request.headers.get("x-forwarded-for") or ""
        if forwarded:
            return _short(forwarded.split(",")[0], 60)
        return _short(getattr(request.client, "host", "") or "", 60)
    except Exception:
        return ""


def _ua_of(request: Any) -> str:
    try:
        return _short(request.headers.get("user-agent") or "", 160)
    except Exception:
        return ""


# ── 身份（只解析一次，按 key 缓存；绝不记原文） ──────────────────────────────
async def _resolve_user(key: str) -> tuple[dict[str, Any] | None, bool]:
    """返回 (user, 解析是否成功)。解析失败（库挂/超时）与「key 无效」分开记。"""
    now = time.time()
    with _users_lock:
        hit = _users.get(key)
        if hit and hit[0] > now:
            return hit[1], hit[2]
    try:
        user = await asyncio.to_thread(service.resolve_user, key)
        ok, ttl = True, 120.0
    except Exception:
        user, ok, ttl = None, False, 10.0  # 库抖了，10 秒后再试，别拿一次失败当长期结论
    with _users_lock:
        if len(_users) > 200:
            _users.clear()
        _users[key] = (now + ttl, user, ok)
    return user, ok


async def _identity(request: Any) -> dict[str, Any]:
    key = _key_of(request)
    kf = _fingerprint(key)
    user = None
    if key:
        user, resolved = await _resolve_user(key)
        auth = "key" if user else ("invalid" if resolved else "error")
    elif REQUIRE_KEY:
        auth = "missing"
    else:
        user = {"id": DEFAULT_USER, "name": DEFAULT_USER}
        auth = "default"
    return {
        "u": _short((user or {}).get("id", ""), 60),
        "un": _short((user or {}).get("name", ""), 60),
        "kf": kf,
        "auth": auth,
        "ip": _ip_of(request),
        "ua": _ua_of(request),
    }


def _session_meta(sid: str) -> dict[str, Any] | None:
    with _sessions_lock:
        meta = _sessions.get(sid)
        return dict(meta) if meta else None


def _remember_session(sid: str, meta: dict[str, Any]) -> None:
    if not sid:
        return
    with _sessions_lock:
        if len(_sessions) >= MAX_SESSIONS and sid not in _sessions:
            _sessions.pop(next(iter(_sessions)), None)
        _sessions[sid] = dict(meta)


# ── 写盘 / 轮转 ─────────────────────────────────────────────────────────────
def _rotate_locked() -> None:
    try:
        size = AUDIT_FILE.stat().st_size
    except FileNotFoundError:
        return
    if size < MAX_BYTES:
        return
    for i in range(KEEP - 1, 0, -1):
        src = AUDIT_DIR / f"mcp-audit.{i}.jsonl"
        dst = AUDIT_DIR / f"mcp-audit.{i + 1}.jsonl"
        if src.exists():
            os.replace(src, dst)
    os.replace(AUDIT_FILE, AUDIT_DIR / "mcp-audit.1.jsonl")


def record(event: dict[str, Any]) -> None:
    """追加一条审计；任何异常都吞掉，审计不能影响 MCP 调用。"""
    if not _ENABLED:
        return
    now = _now()
    event.setdefault("t", _ts(now))
    event.setdefault("ms", int(now.timestamp() * 1000))
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
    try:
        with _write_lock:
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            _rotate_locked()
            with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        return


def _args_summary(args: Any) -> str:
    if not isinstance(args, dict):
        return _short(args, 220)
    parts: list[str] = []
    for key, value in args.items():
        low = str(key).lower()
        if any(mark in low for mark in ("token", "secret", "password", "authorization", "api_key")):
            parts.append(f"{key}=***")
            continue
        if isinstance(value, (dict, list)):
            text = _short(json.dumps(value, ensure_ascii=False, default=str), 80)
        else:
            text = _short(value, 120)
        parts.append(f"{key}={text}")
        if sum(len(p) for p in parts) > 220:
            break
    return _short(" ".join(parts), 240)


def _verdict(result: Any) -> tuple[bool, str]:
    """工具把错误当正常结果返回（如 {"error": ...}），这里替客户端判一次。"""
    if getattr(result, "is_error", False):
        return False, "is_error"
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if structured.get("ok") is False:
            return False, _short(structured.get("error") or "ok=false")
        if structured.get("error"):
            return False, _short(structured.get("error"))
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str) or not text.lstrip().startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict) and (payload.get("error") or payload.get("ok") is False):
            return False, _short(payload.get("error") or "ok=false")
    return True, ""


# ── 中间件 ──────────────────────────────────────────────────────────────────
class McpAuditMiddleware(Middleware):
    """挂在 FastMCP 上，记录 initialize / tools-list / tools-call。

    事件字段刻意短：t 时间、k 类型、sid 会话、c/cv 客户端、u/un 用户、
    kf 密钥指纹、auth 鉴权方式、ip/ua、tool/args、ok/e、d 耗时。
    """

    async def on_initialize(self, context: MiddlewareContext[Any], call_next):
        started = time.perf_counter()
        request = _request()
        sid = _session_of(context, request)
        client, version, protocol = _client_of(context)
        meta = await _identity(request)
        meta.update({"c": client, "cv": version, "pv": protocol})
        try:
            result = await call_next(context)
        except Exception as exc:
            record({"k": "session", "sid": sid, **meta, "ok": False,
                    "e": _short(exc), "d": _ms(started)})
            raise
        _remember_session(sid, meta)
        record({"k": "session", "sid": sid, **meta, "ok": True, "d": _ms(started)})
        return result

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next):
        started = time.perf_counter()
        request = _request()
        sid = _session_of(context, request)
        meta = _session_meta(sid) or await _identity(request)
        tool = _short(getattr(context.message, "name", ""), 80)
        args = _args_summary(getattr(context.message, "arguments", None))
        try:
            result = await call_next(context)
        except Exception as exc:
            record({"k": "tool", "sid": sid, **meta, "tool": tool, "args": args,
                    "ok": False, "e": _short(exc), "d": _ms(started)})
            raise
        ok, error = _verdict(result)
        event = {"k": "tool", "sid": sid, **meta, "tool": tool, "args": args,
                 "ok": ok, "d": _ms(started)}
        if error:
            event["e"] = error
        record(event)
        return result

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next):
        started = time.perf_counter()
        request = _request()
        sid = _session_of(context, request)
        meta = _session_meta(sid) or await _identity(request)
        try:
            result = await call_next(context)
        except Exception as exc:
            record({"k": "list", "sid": sid, **meta, "ok": False,
                    "e": _short(exc), "d": _ms(started)})
            raise
        record({"k": "list", "sid": sid, **meta, "tools": len(result),
                "ok": True, "d": _ms(started)})
        return result


# ── 读盘（控制台用） ────────────────────────────────────────────────────────
def _audit_paths() -> list[Path]:
    paths = [AUDIT_DIR / f"mcp-audit.{i}.jsonl" for i in range(KEEP, 0, -1)]
    paths.append(AUDIT_FILE)
    return [p for p in paths if p.exists()]


def audit_info() -> dict[str, Any]:
    files = []
    total = 0
    for path in _audit_paths():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        files.append({"name": path.name, "bytes": size})
    return {
        "enabled": _ENABLED,
        "path": str(AUDIT_FILE),
        "bytes": total,
        "retention": f"{MAX_BYTES // (1024 * 1024)}MB × {KEEP}",
        "files": files,
    }


def load_events(limit: int = MAX_EVENTS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按时间顺序返回最近事件；超过 limit 丢最旧的。"""
    limit = max(1, min(int(limit), MAX_EVENTS))
    kept: deque[str] = deque(maxlen=limit)
    scanned = 0
    for path in _audit_paths():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    scanned += 1
                    kept.append(line)
        except OSError:
            continue
    events: list[dict[str, Any]] = []
    for line in kept:
        try:
            event = json.loads(line)
        except Exception:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, {"scanned": scanned, "capped": scanned > limit, "kept": len(events)}
