"""变更历史（零 DDL）：append-only JSONL + 哈希链。

为什么用 JSONL 而不是表：今晚不许 alter/create table/create extension；
但「这条结论什么时候、谁、基于什么、有没有被偷偷改过」必须能答——
所以每行带 prev_hash，本行 hash = sha256(prev_hash + 本行正文字段)，篡改任意一行都会断链。

文件：<repo>/data/audit/doc_history.jsonl
"""

from __future__ import annotations

from .config import DATA_DIR

import hashlib
import json
import pathlib
from datetime import datetime, timezone

_PATH = DATA_DIR / "audit" / "doc_history.jsonl"

_FIELDS = ("event", "actor", "doc_id", "new_hash", "new_len", "old_hash", "old_len", "reason", "ts")


def _path() -> pathlib.Path:
    return _PATH


def _canon(rec: dict) -> str:
    body = {k: rec.get(k) for k in _FIELDS}
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _prev_hash(path: pathlib.Path) -> str:
    if not path.exists():
        return ""
    last = ""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                last = line
    try:
        return json.loads(last).get("hash", "")
    except Exception:
        return ""


def add(doc_id: str, event: str, actor: str = "user", old_hash: str = "", new_hash: str = "",
        old_len: int | None = None, new_len: int | None = None, reason: str = "") -> dict:
    """追加一行；event ∈ create/update/approve/reject。"""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = _prev_hash(path)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "doc_id": doc_id,
        "event": event,
        "actor": actor,
        "old_hash": old_hash or "",
        "new_hash": new_hash or "",
        "old_len": old_len,
        "new_len": new_len,
        "reason": reason or "",
        "prev_hash": prev,
    }
    rec["hash"] = hashlib.sha256((prev + _canon(rec)).encode()).hexdigest()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def verify() -> dict:
    """从第一行重算一遍链；有任何不符就返回 first_bad。"""
    path = _path()
    if not path.exists():
        return {"ok": True, "lines": 0, "first_bad": None}
    prev = ""
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        n += 1
        try:
            rec = json.loads(line)
        except Exception as exc:
            return {"ok": False, "lines": n, "first_bad": {"line": n, "error": f"不是合法 JSON: {exc}"}}
        expect = hashlib.sha256((prev + _canon(rec)).encode()).hexdigest()
        if rec.get("prev_hash") != prev or rec.get("hash") != expect:
            return {"ok": False, "lines": n,
                    "first_bad": {"line": n, "doc_id": rec.get("doc_id"),
                                  "expect": expect, "got": rec.get("hash"),
                                  "prev_expect": prev, "prev_got": rec.get("prev_hash")}}
        prev = rec["hash"]
    return {"ok": True, "lines": n, "first_bad": None}


def for_doc(doc_id: str, limit: int = 20) -> dict:
    """读回一条文档的时间线（新的在后）。"""
    path = _path()
    limit = max(1, min(int(limit or 20), 200))
    if not path.exists():
        return {"doc_id": doc_id, "count": 0, "events": []}
    hits = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("doc_id") == doc_id or rec.get("doc_id") == doc_id.replace("/pending/", "/"):
            hits.append({k: rec.get(k) for k in ("ts", "event", "actor", "old_hash", "new_hash",
                                                 "old_len", "new_len", "reason", "hash")})
    return {"doc_id": doc_id, "count": len(hits), "events": hits[-limit:]}
