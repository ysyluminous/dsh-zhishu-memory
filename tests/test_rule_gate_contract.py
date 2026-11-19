"""规则层契约：已批准规则在 gate 无条件可见、search 命中时置顶。

运行：python tests/test_rule_gate_contract.py（只读，连 .env 的 MEM_DSN 与 rules.jsonl）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import service  # noqa: E402


def test_rule_list_has_active_rule():
    got = service.rule_list("owner")
    assert got["count"] >= 1
    row = got["rules"][0]
    assert row["approved"] is True
    assert row["priority"] >= 1


def test_gate_injects_rule_even_when_query_unrelated():
    items = service._gate_rule_items("owner", "zz-unrelated-query-20261119")
    assert items, "gate 规则层没有返回任何已批准规则"
    assert items[0]["type"] == "rule"
    assert items[0]["score"] >= 100


def test_search_rule_ranks_first_when_keyword_hits():
    got = service.search("TABLE_REUSE_FIRST", "owner", limit=5, include_conversations=False)
    assert got["rules"], got
    assert got["results"][0]["type"] == "rule"
    assert got["results"][0]["score"] >= 10


def main() -> None:
    cases = [test_rule_list_has_active_rule,
             test_gate_injects_rule_even_when_query_unrelated,
             test_search_rule_ranks_first_when_keyword_hits]
    for case in cases:
        case()
        print(f"PASS {case.__name__}")
    print(f"PASS {len(cases)} cases")


if __name__ == "__main__":
    main()
