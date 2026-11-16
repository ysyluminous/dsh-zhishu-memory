"""Import the legacy MySQL ai_prompt library into mem.prompt_library.

    python tools/import_prompts.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pymysql
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MEM_DSN  # noqa: E402

ENV = Path(r"D:\code\yunxiao\zhishu\deploy\.env")
DSN = ""
for line in ENV.read_text(encoding="utf-8").splitlines():
    if line.startswith("MYSQL_DSN="):
        DSN = line.split("=", 1)[1].strip()
m = re.match(r"([^:]+):([^@]+)@tcp\(([^:]+):(\d+)\)/([^?]+)", DSN)
user, password, host, port, db = m.groups()

BATCH = 300


def main() -> None:
    my = pymysql.connect(host=host, port=int(port), user=user, password=password,
                         database=db, charset="utf8mb4", connect_timeout=10)
    cur = my.cursor()
    cur.execute("""
      select id, coalesce(ref_id,''), coalesce(title,''), coalesce(description,''),
             coalesce(content,''), coalesce(category,''), tags, param,
             coalesce(is_public,''), user_id, updated_at
      from ai_prompt
    """)
    rows = cur.fetchall()
    my.close()

    def as_json(value):
        if value is None:
            return None
        text = value if isinstance(value, str) else str(value)
        try:
            return json.dumps(json.loads(text), ensure_ascii=False)
        except Exception:
            return json.dumps([text], ensure_ascii=False)

    payload = [
        (r[0], r[1], r[2], r[3], r[4], r[5], as_json(r[6]), as_json(r[7]), r[8], r[9], r[10])
        for r in rows
    ]

    with psycopg.connect(MEM_DSN, connect_timeout=8) as pg:
        with pg.cursor() as pc:
            pc.executemany(
                """insert into mem.prompt_library
                   (id,ref_id,title,description,content,category,tags,params,is_public,user_id,updated_at)
                   values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,coalesce(%s::timestamptz,now()))
                   on conflict (id) do update
                     set ref_id=excluded.ref_id, title=excluded.title, description=excluded.description,
                         content=excluded.content, category=excluded.category, tags=excluded.tags,
                         params=excluded.params, is_public=excluded.is_public,
                         user_id=excluded.user_id, updated_at=excluded.updated_at""",
                payload,
            )
        pg.commit()
        with pg.cursor() as pc:
            pc.execute("select count(*) from mem.prompt_library")
            print("prompt_library", pc.fetchone()[0])


if __name__ == "__main__":
    main()
