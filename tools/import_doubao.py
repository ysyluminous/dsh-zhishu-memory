#!/usr/bin/env python3
"""豆包聊天记录 → mem 库（mem.conv / mem.conv_message）。

数据源：D:\\data\\down\\豆包聊天记录_2026-11-15T07-07-27-215Z.json
库：memory-mcp/.env 的 MEM_DSN

映射（与现有 mem 结构兼容，不新造表、不改 schema）：
  mem.conv          id='doubao:'||conv.id、source='豆包'、external_id=conv.id、
                    title、message_count、created_at=min(msg.createdAt)、
                    updated_at=max(msg.createdAt)、snippet=首条 user.text 前 300 字、
                    hash=sha256(title|首条 text)、user_id='owner'、project/cwd/model=''
  mem.conv_message  omni_id 从 max(omni_id)+1 起递增（每次运行现读，不写死）、
                    conversation_id 同上、seq=msg.index（原值不重排）、role、
                    kind='text'、created_at=msg.createdAt、text=msg.text、
                    content_hash=sha256(text)、synced_at=now()、
                    meta={doubao_id,index,createdAtText,conv_url[,raw_created_at]}

幂等：每个对话一个事务，先 delete 该对话的 conv_message 再插；mem.conv 用
      on conflict (id) do update。重跑行数不变。
边界：只处理 id 形如 'doubao:%' 的行，其它来源一行不碰；不写 mem.live_event。

用法：
  python memory-mcp/tools/import_doubao.py            # 导入
  python memory-mcp/tools/import_doubao.py --report   # 只读核对（不写）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import errors as pgerrors

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

DSN = os.getenv("MEM_DSN", "")
SRC = Path(r"D:\data\down\豆包聊天记录_2026-11-15T07-07-27-215Z.json")
SOURCE_LABEL = "豆包"
PREFIX = "doubao:"

CONV_SQL = """
insert into mem.conv (id, source, external_id, title, project, cwd, model,
                      created_at, updated_at, message_count, snippet, hash, user_id)
values (%s, %s, %s, %s, '', '', '', %s, %s, %s, %s, %s, 'owner')
on conflict (id) do update set
  source        = excluded.source,
  external_id   = excluded.external_id,
  title         = excluded.title,
  created_at    = excluded.created_at,
  updated_at    = excluded.updated_at,
  message_count = excluded.message_count,
  snippet       = excluded.snippet,
  hash          = excluded.hash,
  user_id       = excluded.user_id
"""

MSG_SQL = """
insert into mem.conv_message
  (omni_id, conversation_id, seq, role, kind, created_at, text, content_hash, meta, synced_at)
values (%s, %s, %s, %s, 'text', %s, %s, %s, %s::jsonb, now())
"""


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def parse_ts(value):
    """createdAt → (datetime|None, raw_ok)。兼容 epoch 秒/毫秒 与 ISO 字符串。

    实测本文件 3,738 条 createdAt 全是 epoch 整数（不是卡上写的 ISO 字符串），
    所以这里按类型分派；解析不了的返回 (None, raw)，由调用方写进 meta.raw_created_at。
    """
    if isinstance(value, bool):
        return None, str(value)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # 毫秒
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc), None
        except (OverflowError, OSError, ValueError):
            return None, str(value)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt, None
        except ValueError:
            return None, raw
    return None, (None if value is None else str(value))


def ordered(messages: list[dict]) -> list[dict]:
    return sorted(messages or [], key=lambda m: (m.get("index") is None, m.get("index") or 0))


def import_all(path: Path) -> Counter:
    payload = json.loads(path.read_text(encoding="utf-8"))
    convs = payload["conversations"]
    stats: Counter = Counter()
    stats["json_conversations"] = len(convs)
    stats["json_messages"] = sum(len(c.get("messages") or []) for c in convs)

    with psycopg.connect(DSN, connect_timeout=20) as conn:
        # 现读一次 max(omni_id)，之后每个对话事务内再复核一次，双保险
        with conn.cursor() as cur:
            cur.execute("select coalesce(max(omni_id), 0) from mem.conv_message")
            omni_hint = int(cur.fetchone()[0])
        conn.commit()

        for conv in convs:
            cid = PREFIX + str(conv["id"])
            msgs = ordered(conv.get("messages"))
            texts = [(m.get("text") or "") for m in msgs]
            stamps = []
            for m in msgs:
                dt, raw = parse_ts(m.get("createdAt"))
                if raw is not None:
                    stats["bad_created_at"] += 1
                stamps.append(dt)
            good = [d for d in stamps if d is not None]
            first_user = next(((m.get("text") or "") for m in msgs if m.get("role") == "user"), "")
            first_text = texts[0] if texts else ""
            toggle = (
                cid,
                SOURCE_LABEL,
                str(conv["id"]),
                (conv.get("title") or "")[:500],
                min(good) if good else None,
                max(good) if good else None,
                len(msgs),
                first_user[:300],
                sha256(f"{conv.get('title') or ''}|{first_text}"),
            )

            rows = []
            for seq_no, m in enumerate(msgs):
                dt, raw = parse_ts(m.get("createdAt"))
                meta = {
                    "doubao_id": m.get("id"),
                    "index": m.get("index"),
                    "createdAtText": m.get("createdAtText"),
                    "conv_url": conv.get("url") or "",
                }
                if raw is not None:
                    meta["raw_created_at"] = raw
                rows.append((
                    None,  # omni_id 占位，事务内分配
                    cid,
                    int(m.get("index") if m.get("index") is not None else seq_no + 1),
                    str(m.get("role") or ""),
                    dt,
                    m.get("text") or "",
                    sha256(m.get("text") or ""),
                    json.dumps(meta, ensure_ascii=False),
                ))

            for attempt in range(5):
                try:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(CONV_SQL, toggle)
                            cur.execute(
                                "delete from mem.conv_message where conversation_id = %s", (cid,))
                            if rows:
                                cur.execute("select coalesce(max(omni_id), 0) from mem.conv_message")
                                base = max(int(cur.fetchone()[0]), omni_hint) + 1
                                cur.executemany(
                                    MSG_SQL,
                                    [(base + i,) + r[1:] for i, r in enumerate(rows)])
                                omni_hint = base + len(rows) - 1
                    stats["conversations"] += 1
                    stats["messages"] += len(rows)
                    stats["empty_conversations"] += 1 if not rows else 0
                    break
                except pgerrors.UniqueViolation:
                    # 与其它写入方抢 omni_id：整段重试（delete 幂等）
                    stats["omni_id_retry"] += 1
                    omni_hint += len(rows) + 1
            else:
                stats["conversations_failed"] += 1
    return stats


def report(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    convs = payload["conversations"]
    j_conv = len(convs)
    j_msg = sum(len(c.get("messages") or []) for c in convs)
    j_role = Counter(m.get("role") for c in convs for m in (c.get("messages") or []))
    print(f"[JSON] 对话 {j_conv} / 消息 {j_msg} / role {dict(j_role)}")

    with psycopg.connect(DSN, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from mem.conv where source = %s", (SOURCE_LABEL,))
            print(f"[DB] mem.conv source='豆包' = {cur.fetchone()[0]}")
            cur.execute("select count(*) from mem.conv where id like %s", (PREFIX + "%",))
            print(f"[DB] mem.conv id like 'doubao:%' = {cur.fetchone()[0]}")
            cur.execute("select count(*) from mem.conv_message where conversation_id like %s",
                        (PREFIX + "%",))
            db_msg = cur.fetchone()[0]
            print(f"[DB] mem.conv_message conversation_id like 'doubao:%' = {db_msg}")
            cur.execute("""select role, count(*) from mem.conv_message
                           where conversation_id like %s group by 1 order by 1""",
                        (PREFIX + "%",))
            print(f"[DB] 按 role 分组: {dict(cur.fetchall())}")
            cur.execute("select count(*) from mem.conv_message where conversation_id not like %s",
                        (PREFIX + "%",))
            print(f"[DB] 非豆包消息行数（应保持不动）= {cur.fetchone()[0]}")
            cur.execute("select count(*) from mem.conv where id not like %s", (PREFIX + "%",))
            print(f"[DB] 非豆包对话行数（应保持不动）= {cur.fetchone()[0]}")
            cur.execute("""select count(*) from mem.conv_message
                           where conversation_id like %s
                             and (meta->>'conv_url' is null or meta->>'conv_url' = '')""",
                        (PREFIX + "%",))
            print(f"[DB] meta.conv_url 缺失行数 = {cur.fetchone()[0]}")
            cur.execute("""select count(*) from mem.conv_message
                           where conversation_id like %s and meta ? 'raw_created_at'""",
                        (PREFIX + "%",))
            print(f"[DB] createdAt 解析失败（raw_created_at）行数 = {cur.fetchone()[0]}")

            # 全量逐条核对：JSON 与 DB 的 text / created_at / seq / meta
            db = {}
            cur.execute("""select conversation_id, seq, role, created_at, text, content_hash, meta
                           from mem.conv_message where conversation_id like %s""",
                        (PREFIX + "%",))
            for cid, seq, role, created, text, chash, meta in cur.fetchall():
                db[(cid, seq)] = (role, created, text, chash, meta)

    bad_text = bad_ts = bad_role = bad_hash = bad_meta = missing = 0
    for conv in convs:
        cid = PREFIX + conv["id"]
        for m in (conv.get("messages") or []):
            key = (cid, int(m.get("index")))
            if key not in db:
                missing += 1
                continue
            role, created, text, chash, meta = db[key]
            if text != (m.get("text") or ""):
                bad_text += 1
            if chash != sha256(m.get("text") or ""):
                bad_hash += 1
            if role != m.get("role"):
                bad_role += 1
            want_ts, _ = parse_ts(m.get("createdAt"))
            if created != want_ts:
                bad_ts += 1
            if (meta or {}).get("conv_url") != (conv.get("url") or ""):
                bad_meta += 1
    print(f"[核对] 缺行 {missing} / text 不一致 {bad_text} / content_hash 不一致 {bad_hash} / "
          f"role 不一致 {bad_role} / created_at 不一致 {bad_ts} / conv_url 不一致 {bad_meta}")

    with psycopg.connect(DSN, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for conv_id in ("cron-jobs", "38436937405976834"):
                cid = PREFIX + conv_id
                cur.execute("""select id, source, external_id, title, message_count,
                                      created_at, updated_at, snippet, hash, user_id
                               from mem.conv where id = %s""", (cid,))
                row = cur.fetchone()
                print(f"\n[样本对话 {cid}]")
                print(f"  conv 行: {row}")
                cur.execute("""select seq, role, created_at, left(text, 60), content_hash,
                                      meta->>'conv_url', meta->>'doubao_id', meta->>'createdAtText'
                               from mem.conv_message where conversation_id = %s
                               order by seq limit 3""", (cid,))
                got = cur.fetchall()
                print(f"  前 3 条消息: {got if got else '（该对话 0 条消息）'}")
                src = next(c for c in convs if c["id"] == conv_id)
                print(f"  JSON 原样: {json.dumps(src, ensure_ascii=False)[:400]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="只读核对，不写库")
    parser.add_argument("--file", default=str(SRC))
    args = parser.parse_args()
    if not DSN:
        raise SystemExit("MEM_DSN 未配置")
    path = Path(args.file)
    if args.report:
        report(path)
        return
    stats = import_all(path)
    print("[导入] " + " / ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    if stats.get("conversations_failed"):
        raise SystemExit(f"失败对话 {stats['conversations_failed']} 个，需复查")
    report(path)


if __name__ == "__main__":
    main()
