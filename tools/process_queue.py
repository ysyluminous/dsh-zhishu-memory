"""Process queue items with the per-source prompt template.

    python tools/process_queue.py --limit 5 --dry-run
    python tools/process_queue.py --limit 20

Without an LLM endpoint it refuses to fake results; use --dry-run to inspect the
prompt, or --rule-fallback to test the plumbing (low-confidence, method=rule).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
import uuid
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MEM_DSN, ZHISHU_DSN  # noqa: E402

OMNIHIST = Path(os.path.expanduser("~")) / ".omnihist" / "omnihist.db"
LLM_BASE = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_KEY = os.getenv("LLM_API_KEY", "")


def new_id() -> str:
    return uuid.uuid4().hex[:20]


def load_content(item: dict) -> str:
    st, sid = item["source_type"], item["source_id"]
    if st == "conversation":
        if not OMNIHIST.exists():
            return ""
        con = sqlite3.connect(f"file:{OMNIHIST}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            cur.execute(
                "select role, text from message where conversation_id=? order by seq limit 400",
                (sid,),
            )
            parts = [f"{role}: {text}" for role, text in cur.fetchall() if text]
            return "\n\n".join(parts)[:60000]
        finally:
            con.close()
    if st in {"cms_book_chapters", "cms_resource", "article"}:
        with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("select coalesce(body,'') from cms.search_docs where id=%s limit 1", (sid,))
                row = cur.fetchone()
                body = (row[0] if row else "") or item.get("summary") or ""
                return body[:60000]
    return item.get("summary") or ""


def render(template: str, item: dict, content: str) -> str:
    meta = json.dumps(
        {"source_type": item["source_type"], "source_id": item["source_id"],
         "score": item.get("score"), "priority": item.get("priority")},
        ensure_ascii=False, default=str,
    )
    return (template.replace("{{title}}", item.get("title") or "")
            .replace("{{meta}}", meta)
            .replace("{{content}}", content))


def call_llm(prompt: str) -> dict:
    if not LLM_BASE or not LLM_MODEL:
        raise RuntimeError("没有配置 LLM_BASE_URL / LLM_MODEL")
    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "只输出合法 JSON，不要 Markdown 代码块。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = json.loads(r.read().decode())
    text = payload["choices"][0]["message"]["content"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def rule_extract(content: str) -> dict:
    text = re.sub(r"<[^>]+>", " ", content or "")
    text = re.sub(r"\s+", " ", text).strip()
    summary = text[:300]
    tags = re.findall(r"#([\w\u4e00-\u9fff]{1,20})", content or "")
    links = re.findall(r"https?://[^\s)>\"]+", content or "")
    return {
        "summary": summary,
        "key_points": [],
        "entities": [],
        "relations": [],
        "tags": list(dict.fromkeys(tags))[:10],
        "quotes": [],
        "coverage": {"people": False, "time": False, "numbers": False, "decisions": False, "evidence": False},
        "confidence": 0.3,
        "links": links[:10],
    }


def save(cur, user: str, item: dict, result: dict, method: str) -> None:
    extract_id = new_id()
    confidence = result.get("confidence")
    cur.execute(
        """insert into mem.extract
           (id,user_id,source_item_id,kind,title,body,confidence,coverage,state)
           values (%s,%s,%s,'summary',%s,%s,%s,%s,'extracted')""",
        (extract_id, user, item["id"], item.get("title") or "",
         result.get("summary") or "", confidence,
         json.dumps(result.get("coverage") or {}, ensure_ascii=False)),
    )
    for d in result.get("decisions") or []:
        cur.execute(
            """insert into mem.extract
               (id,user_id,source_item_id,kind,title,body,confidence,coverage,state)
               values (%s,%s,%s,'decision',%s,%s,%s,'{}','extracted')""",
            (new_id(), user, item["id"], d.get("question") or "",
             d.get("decision") or "", d.get("confidence")),
        )
    for f in result.get("facts") or []:
        cur.execute(
            """insert into mem.extract
               (id,user_id,source_item_id,kind,title,body,confidence,coverage,state)
               values (%s,%s,%s,'fact',%s,%s,%s,'{}','extracted')""",
            (new_id(), user, item["id"], "", f.get("statement") or "", f.get("confidence")),
        )
    cur.execute(
        """insert into mem.audit
           (id,user_id,extract_id,source_item_id,auditor,verdict,findings)
           values (%s,%s,%s,%s,'ai','pass',%s)""",
        (new_id(), user, extract_id, item["id"], json.dumps({"method": method}, ensure_ascii=False)),
    )
    cur.execute(
        "update mem.queue_item set status='ready', processed_at=now(), updated_at=now() where id=%s",
        (item["id"],),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--source", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rule-fallback", action="store_true")
    ap.add_argument("--user", default="owner")
    args = ap.parse_args()

    if not args.dry_run and not (LLM_BASE and LLM_MODEL) and not args.rule_fallback:
        print("没有模型端点：设置 LLM_BASE_URL / LLM_MODEL，或加 --dry-run / --rule-fallback")
        sys.exit(2)

    with psycopg.connect(MEM_DSN, connect_timeout=8) as mem:
        with mem.cursor() as cur:
            sql = "select id,source_type,source_id,title,summary,score,priority from mem.queue_item where user_id=%s and status='pending'"
            params = [args.user]
            if args.source:
                sql += " and source_type=%s"; params.append(args.source)
            sql += " order by priority asc, score desc nulls last, created_at asc limit %s"
            params.append(args.limit)
            cur.execute(sql, params)
            rows = cur.fetchall()
            items = [dict(zip(["id", "source_type", "source_id", "title", "summary", "score", "priority"], r)) for r in rows]

            for item in items:
                content = load_content(item)
                cur.execute(
                    "select template from mem.prompt_template where source_type=%s and enabled=true order by version desc limit 1",
                    (item["source_type"],),
                )
                row = cur.fetchone()
                if not row:
                    print(f"skip {item['id']}: no template for {item['source_type']}")
                    continue
                prompt = render(row[0], item, content)
                if args.dry_run:
                    print(f"--- {item['source_type']} {item['source_id']} prompt {len(prompt)} chars ---")
                    print(prompt[:600].replace("\n", " "))
                    continue
                if args.rule_fallback and not (LLM_BASE and LLM_MODEL):
                    result = rule_extract(content)
                    method = "rule"
                else:
                    result = call_llm(prompt)
                    method = "llm"
                save(cur, args.user, item, result, method)
                print(f"processed {item['id']} ({item['source_type']}) method={method}")
        mem.commit()


if __name__ == "__main__":
    main()
