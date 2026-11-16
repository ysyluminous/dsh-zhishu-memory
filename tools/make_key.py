"""Create a user and an API key for zhishu-memory. Prints the key once.

    python tools/make_key.py --user alice --name laptop
"""
from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MEM_DSN  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--name", default="default")
    args = ap.parse_args()

    key = "zm_" + secrets.token_urlsafe(24)
    digest = hashlib.sha256(key.encode()).hexdigest()
    with psycopg.connect(MEM_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute(
                "insert into mem.app_user (id,name) values (%s,%s) on conflict (id) do nothing",
                (args.user, args.user),
            )
            cur.execute(
                "insert into mem.api_key (key_hash,user_id,name) values (%s,%s,%s)",
                (digest, args.user, args.name),
            )
        c.commit()
    print(key)


if __name__ == "__main__":
    main()
