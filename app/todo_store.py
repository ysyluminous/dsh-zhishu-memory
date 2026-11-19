"""待办统一存到 growth.time_entry：网页时间线、MCP、账本是同一张表。

MCP 侧沿用 1..5 的 priority 和 pending/doing/done/canceled 状态，
落库时转成账本的 0/1/2 与 planned/doing/done/canceled，读回来再转回去。
"""
from __future__ import annotations

from datetime import datetime

import psycopg

from .config import ZHISHU_DSN

_MCP_TO_GROWTH_PRIORITY = {1: 0, 2: 1, 3: 2, 4: 2, 5: 2}
_GROWTH_TO_MCP_PRIORITY = {0: 1, 1: 2, 2: 3}
_MCP_TO_GROWTH_STATUS = {
    "pending": "planned", "doing": "doing", "done": "done", "canceled": "canceled",
}
_GROWTH_TO_MCP_STATUS = {
    "planned": "pending", "doing": "doing", "done": "done",
    "skipped": "canceled", "canceled": "canceled",
}


def _conn():
    return psycopg.connect(ZHISHU_DSN, connect_timeout=8)


def _parse_dt(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _ledger(cur, user_id: str) -> str:
    cur.execute("select ledger_user_id from growth.ledger_identity where login_id = %s", (user_id,))
    row = cur.fetchone()
    return row[0] if row else user_id


def _split_project(note: str) -> tuple[str, str]:
    text = note or ""
    if text.startswith("项目：") and " ｜ " in text:
        head, rest = text.split(" ｜ ", 1)
        return head[3:].strip(), rest
    return "", text


def _norm_owner(owner: str) -> str:
    """growth.time_entry 的 CHECK 只允许 'human' / 'ai'（time_entry_owner_check）。

    调用方习惯传 'agent'，统一映射成 'ai'，不做 schema 变更。
    """
    value = (owner or "human").strip().lower()
    if value in ("agent", "bot", "ai"):
        return "ai"
    return "human"


def _row_to_todo(row) -> dict:
    import math
    tid, title, status, priority, planned_start, note, created_at = row[:7]
    owner = row[7] if len(row) > 7 else None
    project, detail = _split_project(note)
    due = planned_start.isoformat() if planned_start else None
    if due is None:
        due = None if not math.isnan(priority) else None
    return {
        "id": str(tid),
        "title": title,
        "status": _GROWTH_TO_MCP_STATUS.get(status, status),
        "priority": _GROWTH_TO_MCP_PRIORITY.get(priority, 3),
        "due_at": due,
        "project": project,
        "detail": detail,
        "owner": owner,
        "created_at": created_at.isoformat() if created_at else None,
    }


def todo_add(user_id: str, title: str, detail: str = "", due_at: str = "",
             priority: int = 2, project: str = "", source: str = "",
             owner: str = "human", priority_override: bool = False) -> dict:
    """owner：human=账号主本人，agent=某个 bot 名。

    之前硬编码 'human'（2026-11-16 前），agent 待办标不出来，
    /growth/time 也没法只读展示 bot 的活。改成参数，默认 human 保持旧行为。

    2026-11-18 老大定（写入层硬规则，不靠 agent 自觉）：
    **自动扫描来的待办（source 命中 auto-scan）永远不能占最高优先级**，一律压到 growth 1；
    只有显式传 priority_override=true（仅 Chief 用）才按传入值落库。
    理由：07:00/08:00 定时任务的 prompt 没写死 priority，agent 每次自己挑、挑出过 p0，
    跟人手写的最高档混排——改 prompt 不可靠，在写入层一次收敛。
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("待办标题不能为空")
    prio = _MCP_TO_GROWTH_PRIORITY.get(max(1, min(int(priority), 5)), 1)
    if (source or "").strip().lower().startswith("auto-scan") and not priority_override:
        prio = 1  # 自动生成固定次高档；人手写/Chief 点名才可能到 0
    note = f"项目：{project} ｜ {detail}" if project else (detail or "")
    owner = _norm_owner(owner)
    with _conn() as c:
        with c.cursor() as cur:
            ledger = _ledger(cur, user_id)
            cur.execute(
                """insert into growth.time_entry
                     (user_id, kind, title, note, planned_start, status, priority, owner, source)
                   values (%s, 'task', %s, %s, %s, 'planned', %s, %s, %s)
                   returning id""",
                (ledger, title, note, _parse_dt(due_at), prio, owner, source or "mcp"),
            )
            tid = cur.fetchone()[0]
        c.commit()
    return {"ok": True, "id": str(tid), "title": title, "due_at": due_at or None, "owner": owner}


def todo_list(user_id: str, status: str = "", limit: int = 50) -> dict:
    limit = max(1, min(limit, 100))
    sql = """select id, title, status, priority, planned_start, note, created_at, owner
             from growth.time_entry where user_id = %s and kind = 'task'"""
    args: list = []
    with _conn() as c:
        with c.cursor() as cur:
            ledger = _ledger(cur, user_id)
            args.append(ledger)
            if status:
                sql += " and status = %s"
                args.append(_MCP_TO_GROWTH_STATUS.get(status, status))
            sql += " order by priority asc, planned_start asc nulls last, id desc limit %s"
            args.append(limit)
            cur.execute(sql, args)
            rows = cur.fetchall()
    return {"count": len(rows), "todos": [_row_to_todo(r) for r in rows]}


def todo_query(user_id: str, status: str = "", owner: str = "", priority: int | None = None,
               project: str = "", limit: int = 100) -> dict:
    """只读查询（给工作台/外部脚本用，方案 C：POST + body）。

    与 todo_list 的区别：额外支持按 owner(ai/human)、priority、project 过滤，
    并把 note 里的「项目：X ｜ detail」拆成结构化 project/detail 返回（前端不用再猜）。
    """
    limit = max(1, min(int(limit or 100), 200))
    sql = ("select id,title,status,priority,planned_start,note,created_at,owner "
           "from growth.time_entry where user_id = %s and kind = 'task'")
    args: list = []
    with _conn() as c:
        with c.cursor() as cur:
            ledger = _ledger(cur, user_id)
            args.append(ledger)
            if status:
                sql += " and status = %s"
                args.append(_MCP_TO_GROWTH_STATUS.get(status, status))
            if owner:
                sql += " and owner = %s"
                args.append(_norm_owner(owner))
            if priority is not None:
                sql += " and priority = %s"
                args.append(_MCP_TO_GROWTH_PRIORITY.get(max(1, min(int(priority), 5)), 1))
            if project:
                # 项目目前拼在 note 前缀里（项目：X ｜ detail）；按前缀匹配，避免误伤正文
                sql += " and note like %s"
                args.append("项目：" + project + " ｜%")
            sql += " order by priority asc, id desc limit %s"
            args.append(limit)
            cur.execute(sql, args)
            rows = cur.fetchall()
    items = [_row_to_todo(r) for r in rows]
    groups = {"p0": 0, "p1": 0, "p2": 0}
    for it in items:
        p = int(it.get("priority") or 3)
        if p <= 1:
            groups["p0"] += 1
        elif p == 2:
            groups["p1"] += 1
        else:
            groups["p2"] += 1
    return {"count": len(items), "todos": items, "by_priority": groups}


def todo_update(user_id: str, todo_id: str, status: str = "", title: str = "",
                detail: str = "", due_at: str = "", priority: int | None = None,
                project: str = "") -> dict:
    try:
        tid = int(str(todo_id).strip())
    except (TypeError, ValueError):
        raise ValueError("待办 ID 不存在")
    sets, args = [], []
    if status:
        if status not in _MCP_TO_GROWTH_STATUS:
            raise ValueError("非法状态")
        sets.append("status=%s")
        args.append(_MCP_TO_GROWTH_STATUS[status])
    if title:
        sets.append("title=%s")
        args.append(title)
    if detail or project:
        sets.append("note=%s")
        args.append(f"项目：{project} ｜ {detail}" if project else detail)
    if due_at:
        sets.append("planned_start=%s")
        args.append(_parse_dt(due_at))
    if priority is not None:
        sets.append("priority=%s")
        args.append(_MCP_TO_GROWTH_PRIORITY.get(max(1, min(int(priority), 5)), 1))
    if not sets:
        raise ValueError("没有要更新的字段")
    sets.append("updated_at=now()")
    args += [tid]
    with _conn() as c:
        with c.cursor() as cur:
            ledger = _ledger(cur, user_id)
            args.append(ledger)
            cur.execute(
                f"update growth.time_entry set {', '.join(sets)} where id=%s and user_id=%s",
                args,
            )
            if cur.rowcount == 0:
                raise ValueError("待办不存在")
        c.commit()
    return {"ok": True, "id": str(tid)}
