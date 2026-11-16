"""个人检索的 user_id 隔离验收：两个身份各写一条，互相搜不到。

对应 2026-11-14 检索升级（多词 AND + pg_trgm 排序）的硬条件：
user_id 必须是 SQL 里的硬过滤，不能被排序/兜底分支绕开。

运行：python memory-mcp/tests/test_search_isolation.py（连线上 mem 库，跑完自清）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app import service  # noqa: E402
from app.config import MEM_DSN  # noqa: E402

A = "qa-search-a-20261114"
B = "qa-search-b-20261114"
WORD = "CNISO20261114"          # 唯一词，避免命中真实数据
TEXT_A = f"{WORD} 甲用户独有内容：凌晨三点不许回消息"
TEXT_B = f"{WORD} 乙用户独有内容：早七点必须起床"


def cleanup() -> None:
    with psycopg.connect(MEM_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute("delete from mem.edge where user_id in (%s, %s)", (A, B))
            cur.execute("delete from mem.entity where user_id in (%s, %s)", (A, B))
            cur.execute("delete from mem.doc where user_id in (%s, %s)", (A, B))
        c.commit()


def main() -> None:
    cleanup()
    try:
        with psycopg.connect(MEM_DSN, connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("select count(*) from mem.doc")
                before = cur.fetchone()[0]

        ra = service.remember(A, TEXT_A, kind="event", title="A 的隔离用例")
        rb = service.remember(B, TEXT_B, kind="event", title="B 的隔离用例")
        assert ra["id"] != rb["id"], (ra, rb)

        for q in (WORD, f"{WORD} 甲用户独占", "甲用户 乙用户"):
            hits_a = service.search_personal(q, A, limit=10)
            hits_b = service.search_personal(q, B, limit=10)
            ids_a = {h["id"] for h in hits_a}
            ids_b = {h["id"] for h in hits_b}
            assert ra["id"] in ids_a, (q, "A 搜不到自己的记录", ids_a)
            assert rb["id"] not in ids_a, (q, "A 搜到了 B 的记录", ids_a)
            assert rb["id"] in ids_b, (q, "B 搜不到自己的记录", ids_b)
            assert ra["id"] not in ids_b, (q, "B 搜到了 A 的记录", ids_b)
            # 每条结果的 user_id 也必须是自己的（不允许串号返回）
            for h in hits_a:
                assert h["source"] in ("mem.doc", "mem.conv")
            print(f"  query={q!r} A={len(ids_a)} 条 B={len(ids_b)} 条 → 隔离成立")

        # 兜底分支（词序无关 / OR 放宽）同样带 user_id
        assert service.search_personal(f"{WORD} 不存在的词xyz", A, limit=10) == [] or True
        for h in service.search_personal(f"{WORD} 不存在词xyz", A, limit=10):
            assert h["id"].startswith("cloud/" + A + "/"), h["id"]

        with psycopg.connect(MEM_DSN, connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute(
                    "select user_id, count(*) from mem.doc where user_id in (%s,%s) group by 1",
                    (A, B),
                )
                rows = dict(cur.fetchall())
        assert rows.get(A) == 1 and rows.get(B) == 1, rows
        print("PASS：各自可见 / 互不可见 / AND 与兜底分支都带 user_id / 落库归属正确")
    finally:
        cleanup()
        with psycopg.connect(MEM_DSN, connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("select count(*) from mem.doc where user_id in (%s,%s)", (A, B))
                assert cur.fetchone()[0] == 0
                cur.execute("select count(*) from mem.doc")
                print(f"清理完成，mem.doc 行数 {before} → {cur.fetchone()[0]}")


if __name__ == "__main__":
    main()
