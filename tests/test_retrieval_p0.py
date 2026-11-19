"""P0/P1 检索改造的纯逻辑回归：不连库，锁住分型、锚点、片段、工具引导。

运行：python tests/test_retrieval_p0.py
背景：2026-11-19 WorkBuddy 首次接入反馈——搜不到、看不全、分不清、没引导。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import quality  # noqa: E402
from app import service  # noqa: E402


def test_conv_terms_avoid_two_char_scans():
    """2 字词吃不到 trgm 索引（实测 7-11s）；必须生成 4-gram，且含「三家合参」。"""
    terms = service._conv_terms("三家合参")
    assert "三家合参" in terms
    assert all(len(t) >= 3 for t in terms), terms

    long_terms = service._conv_terms("伤寒论三家合参笔记.pdf")
    assert "三家合参" in long_terms, long_terms
    assert all(len(t) >= 3 for t in long_terms), long_terms


def test_snippet_around_keyword():
    text = "前面很多无关内容。" * 20 + "这里提到三家合参的处理。" + "后面还有很多内容。" * 20
    snippet = service._snippet_around(text, "三家合参", 20)
    assert "三家合参" in snippet
    assert len(snippet) < len(text)
    assert snippet.startswith("…") and snippet.endswith("…")


def test_match_info_and_anchor():
    info = service._match_info(["三家合参", "伤寒"], "三家合参笔记", "正文里提到三家合参", "")
    assert "三家合参" in info["title_hits"]
    assert "三家合参" in info["body_hits"]
    assert "伤寒" not in info["matched_terms"]

    anchor = service._anchor("mem.conv", "dsh:session-x", 12, "2026-11-19T10:00:00+08:00")
    assert anchor == "mem.conv:dsh:session-x#seq=12@2026-11-19T10:00:00+08:00"


def test_meta_note_detection():
    assert service._is_meta_note("omnihist 会话检索说明", "cloud/owner/x.md")
    assert not service._is_meta_note("伤寒论三家合参笔记", "cloud/owner/y.md")


def test_help_and_capabilities():
    guide = service.help_text("search")
    assert guide["ok"] is True
    assert "conv_search" in guide["tools"]
    full = service.help_text()
    assert "search" in full["tools"] and "remember" in full["tools"]
    caps = service.capabilities()
    assert "conv_search" in caps["tools"]
    assert "search" in caps["retrieval"]


def test_feedback_score_clamp():
    assert quality._score(None) is None
    assert quality._score("") is None
    assert quality._score(-2) == 0
    assert quality._score(9) == 5
    assert quality._score("4") == 4


def test_semantic_decision_boost():
    profile = service._semantic_profile("改成 cms_book")
    assert profile["decision_intent"] is True
    decision = service._semantic_score("决定改成 cms_book，17:45 定稿", profile, role="assistant")
    code = service._semantic_score("cms_book 表结构：select * from cms_book | ---", profile)
    assert decision > code, (decision, code)


def main() -> None:
    cases = [
        test_conv_terms_avoid_two_char_scans,
        test_snippet_around_keyword,
        test_match_info_and_anchor,
        test_meta_note_detection,
        test_help_and_capabilities,
        test_feedback_score_clamp,
        test_semantic_decision_boost,
    ]
    for case in cases:
        case()
        print(f"PASS {case.__name__}")
    print(f"PASS {len(cases)} cases")


if __name__ == "__main__":
    main()
