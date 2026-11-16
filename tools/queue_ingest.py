"""Ingest conversations and CMS items into the unified queue.

    python tools/queue_ingest.py --days 7 --cms-limit 50
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MEM_DSN, ZHISHU_DSN  # noqa: E402


def qid() -> str:
    return uuid.uuid4().hex[:20]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--cms-limit", type=int, default=50)
    ap.add_argument("--user", default="owner")
    args = ap.parse_args()

    inserted = 0
    updated = 0

    with psycopg.connect(MEM_DSN, connect_timeout=8) as mem:
        with mem.cursor() as cur:
            # conversations
            cur.execute(
                """select id, source, title, snippet, message_count, updated_at
                   from mem.conv
                   where user_id=%s and updated_at >= now() - make_interval(days => %s)
                     and message_count >= 2
                   order by updated_at desc""",
                (args.user, args.days),
            )
            convs = cur.fetchall()
            for cid, source, title, snippet, count, updated in convs:
                text = (snippet or "").strip()
                low = text.startswith(("<system-reminder", "OS Version:", "You are ")) or len(text) < 20
                status = "skipped" if low else "pending"
                score = 0 if low else min(100.0, 20 + float(count or 0) / 10.0)
                cur.execute(
                    """insert into mem.queue_item
                       (id,user_id,source_type,source_id,title,summary,status,priority,score,tags,meta)
                       values (%s,%s,'conversation',%s,%s,%s,%s,2,%s,%s,%s)
                       on conflict (user_id,source_type,source_id) do update
                         set title=excluded.title, summary=excluded.summary,
                             score=excluded.score, updated_at=now(),
                             status=case when mem.queue_item.status='pending' then excluded.status else mem.queue_item.status end""",
                    (qid(), args.user, cid, f"{source}: {title or ''}", text[:400], status,
                     score, ["conversation", source], psycopg.types.json.Jsonb({
                         "message_count": count, "source": source,
                         "updated_at": updated.isoformat() if updated else None,
                     })),
                )
                inserted += cur.rowcount

            # CMS articles/resources
            if args.cms_limit > 0:
                with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as zh:
                    with zh.cursor() as zcur:
                        zcur.execute(
                            """select id, source, title, body, updated_at
                               from cms.search_docs
                               where source in ('cms_book_chapters','cms_resource')
                               order by updated_at desc nulls last limit %s""",
                            (args.cms_limit,),
                        )
                        rows = zcur.fetchall()
                for sid, source, title, body, updated in rows:
                    cur.execute(
                        """insert into mem.queue_item
                           (id,user_id,source_type,source_id,title,summary,status,priority,score,tags,meta)
                           values (%s,%s,%s,%s,%s,%s,'pending',3,50,%s,%s)
                           on conflict (user_id,source_type,source_id) do update
                             set title=excluded.title, summary=excluded.summary,
                                 score=excluded.score, updated_at=now()""",
                        (qid(), args.user, source, sid, title or "",
                         (body or "")[:400], [source], psycopg.types.json.Jsonb({
                             "updated_at": updated.isoformat() if updated else None,
                         })),
                    )
                    inserted += cur.rowcount
        mem.commit()
        with mem.cursor() as cur:
            cur.execute("select count(*) from mem.queue_item where user_id=%s", (args.user,))
            total = cur.fetchone()[0]
            cur.execute("select status,count(*) from mem.queue_item where user_id=%s group by 1 order by 2 desc", (args.user,))
            by_status = cur.fetchall()
    print(f"queue ingest done: rows touched {inserted}, total {total}, status {by_status}")


if __name__ == "__main__":
    main()
