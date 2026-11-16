"""growth_add 的库级验收：连 ZHISHU_DSN 真跑，用完把测试账本行删干净。

运行：python memory-mcp/tests/_manual_accept.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app import growth_log as growth  # noqa: E402
from app.config import ZHISHU_DSN  # noqa: E402

USER = "qa-manual-20261114"
START = "2026-11-14T18:30:00+08:00"
checks: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    assert cond, f"FAIL {name} {detail}"
    checks.append(name)


def count_rows() -> int:
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute("select count(*) from growth.time_entry where user_id = %s", (USER,))
            return cur.fetchone()[0]


def cleanup() -> None:
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute("delete from growth.time_entry where user_id = %s", (USER,))
            cur.execute("delete from growth.ledger_identity where login_id = %s", (USER,))
        c.commit()


def main() -> None:
    cleanup()
    try:
        draft = growth.growth_add(USER, kind="purchase", title="客户那顿饭", amount=260,
                                  capital="relation", start=START)
        ok("draft 不落库", draft["mode"] == "draft" and count_rows() == 0, str(draft))
        ok("draft 字段", draft["draft"]["amount"] == 260.0
           and draft["draft"]["capital"] == "relation", str(draft.get("draft")))

        saved = growth.growth_add(USER, kind="purchase", title="客户那顿饭", amount=260,
                                  capital="relation", start=START, confirm=True)
        ok("confirm 落库", saved["mode"] == "saved" and count_rows() == 1, str(saved))
        entry = saved["entry"]
        ok("落库内容", entry["kind"] == "purchase" and entry["amount"] == 260.0
           and (entry["planned_start"] or entry["actual_start"] or "").startswith(
               "2026-11-14T18:30"), str(entry))
        ok("来源标记", entry["source"] == "ai")

        again = growth.growth_add(USER, kind="purchase", title="客户那顿饭", amount=260,
                                  capital="relation", start=START, confirm=True)
        ok("重复确认去重", again.get("deduped") is True and count_rows() == 1, str(again))

        day = growth.growth_day(USER, "2026-11-14")
        ok("按日读回", day["count"] == 1 and day["items"][0]["title"] == "客户那顿饭", str(day))

        bad = {}
        for fields in (
            dict(kind="oops", title="x"),
            dict(kind="task", title=" "),
            dict(kind="purchase", title="饭", amount=-3),
        ):
            try:
                growth.growth_add(USER, confirm=True, **fields)
                bad[str(fields)] = "没有报错"
            except ValueError:
                pass
        ok("非法字段被拒", not bad and count_rows() == 1, str(bad))
    finally:
        cleanup()

    ok("清理干净", count_rows() == 0)
    print(f"PASS {len(checks)} 项 · growth_add 真实链路")


if __name__ == "__main__":
    main()
