"""growth_add 的单测：不连数据库，只验证「调用方给标准字段」这条路。

运行：python memory-mcp/tests/test_growth_add.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.growth_log import build_manual, _describe  # noqa: E402

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 14, 9, 30, tzinfo=TZ)


def ok(**fields):
    return build_manual(now=NOW, **fields)


def expect_error(**fields) -> str:
    try:
        build_manual(now=NOW, **fields)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"应该报错却没有：{fields}")


def main() -> None:
    # 1) 客户那顿饭：调用方已经切成标准字段，时间带时区
    d = ok(kind="purchase", title="客户那顿饭", amount=260, capital="关系",
           start="2026-11-13T18:30:00+08:00")
    assert d["kind"] == "purchase" and d["title"] == "客户那顿饭"
    assert d["amount"] == 260.0 and d["capital"] == "relation"
    assert d["start"].strftime("%Y-%m-%d %H:%M") == "2026-11-13 18:30"
    assert d["end"].strftime("%H:%M") == "19:30", d["end"]      # 没给 end 按种类默认时长
    assert d["planned"] is False
    assert _describe(d) == "消费 · 客户那顿饭 · 昨天 18:30–19:30 · 260 元 · 关系资本"

    # 2) 只给时间：按今天算
    d = ok(kind="habit", title="跑步", start="07:10")
    assert d["start"].strftime("%Y-%m-%d %H:%M") == "2026-11-14 07:10"
    assert d["end"].strftime("%H:%M") == "07:55"

    # 3) 只给日期：当天 09:00
    d = ok(kind="review", title="周复盘", start="2026-11-15")
    assert d["start"].strftime("%Y-%m-%d %H:%M") == "2026-11-15 09:00"
    assert d["planned"] is True                                 # 未来 → 计划态

    # 4) 明确状态优先于时间推断
    d = ok(kind="task", title="交周报", start="2026-11-15T10:00", status="done")
    assert d["planned"] is False

    # 5) 参数校验
    assert "kind" in expect_error(kind="oops", title="x")
    assert "title" in expect_error(kind="task", title="   ")
    assert "amount" in expect_error(kind="purchase", title="饭", amount=-1)
    assert "capital" in expect_error(kind="task", title="x", capital="运气")
    assert "end" in expect_error(kind="task", title="x", start="2026-11-14T10:00", end="2026-11-14T09:00")
    assert "时间认不出来" in expect_error(kind="task", title="x", start="下午三点")
    assert "status" in expect_error(kind="task", title="x", status="doing")

    # 6) 中文资本口径也认，标题超长截断
    d = ok(kind="meal", title="店" * 120, capital="健康")
    assert d["capital"] == "health" and len(d["title"]) == 80

    print("PASS 6 groups / growth_add 标准字段")


if __name__ == "__main__":
    main()
