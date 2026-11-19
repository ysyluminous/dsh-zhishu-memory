"""规则 / 经验权重层（零 DDL，JSONL）。

2026-11-19 账号主定：AI 在检索、编码、回写过程中要能自己把「铁律」提权、沉淀、触发；
人工只给方向。规则不能埋在和普通文档同一个 pending 堆里。

记录是 append-only 事件，读时按 path 折叠；同一个 path 后写覆盖前写。
字段：path/uid/kind/title/summary/priority/pinned/project/topics/approved/source/anchor/by/ts。
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
from datetime import datetime, timezone
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PATH = pathlib.Path(os.getenv("MEM_RULES_PATH") or (_ROOT / "data" / "audit" / "rules.jsonl"))
_LOCK = threading.Lock()
MAX_RULES = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(rec: dict[str, Any]) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with _PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 权重层写失败不能拖垮 remember/gate


def _fold() -> dict[str, dict[str, Any]]:
    if not _PATH.exists():
        return {}
    state: dict[str, dict[str, Any]] = {}
    try:
        lines = _PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        path = str(rec.get("path") or "")
        if not path:
            continue
        if rec.get("deleted"):
            state.pop(path, None)
            continue
        state[path] = rec
    return state


def _norm_priority(value: Any) -> int:
    try:
        return max(1, min(100, int(value)))
    except (TypeError, ValueError):
        return 50


def _norm_topics(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").replace("，", ",").split(",")
    return [str(t).strip() for t in raw if str(t).strip()][:12]


def set_rule(path: str, *, uid: str, kind: str = "rule", title: str = "",
             summary: str = "", priority: Any = 50, pinned: Any = False,
             project: str = "", topics: Any = None, approved: bool = False,
             source: str = "", anchor: str = "", by: str = "ai") -> dict[str, Any]:
    """写入/覆盖一条规则或提权记录；同一 path 后写生效。"""
    existing = _fold().get(path) or {}
    rec = {
        **existing,
        "ts": _now(),
        "path": path,
        "uid": uid,
        "kind": kind or "rule",
        "title": title or existing.get("title") or "",
        "summary": summary or existing.get("summary") or "",
        "priority": _norm_priority(priority),
        "pinned": bool(pinned),
        "project": (project or "").strip(),
        "topics": _norm_topics(topics if topics is not None else existing.get("topics")),
        "approved": bool(approved),
        "source": source or existing.get("source") or "",
        "anchor": anchor or existing.get("anchor") or "",
        "by": by,
        "deleted": False,
    }
    _append(rec)
    return rec


def get(path: str) -> dict[str, Any] | None:
    return _fold().get(path)


def approve(old_path: str, new_path: str) -> dict[str, Any] | None:
    """review pass：pending 路径换成正式路径，规则层跟着搬家。"""
    rec = get(old_path)
    if not rec:
        return None
    moved = set_rule(
        new_path, uid=rec.get("uid") or "", kind=rec.get("kind") or "rule",
        title=rec.get("title") or "", summary=rec.get("summary") or "",
        priority=rec.get("priority") or 100, pinned=rec.get("pinned", True),
        project=rec.get("project") or "", topics=rec.get("topics") or [],
        approved=True, source=rec.get("source") or "", anchor=rec.get("anchor") or "",
        by="review",
    )
    _append({"ts": _now(), "path": old_path, "deleted": True, "by": "review"})
    return moved


def remove(path: str) -> None:
    _append({"ts": _now(), "path": path, "deleted": True, "by": "review"})


def update(path: str, **fields: Any) -> dict[str, Any] | None:
    rec = get(path)
    if not rec:
        return None
    allowed = {k: v for k, v in fields.items()
               if k in {"priority", "pinned", "project", "topics", "approved",
                        "title", "summary", "source", "anchor"}}
    return set_rule(path, uid=rec.get("uid") or "", kind=rec.get("kind") or "rule",
                    title=rec.get("title") or "", summary=rec.get("summary") or "",
                    priority=allowed.get("priority", rec.get("priority")),
                    pinned=allowed.get("pinned", rec.get("pinned")),
                    project=allowed.get("project", rec.get("project") or ""),
                    topics=allowed.get("topics", rec.get("topics") or []),
                    approved=allowed.get("approved", rec.get("approved")),
                    source=allowed.get("source", rec.get("source") or ""),
                    anchor=allowed.get("anchor", rec.get("anchor") or ""),
                    by="ai")


def active(uid: str = "") -> list[dict[str, Any]]:
    """已批准且未删除的规则/提权记录；按 pinned > priority > 更新时间。"""
    rows = []
    for rec in _fold().values():
        if not rec.get("approved"):
            continue
        if uid and rec.get("uid") != uid:
            continue
        rows.append(rec)
    rows.sort(key=lambda r: (bool(r.get("pinned")), int(r.get("priority") or 0),
                             str(r.get("ts") or "")), reverse=True)
    return rows[:MAX_RULES]


def stats(uid: str = "") -> dict[str, Any]:
    rows = active(uid)
    return {
        "total": len(rows),
        "pinned": sum(1 for r in rows if r.get("pinned")),
        "rules": sum(1 for r in rows if r.get("kind") == "rule"),
        "path": str(_PATH),
    }
