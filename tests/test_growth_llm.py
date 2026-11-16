"""模型兜底的离线单测：不连网、不连库，替掉 _post_json 模拟端点。

运行：python memory-mcp/tests/test_growth_llm.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import growth_llm  # noqa: E402
from app.growth_log import resolve_draft  # noqa: E402

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 14, 2, 56, tzinfo=TZ)

CALLS: list[dict] = []


def fake_llm(content: str):
    def _post(payload, timeout):
        CALLS.append(payload)
        return {"choices": [{"message": {"content": content}}]}
    return _post


def use_endpoint(handler) -> None:
    growth_llm.LLM_BASE = "http://fake.local/v1"
    growth_llm.LLM_MODEL = "test-model"
    growth_llm._post_json = handler


def main() -> None:
    # 1) 没配端点：一条都不该调用
    growth_llm.LLM_BASE = ""
    growth_llm.LLM_MODEL = ""
    assert growth_llm.available() is False
    assert growth_llm.parse("客户那顿饭 两百六", NOW) is None

    # 2) 有端点、模型正常：中文金额换算 + 字段归一
    use_endpoint(fake_llm(json.dumps({
        "kind": "purchase", "title": "客户那顿饭", "amount": "两百六",
        "capital": "relation", "start": "2026-11-13T18:30:00",
        "end": None, "planned": False,
    }, ensure_ascii=False)))
    got = growth_llm.parse("客户那顿饭 两百六", NOW)
    assert got is not None, "模型输出应被采纳"
    assert got["kind"] == "purchase"
    assert got["title"] == "客户那顿饭"
    assert got["amount"] == 260.0, got["amount"]
    assert got["capital"] == "relation"
    assert got["start"].strftime("%Y-%m-%d %H:%M") == "2026-11-13 18:30"
    assert got["end"].strftime("%H:%M") == "19:30"  # purchase 默认 60 分钟
    assert got["planned"] is False
    assert got["parser"] == "llm"

    # 3) 模型包了代码块也认
    use_endpoint(fake_llm(
        "```json\n" + json.dumps({"kind": "meal", "title": "午饭", "amount": 38,
                                 "capital": None, "start": None, "end": None,
                                 "planned": False}, ensure_ascii=False) + "\n```"
    ))
    got = growth_llm.parse("午饭三十八", NOW)
    assert got is not None and got["amount"] == 38.0 and got["kind"] == "meal"
    assert got["start"] is None

    # 4) 模型胡说（非 JSON / 缺标题 / 越界金额）一律丢弃
    for bad in ("我不知道", json.dumps({"kind": "purchase", "amount": 260}),
                json.dumps({"title": "打车", "amount": -5})):
        use_endpoint(fake_llm(bad))
        assert growth_llm.parse("打车 35 块", NOW) is None, bad

    # 5) 端点报错也不能把异常抛出来
    def boom(payload, timeout):
        raise OSError("connection refused")
    use_endpoint(boom)
    assert growth_llm.parse("打车 35 块", NOW) is None

    # 6) 规则高置信度认得出来的句子不走模型（省钱、省时间）
    CALLS.clear()
    use_endpoint(fake_llm(json.dumps({"title": "不该被用到"})))
    d = resolve_draft("今天上午9点到10点写方案", NOW)
    assert d["parser"] == "rule" and d["confidence"] == "high"
    assert CALLS == [], "高置信度就不该调模型"

    # 7) 规则认不出、模型认得出：走模型
    use_endpoint(fake_llm(json.dumps({
        "kind": "purchase", "title": "客户那顿饭", "amount": 260,
        "capital": "relation", "start": "2026-11-13T18:30:00",
        "end": None, "planned": False,
    }, ensure_ascii=False)))
    d = resolve_draft("客户那顿饭 两百六", NOW)
    assert d["parser"] == "llm" and d["recognized"] is True
    assert d["amount"] == 260.0 and d["capital"] == "relation"

    # 8) 规则认不出、模型也不行：交给待办，不假装识别
    use_endpoint(fake_llm("我不确定"))
    d = resolve_draft("嗯", NOW)
    assert d["recognized"] is False and d["parser"] == "rule"

    # 9) 模型给未来时间却标成已发生：以时间为准改成计划
    use_endpoint(fake_llm(json.dumps({
        "kind": "habit", "title": "健身", "amount": None, "capital": "health",
        "start": "2026-11-15T20:00:00", "end": None, "planned": False,
    }, ensure_ascii=False)))
    got = growth_llm.parse("明晚八点健身", NOW)
    assert got is not None and got["planned"] is True

    # 10) 规则认出但低置信度：交给模型复核，模型的结果被采纳
    CALLS.clear()
    use_endpoint(fake_llm(json.dumps({
        "kind": "meal", "title": "跟客户吃饭", "amount": 260,
        "capital": "relation", "start": "2026-11-13T12:00:00",
        "end": None, "planned": False,
    }, ensure_ascii=False)))
    d = resolve_draft("昨天跟客户吃饭 260 块，算关系资本", NOW)
    assert len(CALLS) == 1, "低置信度应交给模型复核"
    assert d["parser"] == "llm" and d["kind"] == "meal" and d["confidence"] == "high", d
    assert d["amount"] == 260.0 and d["capital"] == "relation"

    # 11) 低置信度但模型回不出合规草稿：保留规则结果，并明确标低置信度和原因
    use_endpoint(fake_llm("我不知道"))
    d = resolve_draft("打车 35 块", NOW)
    assert d["parser"] == "rule" and d["recognized"] is True
    assert d["confidence"] == "low" and d["review"], d
    assert d["kind"] == "purchase" and d["amount"] == 35.0

    print("PASS 11 cases")


if __name__ == "__main__":
    main()
