"""一句话记一笔的解析单测：不连数据库，只验证草稿字段。

运行：python memory-mcp/tests/test_growth_log.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.growth_log import _parse  # noqa: E402

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 14, 2, 56, tzinfo=TZ)


def check(text: str, **expected):
    got = _parse(text, NOW)
    for key, value in expected.items():
        actual = got[key]
        if isinstance(actual, datetime):
            actual = actual.strftime("%Y-%m-%d %H:%M")
        assert actual == value, f"{text!r} 的 {key}: 期望 {value!r}，实际 {actual!r}"
    return got


def main() -> None:
    # 1) 有金额、有吃饭字样：规则按任务书记成消费，但这是已知误判风险 → 低置信度
    d = check(
        "昨天下午跟客户吃饭 260 块，算关系资本",
        recognized=True, kind="purchase", title="跟客户吃饭",
        amount=260.0, capital="relation", planned=False,
        confidence="low",
    )
    assert d["start"].strftime("%Y-%m-%d %H:%M") == "2026-11-13 14:00"
    assert d["end"].strftime("%H:%M") == "15:00"
    assert any("吃饭" in r for r in d["review"]), d["review"]

    # 2) 时间完整、无金额：高置信度
    d = check("今天上午9点到10点写方案", recognized=True, kind="task", title="写方案",
              amount=None, capital=None, confidence="high", review=[])
    assert d["start"].strftime("%H:%M") == "09:00"
    assert d["end"].strftime("%H:%M") == "10:00"

    d = check("明天晚上8点健身，算健康资本", recognized=True, kind="habit", title="健身",
              capital="health", planned=True, confidence="high")
    assert d["start"].strftime("%Y-%m-%d %H:%M") == "2026-11-15 20:00"

    # 3) 只有金额、没有任何时间词：时间按“现在”猜，必须提示
    d = check("打车 35 块", recognized=True, kind="purchase", title="打车", amount=35.0,
              confidence="low")
    assert any("没认出时间" in r for r in d["review"]), d["review"]

    d = check("午饭花了三十块，算钱资本", recognized=True, kind="purchase",
              amount=30.0, capital="money", confidence="low")

    d = check("11月13日下午3点半跟张总开会 1.5 小时", recognized=True, kind="task",
              title="跟张总开会", confidence="high")
    assert d["start"].strftime("%Y-%m-%d %H:%M") == "2026-11-13 15:30"
    assert d["end"].strftime("%H:%M") == "17:00"

    check("两百六块打车", recognized=True, kind="purchase", title="打车", amount=260.0,
          confidence="low")

    # 4) 凌晨有时段词就不算歧义；只写 3 点才歧义
    d = check("凌晨2点睡了一觉", recognized=True, kind="sleep", confidence="high")
    assert d["start"].strftime("%H:%M") == "02:00"

    d = check("3点开个短会", recognized=True, kind="task", confidence="low")
    assert any("凌晨还是下午" in r for r in d["review"]), d["review"]

    # 5) 投资类有金额也不算误判
    check("今天下午2点定投基金 500 块", recognized=True, kind="investment",
          amount=500.0, confidence="high")

    # 6) 只有时间没有内容：标题空要提示
    d = check("今天14:00", recognized=True, confidence="low")
    assert any("标题" in r for r in d["review"]), d["review"]

    # 7) 认不出来：confidence 是 none，不给 review
    check("嗯", recognized=False, kind="task", confidence="none", review=[])
    check("写完接口文档", recognized=False, kind="task", confidence="none", review=[])

    print("PASS 13 cases")


if __name__ == "__main__":
    main()
