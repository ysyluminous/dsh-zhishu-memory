"""P3 有用率反馈 + P4 写入前词法去重（零 DDL，JSONL）。

- 反馈：data/audit/feedback.jsonl，一行一条 {ts, doc_id, useful, note}
- 去重：字符 3-gram Jaccard，对正式区最近 N 条比，>=0.62 判重
"""

from __future__ import annotations

from .config import DATA_DIR

import json
import pathlib
from datetime import datetime, timezone

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FEEDBACK = DATA_DIR / "audit" / "feedback.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _score(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, min(5, int(value)))
    except (TypeError, ValueError):
        return None


def add_feedback(doc_id: str, useful: bool, note: str = "",
                 relevance: int | None = None, completeness: int | None = None,
                 freshness: int | None = None) -> dict:
    _FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now(), "doc_id": doc_id, "useful": bool(useful), "note": note or ""}
    # P2（2026-11-19）：分项 0-5 分；不传就不落字段，旧记录口径不变。
    for key, value in (("relevance", relevance), ("completeness", completeness),
                       ("freshness", freshness)):
        score = _score(value)
        if score is not None:
            rec[key] = score
    with _FEEDBACK.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, **rec}


def feedback_stats(days: int = 7) -> dict:
    """近 N 天有用率 + 分项均分。没有数据时率/均分返回 None（不许装 0%）。"""
    if not _FEEDBACK.exists():
        return {"rate": None, "total": 0, "useful": 0, "top": [], "scores": {}}
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    total = useful = 0
    per_doc: dict[str, dict] = {}
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for line in _FEEDBACK.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        try:
            ts = datetime.fromisoformat(str(rec.get("ts")).replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
        if ts < cutoff:
            continue
        total += 1
        if rec.get("useful"):
            useful += 1
        d = per_doc.setdefault(rec.get("doc_id") or "", {"useful": 0, "total": 0})
        d["total"] += 1
        d["useful"] += 1 if rec.get("useful") else 0
        for key in ("relevance", "completeness", "freshness"):
            value = rec.get(key)
            if isinstance(value, (int, float)):
                sums[key] = sums.get(key, 0.0) + float(value)
                counts[key] = counts.get(key, 0) + 1
    top = sorted(per_doc.items(), key=lambda kv: (-kv[1]["total"], kv[0]))[:5]
    scores = {
        key: (round(sums[key] / counts[key], 2) if counts.get(key) else None)
        for key in ("relevance", "completeness", "freshness")
    }
    return {
        "rate": (round(useful / total, 4) if total else None),
        "total": total,
        "useful": useful,
        "top": [{"doc_id": k, "useful": v["useful"], "total": v["total"]} for k, v in top],
        "scores": scores,
    }


def _grams(text: str, n: int = 3) -> set[str]:
    t = "".join((text or "").split()).lower()
    if len(t) <= n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def jaccard(a: str, b: str, n: int = 3) -> float:
    ga, gb = _grams(a, n), _grams(b, n)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return inter / (len(ga) + len(gb) - inter)
