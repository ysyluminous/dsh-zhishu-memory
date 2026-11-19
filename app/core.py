"""P5 core 常驻记忆（零 DDL）。

机制：`data/audit/core.jsonl` 记事件 `{ts, action: add|remove|hit, doc_id}`——
当前 core 集合 = 每个 doc_id 最后一条 add/remove 决定的；
`last_hit` = 该 doc 最后一条 hit 的时间（`gate()` 注入时写一条），
超过 20 条时淘汰「最久未命中」的那条（追加一条 remove 事件，不改历史）。
"""

from __future__ import annotations

from .config import DATA_DIR

import json
import pathlib
from datetime import datetime, timezone

_PATH = DATA_DIR / "audit" / "core.jsonl"
MAX_CORE = 20
# 字符预算（2026-11-17，对齐上游 core-memory.js 的 maxChars）：
# gate() 的 context 总预算只有 4000（service.py 末尾的 [:4000]），
# 20 条 × 300 字能到 6000 字——core 一旦开闸就能把召回预算挤爆。条数和字符是两道闸，别互相替。
CORE_MAX_CHARS = 1000   # 产品判断（2026-11-17 幕后大佬定）：召回的相关性高于常驻性
# 为什么从 2000 降到 1000：gate 总预算只有 4000，自然召回长度实测 2254–3310；
# core 占 2000 时召回只剩 2000，正常查询都会被截。1000 给 core、3000 给召回，
# 自然流量下基本放得下，只在极端情况才截。core 是「用户点名要一直记着的」，
# 召回是「跟当前问题直接相关的」——后者优先级更高。
CORE_LINE_CHARS = 300   # 单条先截断，再计入预算


def budget(rows: list[dict]) -> list[dict]:
    """按传入顺序取 core 条目，返回真正能进上下文的那些（2026-11-17）。

    三条行为照上游，不自己发明：
      1. 单条先截 CORE_LINE_CHARS，再计入预算；
      2. 累计超 CORE_MAX_CHARS 就 break——后面的**全部不要**，不是跳过超长那条继续取
         （后者会让一条长 core 把后面全挤掉）；
      3. 至少保留 1 条：首条即使自己就超预算也要给。
    调用方负责先按 last_hit 倒序（最新的优先占预算）。
    """
    out: list[dict] = []
    total = 0
    for r in rows:
        text = (r.get("text") or "")[:CORE_LINE_CHARS]
        if out and total + len(text) > CORE_MAX_CHARS:
            break
        out.append({**r, "text": text})
        total += len(text)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> tuple[dict[str, str], dict[str, str]]:
    """返回 (当前 core 集合 {doc_id: added_ts}, 最近命中 {doc_id: hit_ts})。"""
    active: dict[str, str] = {}
    hits: dict[str, str] = {}
    if not _PATH.exists():
        return active, hits
    for line in _PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        did, action = rec.get("doc_id") or "", rec.get("action")
        ts = rec.get("ts") or ""
        if action == "add":
            active[did] = ts
        elif action == "remove":
            active.pop(did, None)
        elif action == "hit":
            hits[did] = ts
    return active, hits


def _append(action: str, doc_id: str) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _now(), "action": action, "doc_id": doc_id}, ensure_ascii=False) + "\n")


def mark(doc_id: str, on: bool = True) -> dict:
    """把一条标成 / 取消 core；超过上限时淘汰最久未命中的。"""
    if not doc_id:
        raise ValueError("doc_id 不能为空")
    active, hits = _load()
    if not on:
        _append("remove", doc_id)
        return {"ok": True, "doc_id": doc_id, "core": False, "count": len(active) - (1 if doc_id in active else 0)}
    if doc_id in active:
        return {"ok": True, "doc_id": doc_id, "core": True, "count": len(active)}
    if len(active) >= MAX_CORE:
        victim = min(active, key=lambda d: hits.get(d, active[d]))
        _append("remove", victim)
        active.pop(victim, None)
    _append("add", doc_id)
    return {"ok": True, "doc_id": doc_id, "core": True, "count": len(active) + 1}


def listing() -> dict:
    active, hits = _load()
    items = [{"doc_id": d, "added": active[d], "last_hit": hits.get(d)} for d in active]
    return {"count": len(items), "max": MAX_CORE, "items": items}


def touch(doc_ids: list[str]) -> None:
    for d in doc_ids:
        _append("hit", d)
