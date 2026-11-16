"""Check that omnihist and online mem are in sync.

    python tools/sync_audit.py

Exits 0 when conversation/message counts match and the message cursor is current.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MEM_DSN  # noqa: E402

OMNIHIST = Path(os.path.expanduser("~")) / ".omnihist" / "omnihist.db"


def main() -> None:
    src = sqlite3.connect(f"file:{OMNIHIST}?mode=ro", uri=True)
    q = src.cursor()
    q.execute("select count(*) from conversation")
    omni_convs = q.fetchone()[0]
    q.execute("select count(*), max(id) from message")
    omni_msgs, omni_max = q.fetchone()
    src.close()

    with psycopg.connect(MEM_DSN, connect_timeout=8) as pg:
        with pg.cursor() as cur:
            cur.execute("select count(*) from mem.conv")
            pg_convs = cur.fetchone()[0]
            cur.execute("select count(*), max(omni_id) from mem.conv_message")
            pg_msgs, pg_max = cur.fetchone()
            cur.execute("select cursor from mem.sync_state where name='msg'")
            row = cur.fetchone()
            cursor = int(row[0]) if row and str(row[0]).isdigit() else 0

    print(f"conversations: omnihist={omni_convs} mem={pg_convs}")
    print(f"messages:      omnihist={omni_msgs} mem={pg_msgs}")
    print(f"message cursor: {cursor} / source max {omni_max}")
    ok = omni_convs == pg_convs and omni_msgs == pg_msgs and cursor >= omni_max
    if ok:
        print("PASS")
    else:
        print("WARN: counts or cursor differ; run sync-all.ps1, then this again")
        sys.exit(1)


if __name__ == "__main__":
    main()
