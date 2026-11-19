"""规则权重层的纯逻辑测试：不连库，只验 append-only 折叠、审批搬家、排序。

运行：python tests/test_rules.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="rules-")
os.environ["MEM_RULES_PATH"] = str(Path(_TMP) / "rules.jsonl")

from app import rules  # noqa: E402


def test_pending_not_active():
    rules.set_rule("cloud/u/pending/a.md", uid="u", title="A", priority=90,
                   pinned=True, approved=False)
    assert rules.active("u") == []
    assert rules.get("cloud/u/pending/a.md")["priority"] == 90


def test_approve_moves_path():
    moved = rules.approve("cloud/u/pending/a.md", "cloud/u/a.md")
    assert moved and moved["approved"] is True
    assert moved["path"] == "cloud/u/a.md"
    assert rules.get("cloud/u/pending/a.md") is None
    rows = rules.active("u")
    assert rows and rows[0]["path"] == "cloud/u/a.md"


def test_priority_pin_sort_and_update():
    rules.set_rule("cloud/u/b.md", uid="u", title="B", priority=60, pinned=False,
                   approved=True)
    rules.set_rule("cloud/u/c.md", uid="u", title="C", priority=100, pinned=True,
                   approved=True)
    rows = rules.active("u")
    assert [r["title"] for r in rows[:2]] == ["C", "A"], [r["title"] for r in rows]
    rules.update("cloud/u/b.md", priority=100, pinned=True)
    rows = rules.active("u")
    assert rows[0]["title"] in ("B", "C")


def main() -> None:
    cases = [test_pending_not_active, test_approve_moves_path,
             test_priority_pin_sort_and_update]
    try:
        for case in cases:
            case()
            print(f"PASS {case.__name__}")
        print(f"PASS {len(cases)} cases")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
