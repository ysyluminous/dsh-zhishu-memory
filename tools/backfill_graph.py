"""Backfill public graph nodes/edges from CMS relations and book links.

    python tools/backfill_graph.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MEM_DSN, ZHISHU_DSN  # noqa: E402

TYPE_MAP = {
    "question": "cms_question",
    "knowledge": "cms_knowledge",
    "lesson": "cms_lesson",
    "route": "cms_lesson",
    "term": "cms_tags",
    "people": "cms_people",
    "resource": "cms_resource",
    "book": "cms_book",
    "soft": "cms_soft",
    "link": "cms_link",
    "position": "cms_position",
    "interview_module": "cms_question_category",
    "compareProduct": "compare_product",
    "compareAlbum": "compare_album",
    "compareCategory": "compare_category",
    "article": "cms_book_chapters",
}


def table_of(kind: str) -> str:
    return TYPE_MAP.get(kind, kind)


def node_id(kind: str, ref: str) -> str:
    return f"{table_of(kind)}:{ref}"


def main() -> None:
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str, str]] = []

    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as zh:
        with zh.cursor() as cur:
            cur.execute("select source_type,source_id,target_type,target_id,relation_type from cms.cms_relations")
            for st, sid, tt, tid, rel in cur.fetchall():
                if not sid or not tid:
                    continue
                nodes[node_id(st, sid)] = {"kind": st, "ref": sid}
                nodes[node_id(tt, tid)] = {"kind": tt, "ref": tid}
                edges.append((node_id(st, sid), node_id(tt, tid), rel or "related"))

            cur.execute("select id, coalesce(book_id,'') from cms.cms_book_chapters where coalesce(book_id,'') <> ''")
            for cid, bid in cur.fetchall():
                a = f"cms_book_chapters:{cid}"
                b = f"cms_book:{bid}"
                nodes[a] = {"kind": "article", "ref": cid}
                nodes[b] = {"kind": "book", "ref": bid}
                edges.append((a, b, "part_of"))

            cur.execute("select id, coalesce(book_id,'') from cms.cms_resource where coalesce(book_id,'') <> ''")
            for rid, bid in cur.fetchall():
                a = f"cms_resource:{rid}"
                b = f"cms_book:{bid}"
                nodes[a] = {"kind": "resource", "ref": rid}
                nodes[b] = {"kind": "book", "ref": bid}
                edges.append((a, b, "part_of"))

            # titles
            by_table: dict[str, list[str]] = defaultdict(list)
            meta: dict[str, dict] = {}
            for nid, info in nodes.items():
                table = nid.split(":", 1)[0]
                ref = info["ref"]
                by_table[table].append(ref)
                meta[nid] = {"table": table, "ref": ref, "kind": info["kind"]}
            titles: dict[str, str] = {}
            for table, ids in by_table.items():
                try:
                    cur.execute(
                        """select column_name from information_schema.columns
                           where table_schema='cms' and table_name=%s""",
                        (table,),
                    )
                    cols = {r[0] for r in cur.fetchall()}
                    col = "title" if "title" in cols else "name" if "name" in cols else "test_name" if "test_name" in cols else None
                    if not col:
                        continue
                    cur.execute(
                        f"""select id, coalesce("{col}", '') from cms."{table}" where id = any(%s)""",
                        (ids,),
                    )
                except Exception:
                    zh.rollback()
                    continue
                for ref, title in cur.fetchall():
                    titles[f"{table}:{ref}"] = title or ""

    with psycopg.connect(MEM_DSN, connect_timeout=8) as mem:
        with mem.cursor() as cur:
            cur.execute("""
              create table if not exists mem.graph_node (
                id text primary key, kind text not null default '', title text not null default '',
                ref_type text not null default '', ref_id text not null default '',
                scope text not null default 'public', visibility text not null default 'public',
                region text not null default 'cn', owner_user_id text,
                updated_at timestamptz not null default now())
            """)
            cur.execute("""
              create table if not exists mem.graph_edge (
                src text not null, dst text not null, relation text not null,
                weight integer not null default 1, scope text not null default 'public',
                visibility text not null default 'public', owner_user_id text,
                updated_at timestamptz not null default now(),
                primary key (src, dst, relation))
            """)
            cur.execute("create index if not exists graph_edge_src on mem.graph_edge (src)")
            cur.execute("create index if not exists graph_edge_dst on mem.graph_edge (dst)")

            node_rows = [
                (nid, info["kind"], titles.get(nid, ""), info["table"], info["ref"])
                for nid, info in meta.items()
            ]
            cur.executemany(
                """insert into mem.graph_node (id,kind,title,ref_type,ref_id,scope,visibility)
                   values (%s,%s,%s,%s,%s,'public','public')
                   on conflict (id) do update set title=excluded.title, updated_at=now()""",
                node_rows,
            )
            edge_rows = [(s, d, r) for s, d, r in edges]
            cur.executemany(
                """insert into mem.graph_edge (src,dst,relation,scope,visibility)
                   values (%s,%s,%s,'public','public')
                   on conflict (src,dst,relation) do nothing""",
                edge_rows,
            )
            cur.execute("select count(*) from mem.graph_node")
            node_count = cur.fetchone()[0]
            cur.execute("select count(*) from mem.graph_edge")
            edge_count = cur.fetchone()[0]
        mem.commit()
    print(f"graph ok: nodes {node_count}, edges {edge_count}")


if __name__ == "__main__":
    main()

