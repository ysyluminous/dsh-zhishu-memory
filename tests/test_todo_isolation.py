"""待办/MCP 的 user_id 隔离验收：两个身份互相看不到，接口不能越权指定别人。

运行：python memory-mcp/tests/test_todo_isolation.py（连线上 zhishu 库，跑完自清）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app import todo_store  # noqa: E402
from app.config import ZHISHU_DSN  # noqa: E402

A = "qa-iso-a-20261114"
B = "qa-iso-b-20261114"


def cleanup():
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute("delete from growth.time_entry where user_id in (%s, %s)", (A, B))
            cur.execute("delete from growth.ledger_identity where login_id in (%s, %s)", (A, B))
        c.commit()


def main() -> None:
    cleanup()
    try:
        ra = todo_store.todo_add(A, "A 的待办", "只有 A 能看", "2026-11-15T09:00:00+08:00", 1)
        rb = todo_store.todo_add(B, "B 的待办", "只有 B 能看", "2026-11-15T10:00:00+08:00", 2)

        list_a = todo_store.todo_list(A)["todos"]
        list_b = todo_store.todo_list(B)["todos"]
        ids_a = {t["title"] for t in list_a}
        ids_b = {t["title"] for t in list_b}
        assert ids_a == {"A 的待办"}, ids_a
        assert ids_b == {"B 的待办"}, ids_b
        assert ra["id"] != rb["id"]

        # A 拿 B 的 id 去改，必须失败，且 B 的内容不变
        try:
            todo_store.todo_update(A, rb["id"], status="done")
            raise AssertionError("A 改到了 B 的待办，隔离失效")
        except ValueError:
            pass
        assert todo_store.todo_list(B)["todos"][0]["status"] == "pending"

        # 账本表里也是各自的 user_id
        with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("select user_id, count(*) from growth.time_entry where user_id in (%s,%s) group by 1", (A, B))
                rows = dict(cur.fetchall())
        assert rows.get(A) == 1 and rows.get(B) == 1, rows
        print("PASS 5 项：各自可见 / 互不可见 / 越权改被拒 / 数据未被改 / 落库归属正确")
    finally:
        cleanup()
        with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("select count(*) from growth.time_entry where user_id in (%s,%s)", (A, B))
                assert cur.fetchone()[0] == 0
        print("清理完成")


if __name__ == "__main__":
    main()
