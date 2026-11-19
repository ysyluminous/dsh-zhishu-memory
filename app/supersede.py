"""supersede 机制（零 DDL，2026-11-17）。

为什么需要：`review()` 只把 path 从 pending 挪到正式区，**没有 supersedes 关系**——
一条被「撤回/修正」的旧结论仍然留在检索面里，检索时会和新结论一起返回，AI 读到就混乱。
所以按 `core.jsonl` / `doc_history.jsonl` 的路子做 append-only JSONL：

- `data/audit/supersede.jsonl` 每行 `{superseded_id, by_id, ts, actor, reason}`；
- **只隐藏、不删除**：按 id 直接取仍然拿得到全文；
- 幂等：同一条被 supersede 两次只记一条，第二次返回 `{"ok": true, "skipped": true}`。
"""

from __future__ import annotations

from .config import DATA_DIR

import json
import pathlib
from datetime import datetime, timezone

_PATH = DATA_DIR / "audit" / "supersede.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> list[dict]:
    if not _PATH.exists():
        return []
    out: list[dict] = []
    for line in _PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def superseded_ids() -> set[str]:
    """当前被 supersede 的 doc id 集合（检索时把这些隐藏）。"""
    return {r.get("superseded_id") for r in _load() if r.get("superseded_id")}


def by(doc_id: str) -> str | None:
    """某个 id 是被谁 supersede 的（没有则 None）。"""
    for r in _load():
        if r.get("superseded_id") == doc_id:
            return r.get("by_id")
    return None


def add(superseded_id: str, by_id: str, reason: str = "", actor: str = "user") -> dict:
    """登记一条 supersede；幂等，不删任何数据。"""
    superseded_id = (superseded_id or "").strip()
    by_id = (by_id or "").strip()
    if not superseded_id or not by_id:
        raise ValueError("superseded_id 与 by_id 都不能为空")
    if superseded_id == by_id:
        raise ValueError("superseded_id 与 by_id 不能是同一条")
    if by(superseded_id):
        return {"ok": True, "skipped": True, "superseded_id": superseded_id,
                "by_id": by(superseded_id), "reason": "已登记过，幂等跳过"}
    rec = {"superseded_id": superseded_id, "by_id": by_id, "ts": _now(),
           "actor": actor, "reason": (reason or "").strip()}
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        from . import history as _history
        _history.add(superseded_id, "supersede", actor, new_hash=by_id,
                     reason=rec["reason"] or f"superseded by {by_id}")
    except Exception:
        pass          # 历史链写失败不影响隐藏效果
    return {"ok": True, "skipped": False, **rec}
