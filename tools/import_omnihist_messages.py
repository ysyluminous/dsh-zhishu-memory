"""Mirror all omnihist messages into mem.conv_message (online PG).

    python tools/import_omnihist_messages.py --batch 5000

Resumable: rerun after a timeout, it upserts by omni_id. Use --start-id to
continue from a known point, and --limit for a bounded test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MEM_DSN  # noqa: E402

OMNIHIST = Path(os.path.expanduser("~")) / ".omnihist" / "omnihist.db"
SCHEMA = Path(__file__).resolve().parent.parent / "schema-conv-messages.sql"


def ms(value) -> datetime | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return datetime.fromtimestamp(n / 1000, tz=timezone.utc)


def clean(value) -> str:
    return (value or "").replace("\x00", "")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--start-id", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{OMNIHIST}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    cur = src.cursor()

    total = 0
    last_id = args.start_id
    started = time.time()

    with psycopg.connect(MEM_DSN, connect_timeout=10) as pg:
        with pg.cursor() as pgc:
            pgc.execute(SCHEMA.read_text(encoding="utf-8"))
            pgc.execute("create temp table if not exists stage_msg (like mem.conv_message including defaults)")
        pg.commit()

        while True:
            cur.execute(
                """select id,conversation_id,seq,role,kind,created_at,text,content_hash,meta
                   from message where id > ? order by id limit ?""",
                (last_id, args.batch),
            )
            rows = cur.fetchall()
            if not rows:
                break
            if args.limit and total + len(rows) > args.limit:
                rows = rows[: max(0, args.limit - total)]
            if not rows:
                break

            with pg.cursor() as pgc:
                with pgc.copy(
                    "copy stage_msg (omni_id,conversation_id,seq,role,kind,created_at,text,content_hash,meta) from stdin"
                ) as copy:
                    for r in rows:
                        copy.write_row((
                            r["id"], r["conversation_id"], r["seq"] or 0,
                            r["role"] or "", r["kind"] or "", ms(r["created_at"]),
                            clean(r["text"]), r["content_hash"] or sha(clean(r["text"])),
                            clean(json.dumps(json.loads(r["meta"]), ensure_ascii=False)) if r["meta"] else None,
                        ))
                pgc.execute(
                    """insert into mem.conv_message
                       (omni_id,conversation_id,seq,role,kind,created_at,text,content_hash,meta,synced_at)
                       select omni_id,conversation_id,seq,role,kind,created_at,text,content_hash,meta,now()
                       from stage_msg
                       on conflict (omni_id) do update
                         set conversation_id=excluded.conversation_id, seq=excluded.seq,
                             role=excluded.role, kind=excluded.kind,
                             created_at=excluded.created_at, text=excluded.text,
                             content_hash=excluded.content_hash, meta=excluded.meta,
                             synced_at=now()"""
                )
                pgc.execute("truncate stage_msg")
            pg.commit()

            total += len(rows)
            last_id = rows[-1]["id"]
            elapsed = max(1, time.time() - started)
            print(f"{total} rows, last_id={last_id}, {total/elapsed:.0f} rows/s", flush=True)
            if args.limit and total >= args.limit:
                break

    with psycopg.connect(MEM_DSN, connect_timeout=10) as pg:
        with pg.cursor() as pgc:
            pgc.execute(
                """insert into mem.sync_state (name,cursor,updated_at) values ('msg',%s,now())
                   on conflict (name) do update
                     set cursor=greatest(mem.sync_state.cursor::bigint, excluded.cursor::bigint)::text, updated_at=now()""",
                (str(last_id),),
            )
        pg.commit()
    print(f"done: {total} messages, last_id={last_id}, {time.time()-started:.1f}s")


if __name__ == "__main__":
    main()
