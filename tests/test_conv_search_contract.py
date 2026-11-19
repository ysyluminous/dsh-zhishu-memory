"""conv_search / conv_get 的数据库契约冒烟（只读，连 .env 的 MEM_DSN）。

运行：python tests/test_conv_search_contract.py
它与 test_retrieval_p0.py 的分工：纯函数回归不连库；这里确认 SQL 路径、
空关键词、compact 截断和 anchor 真的能跑通。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import service  # noqa: E402


def test_conv_search_no_match_is_safe():
    result = service.conv_search("zz-no-such-keyword-20261119", user_id="owner",
                                 limit=1, context=0)
    assert result["count"] == 0
    assert result["matches"] == []
    assert "hint" in result


def test_conv_get_compact_contract():
    recent = service.conv_list(since="30d", limit=1).get("conversations") or []
    if not recent:
        return
    got = service.conv_get(recent[0]["id"], limit=2, compact=True)
    assert got["compact"] is True
    assert got["last_synced_at"]
    assert got["sync_status"] in ("live", "reconciled")
    assert got["reconcile"]["status"] == got["sync_status"]
    assert "message_count" in got
    if got["messages"]:
        first = got["messages"][0]
        assert len(first["text"]) <= 320
        assert first["anchor"].startswith("mem.conv:")
        assert first["chars"] >= len(first["text"])


def test_conv_search_output_budget():
    got = service.conv_search("改成 cms_book", user_id="owner", limit=10,
                              context=1, max_chars=200)
    assert got["output_chars"] <= got["max_total_chars"]
    assert got["max_chars"] == 200
    for match in got["matches"]:
        assert len(match["snippet"]) <= 200
        assert len(match.get("context") or []) <= 3
        for item in match.get("context") or []:
            assert len(item["text"]) <= 200
    for match in got["matches"]:
        assert 0.0 <= match["literal_score"] <= 1.0
        assert 0.0 <= match["semantic_score"] <= 1.0
        assert 0.0 <= match["score"] <= 1.0


def test_doc_get_upgrade_doc():
    rules = service.rule_list("owner").get("rules") or []
    if not rules:
        return
    got = service.doc_get("owner", rules[0]["path"], max_chars=500)
    assert got["ok"] is True
    assert got["body"]
    assert got["truncated"] is True
    assert got["anchor"].startswith("mem.doc:")


def main() -> None:
    cases = [test_conv_search_no_match_is_safe, test_conv_get_compact_contract,
             test_conv_search_output_budget, test_doc_get_upgrade_doc]
    for case in cases:
        case()
        print(f"PASS {case.__name__}")
    print(f"PASS {len(cases)} cases")


if __name__ == "__main__":
    main()
