"""search 权重护栏的回归用例（不连库）。

2026-11-16 事故：英文/编号类查询（如 `zz-probe-20261116-2227`）的 gram 在 mem.doc 里 df=0，
IDF 权重全是 1e-6 → 权重和 3e-6 → 拼进 SQL 时被格式化成 `0.0000`，PG 抛 `division by zero`，
整条 search 直接返错。本用例锁住三件事：df 的坏值折成 1、权重和永不为 0、6 位小数格式不会归零。

运行：python memory-mcp/tests/test_search_weights.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.service import _safe_df, _weight_sum  # noqa: E402


def test_df_bad_values_become_one():
    """df 为 0 / 负数 / NaN / inf / 非数字 → 一律 1.0。"""
    for bad in (0, 0.0, -3, -1e-9, float("nan"), float("inf"), float("-inf"), None, "abc", ""):
        assert _safe_df(bad) == 1.0, f"df={bad!r} 应折成 1.0，实际 {_safe_df(bad)}"
    assert _safe_df(7) == 7.0
    assert _safe_df("12") == 12.0
    assert _safe_df("12.5") == 12.5


def test_weight_sum_never_zero():
    """权重和是 SQL 里的除数：全 1e-6、空列表、全坏值都必须有下限。"""
    assert _weight_sum([1e-6, 1e-6, 1e-6]) >= 1e-6
    assert _weight_sum([]) == 1e-6
    assert _weight_sum([float("nan"), 0.0, -1.0]) == 1e-6
    assert _weight_sum([0.5, 0.25]) == 0.75
    # 坏值不能把好值带崩
    assert _weight_sum([0.5, float("nan"), 0.25]) == 0.75


def test_formatted_divisor_is_not_zero():
    """真正上 SQL 的是格式化后的浮点数：6 位小数下 1e-6 不能变成 0.000000。"""
    assert f"{_weight_sum([1e-6, 1e-6, 1e-6]):.6f}" != "0.000000"
    assert f"{_weight_sum([]):.6f}" != "0.000000"


if __name__ == "__main__":
    test_df_bad_values_become_one()
    test_weight_sum_never_zero()
    test_formatted_divisor_is_not_zero()
    print("search 权重护栏 3 组用例全部通过")
