"""统一可见性出口：被 supersede 的文档在检索面必须一致地不出现。

2026-11-18 复盘（先说清根因，别再猜）：
旧条 `status=superseded` 却还能排在检索第 3 位。第一反应是「过滤点没覆盖全」，**错了**——
`_personal_rows` 里明明有 `path <> all(%(sup)s)`。真正的原因是**写侧**：显式 `supersedes`
那条分支只写了 Observation 事件，没调 `supersede.add()`，于是 `superseded_ids()` 返回空集，
各处「看起来有过滤」的查询过滤的是空集——漏的是登记，不是查询。

修法分两层：
  1. 写侧：任何「旧条被接替」都必须登记进 supersede.jsonl（`observation.decide` 的
     显式 supersedes 分支已补 `supersede.add`）；
  2. 读侧：本模块是唯一的可见性出口。新增任何列文档的查询，带一个
     `{_visibility.hidden_param()}` 就自动继承，不用在每个 handler 里自己写 if。

口径：**隐藏、不删除**。按 id 直接取全文仍然拿得到（那是有意为之：复盘要看原文）。
"""

from __future__ import annotations

from typing import Any

from . import supersede as _supersede


def hidden_ids() -> list[str]:
    """当前应被隐藏的 doc id（supersede 登记过的旧条）。"""
    return sorted(_supersede.superseded_ids())


def hidden_param(column: str = "path") -> str:
    """拼进 SQL 的过滤片段；没有隐藏项时返回空串（避免 `<> all(空数组)` 的边界歧义）。"""
    return f" and {column} <> all(%(sup)s)" if hidden_ids() else ""


def with_hidden(params: dict[str, Any]) -> dict[str, Any]:
    """给查询参数字典补上 `sup`（配合 hidden_param 使用）。"""
    return {**params, "sup": hidden_ids()}


def is_visible(doc_id: str) -> bool:
    """Python 侧同一口径的判断（用于已取回再过滤的场景）。"""
    return (doc_id or "") not in _supersede.superseded_ids()
