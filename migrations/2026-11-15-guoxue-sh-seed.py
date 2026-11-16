#!/usr/bin/env python3
"""伤寒论·条文注解 种子导入（幂等，重复跑不重复插）。

数据源：D:\\code\\yunxiao\\temp\\shanghanlun\\records.json（13,112 条 + meta）
库：memory-mcp/.env 里的 ZHISHU_DSN（zhishu 库，guoxue schema）
用法：python memory-mcp/migrations/2026-11-15-guoxue-sh-seed.py [--dsn ...]

章节映射规则（按条文号前缀切段，边界已用原文逐条核对：
  1 太阳之为病… / 31 太阳病项背强几几 / 128 病有结胸 / 179 病有太阳阳明 /
  263 少阳之为病 / 273 太阴之为病 / 281 少阴之为病 / 326 厥阴之为病 /
  382 病有霍乱者何 / 392 伤寒阴阳易之为病）：
    1-30    taiyang-shang   辨太阳病脉证并治（上）
    31-127  taiyang-zhong   辨太阳病脉证并治（中）
    128-178 taiyang-xia     辨太阳病脉证并治（下）
    179-262 yangming        辨阳明病脉证并治
    263-272 shaoyang        辨少阳病脉证并治
    273-280 taiyin          辨太阴病脉证并治
    281-325 shaoyin         辨少阴病脉证并治
    326-381 jueyin          辨厥阴病脉证并治
    382-391 huoluan         辨霍乱病脉证并治
    392-398 yinyang-yi      辨阴阳易差后劳复病脉证并治
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import os  # noqa: E402

DSN = os.getenv("ZHISHU_DSN", "")
RECORDS = Path(r"D:\code\yunxiao\temp\shanghanlun\records.json")

CHAPTERS = [
    ("taiyang-shang", "辨太阳病脉证并治（上）", 1, 1, 30),
    ("taiyang-zhong", "辨太阳病脉证并治（中）", 2, 31, 127),
    ("taiyang-xia", "辨太阳病脉证并治（下）", 3, 128, 178),
    ("yangming", "辨阳明病脉证并治", 4, 179, 262),
    ("shaoyang", "辨少阳病脉证并治", 5, 263, 272),
    ("taiyin", "辨太阴病脉证并治", 6, 273, 280),
    ("shaoyin", "辨少阴病脉证并治", 7, 281, 325),
    ("jueyin", "辨厥阴病脉证并治", 8, 326, 381),
    ("huoluan", "辨霍乱病脉证并治", 9, 382, 391),
    ("yinyang-yi", "辨阴阳易差后劳复病脉证并治", 10, 392, 398),
]


def chapter_of(seq: int) -> str:
    for cid, _title, _order, lo, hi in CHAPTERS:
        if lo <= seq <= hi:
            return cid
    raise ValueError(f"条文号 {seq} 落在 10 章之外")


def main() -> None:
    payload = json.loads(RECORDS.read_text(encoding="utf-8"))
    recs = payload["records"]

    clauses, annotations = [], []
    for r in recs:
        no, kind = r["no"], r["kind"]
        if kind == "原文":
            seq = int(no.split(".")[0])
            clauses.append((
                no, chapter_of(seq), seq,
                (r.get("kangping") or "").strip(),
                (r.get("song") or "").strip(),
            ))
            continue
        # 非原文：注解 / 词解 / 金匮讲座
        if no.startswith("399"):
            clause_no, akind = None, "金匮"
        else:
            seq = int(no.split(".")[0])
            clause_no = f"{seq}.001"
            akind = "词解" if kind == "词解" else "注解"
        annotations.append((
            clause_no, kind, (r.get("book") or "").strip(), akind,
            (r.get("ver") or "").strip(), (r.get("kangping") or "").strip(),
            (r.get("song") or "").strip(),
        ))

    if len(clauses) != 398:
        raise SystemExit(f"条文数异常：{len(clauses)}，应为 398")
    if len(annotations) != 13112 - 398:
        raise SystemExit(f"注解数异常：{len(annotations)}，应为 12714")

    with psycopg.connect(DSN, connect_timeout=15) as c:
        with c.cursor() as cur:
            cur.executemany(
                """insert into guoxue.sh_chapter (id, title, order_idx)
                   values (%s,%s,%s)
                   on conflict (id) do update
                     set title = excluded.title, order_idx = excluded.order_idx""",
                [(cid, title, order) for cid, title, order, _lo, _hi in CHAPTERS],
            )
            cur.executemany(
                """insert into guoxue.sh_clause (no, chapter, order_idx, kangping, song)
                   values (%s,%s,%s,%s,%s)
                   on conflict (no) do update
                     set chapter = excluded.chapter, order_idx = excluded.order_idx,
                         kangping = excluded.kangping, song = excluded.song""",
                clauses,
            )
            cur.executemany(
                """insert into guoxue.sh_annotation (clause_no, author, book, kind, ver, body, song)
                   values (%s,%s,%s,%s,%s,%s,%s)
                   on conflict ((coalesce(clause_no, '')), author, book, ver, md5(body)) do update
                     set kind = excluded.kind, book = excluded.book, song = excluded.song""",
                annotations,
            )
        c.commit()

        with c.cursor() as cur:
            cur.execute("select count(*) from guoxue.sh_clause")
            n_clause = cur.fetchone()[0]
            cur.execute("select count(*) from guoxue.sh_annotation")
            n_ann = cur.fetchone()[0]
            cur.execute("select count(*) from guoxue.sh_annotation where clause_no is null")
            n_null = cur.fetchone()[0]
            cur.execute("select count(*) from guoxue.sh_chapter")
            n_chap = cur.fetchone()[0]
            cur.execute("select count(*) from guoxue.sh_clause where song <> ''")
            n_song_clause = cur.fetchone()[0]
            cur.execute("select count(*) from guoxue.sh_annotation where song <> ''")
            n_song_ann = cur.fetchone()[0]
    print(f"chapters={n_chap} clauses={n_clause} annotations={n_ann} "
          f"(clause_no is null: {n_null}) clause.song非空={n_song_clause} annotation.song非空={n_song_ann}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--dsn":
        DSN = sys.argv[2]
    if not DSN:
        raise SystemExit("ZHISHU_DSN 未配置")
    main()
