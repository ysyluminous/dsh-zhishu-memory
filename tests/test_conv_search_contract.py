"""conv_search 的数据库契约冒烟（只读，连 .env 的 MEM_DSN）。

运行：python tests/test_conv_search_contract.py

与 memory-mcp 的同名测试分工：这里只覆盖 `zhishu-memory` 已回迁的部分——
空关键词、未命中、命中时的 seq/role/anchor/snippet 形状。`conv_get(compact=...)`
那半属于 S3 引擎对齐，本测试不测。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import service  # noqa: E402


def test_empty_keyword_is_rejected():
    try:
        service.conv_search("  ")
    except ValueError:
        return
    raise AssertionError("空关键词应当报 ValueError")


def test_no_match_is_safe():
    result = service.conv_search("zz-no-such-keyword-20261119", limit=1, context=0)
    assert result["count"] == 0, result["count"]
    assert result["matches"] == [], result["matches"]
    assert "hint" in result


def test_hit_shape():
    recent = service.conv_list(since="30d", limit=20).get("conversations") or []
    if not recent:
        print("SKIP 库里最近 30 天没有会话，跳过命中形状检查")
        return
    conv = recent[0]
    got = service.conv_get(conv["id"], limit=3)
    if not got.get("messages"):
        print("SKIP 该会话没有消息，跳过命中形状检查")
        return
    keyword = (got["messages"][0].get("text") or "")[:6].strip()
    if len(keyword) < 2:
        print("SKIP 取不到可检索的关键词，跳过命中形状检查")
        return
    result = service.conv_search(keyword, conv_id=conv["id"], limit=5, context=1)
    assert result["count"] >= 1, f"在会话内查 {keyword!r} 应至少命中 1 条"
    match = result["matches"][0]
    assert isinstance(match["seq"], int)
    assert match["role"] in ("user", "assistant", "system", "tool")
    assert match["anchor"].startswith("mem.conv:")
    assert isinstance(match["matched_terms"], list) and match["matched_terms"]
    assert "context" in match, "context>0 时每条命中都应带上下文"
    assert all(isinstance(item["anchor"], str) for item in match["context"])


def main() -> None:
    cases = [test_empty_keyword_is_rejected, test_no_match_is_safe, test_hit_shape]
    for case in cases:
        case()
        print(f"PASS {case.__name__}")
    print(f"PASS {len(cases)} cases")


if __name__ == "__main__":
    main()
