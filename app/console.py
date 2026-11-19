"""只读控制台：会话 / 上下文 / 门控日志 / 记忆与题库。

设计约束（2026-11-17 账号主定）：
- 控制台是**服务自带的独立 HTML 页面**，不嵌 DSH；同源挂在服务端口（41016）。
- 第一版**只读**：本模块只有 SELECT，没有任何写库语句。改的能力要连带审核流，放第二轮。
- 页头 env 直接取 config.SERVICE_ENV，与 /health 同一个来源，不另存一份配置。

「上下文」视图的数据现实（必须先说清楚，否则页面会撒谎）：
- 能读到：每次注入命中了哪几条、注入字符数、检索耗时（mem.gate_log，2394 行）。
- 读不到：上下文 token 占用、占比、压缩前后大小——这些在 mem.ctx_trace，而它在线上
  至今 **0 行**，采集端（DSH 插件的余量/压缩观测）还没实现。
- 所以页面里 token 只有「注入量估算（字符 ÷ 1.7）」，占比的分母由人工填一个上下文上限，
  压缩一律显示「未采集」。不拿估算值冒充实测值。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse

# 统一序列化出口：Decimal / datetime 等数据库原样值不再让响应 500（app/json_out.py）。
from .json_out import SafeJSONResponse as JSONResponse

from .config import DB_LABEL, MEM_DSN, REQUIRE_KEY, SERVICE_ENV
from .mcp_audit import audit_info, load_events
from .service import _conn

_HERE = Path(__file__).parent
_HTML_CACHE: str | None = None

# 同一份本地时间口径，所有查询共用；跨库比对时也按这个时区，别用默认 UTC 渲染。
_TZ = "Asia/Shanghai"
_TS = "to_char({col} at time zone 'Asia/Shanghai','MM-DD HH24:MI:SS')"


def _html() -> str:
    global _HTML_CACHE
    if _HTML_CACHE is None:
        _HTML_CACHE = (_HERE / "console.html").read_text(encoding="utf-8")
    return _HTML_CACHE


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    got = _rows(sql, params)
    return got[0] if got else None


def _limit(request: Request, default: int = 50, cap: int = 200) -> int:
    try:
        return max(1, min(int(request.query_params.get("limit", default)), cap))
    except (TypeError, ValueError):
        return default


def _offset(request: Request) -> int:
    try:
        return max(0, int(request.query_params.get("offset", 0)))
    except (TypeError, ValueError):
        return 0


def _like(request: Request) -> str:
    return f"%{(request.query_params.get('q') or '').strip()}%"


def _count(sql: str, params: tuple[Any, ...] = ()) -> int:
    row = _one(sql, params)
    return int(next(iter(row.values()))) if row else 0


# ── 概览 ────────────────────────────────────────────────────────────────────
def overview(_: Request) -> JSONResponse:
    row = _one(
        f"""
        select
          (select count(*) from mem.conv) as convs,
          (select count(*) from mem.conv where id like %s) as dsh_convs,
          (select count(*) from mem.conv_message) as messages,
          (select count(*) from mem.gate_log) as gates,
          (select count(*) from mem.ctx_trace) as ctx_traces,
          (select count(*) from mem.doc) as docs,
          (select count(*) from mem.quiz_bank) as quiz,
          (select {_TS.format(col='max(created_at)')} from mem.conv_message) as last_msg,
          (select {_TS.format(col='max(created_at)')} from mem.gate_log) as last_gate,
          (select count(*) from mem.idea) as ideas,
          (select count(*) from mem.todo) as todos
        """,
        ("dsh:%",),
    ) or {}
    return JSONResponse({"ok": True, "env": SERVICE_ENV, "db": DB_LABEL, "data": row})


# ── 视图一：会话 ─────────────────────────────────────────────────────────────
def convs(request: Request) -> JSONResponse:
    like = _like(request)
    where = "(id like %s or coalesce(title,'') like %s)"
    params = (like, like)
    total = _count(f"select count(*) as n from mem.conv where {where}", params)
    rows = _rows(
        f"""
        select id, source, title, message_count, model,
               {_TS.format(col='updated_at')} as updated_at
        from mem.conv
        where {where}
        order by updated_at desc
        limit %s offset %s
        """,
        (*params, _limit(request), _offset(request)),
    )
    return JSONResponse({"ok": True, "total": total, "rows": rows})


def conv_detail(request: Request) -> JSONResponse:
    cid = (request.query_params.get("id") or "").strip()
    if not cid:
        return JSONResponse({"ok": False, "error": "id required"}, status_code=400)
    conv = _one(
        f"""
        select id, source, external_id, title, project, cwd, model,
               message_count, {_TS.format(col='created_at')} as created_at,
               {_TS.format(col='updated_at')} as updated_at
        from mem.conv where id = %s
        """,
        (cid,),
    )
    if not conv:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    # 最近 60 条，正文截断到 4000 字：控制台是看结构，不是看全文。
    msgs = _rows(
        f"""
        select seq, role, kind, {_TS.format(col='created_at')} as created_at,
               left(text, 4000) as text, (length(text) > 4000) as truncated
        from mem.conv_message
        where conversation_id = %s
        order by seq desc
        limit 60
        """,
        (cid,),
    )
    # 这个会话被门控检索过几次：直接串到「上下文」视图，少点一次。
    gates = _rows(
        f"""
        select id, {_TS.format(col='created_at')} as created_at, source, bot,
               count, injected_chars, latency_ms, ok, left(query, 120) as q
        from mem.gate_log
        where conversation_id = %s
        order by id desc limit 100
        """,
        (cid,),
    )
    return JSONResponse({"ok": True, "conv": conv, "messages": msgs, "gates": gates})


# ── 视图二：上下文（核心） ────────────────────────────────────────────────────
def ctx_list(request: Request) -> JSONResponse:
    cid = (request.query_params.get("conversation_id") or "").strip()
    like = f"%{cid}%" if cid else "%"
    params = (like,)
    total = _count("select count(*) as n from mem.gate_log where conversation_id like %s", params)
    rows = _rows(
        f"""
        select g.id, {_TS.format(col='g.created_at')} as created_at,
               g.conversation_id, g.source, g.bot, g.count,
               g.injected_chars, g.latency_ms, g.ok,
               left(g.query, 140) as q
        from mem.gate_log g
        where g.conversation_id like %s
        order by g.id desc
        limit %s offset %s
        """,
        (*params, _limit(request), _offset(request)),
    )
    return JSONResponse({"ok": True, "total": total, "rows": rows})


def _resolve_hits(hit_ids: list[str], scores: list[float]) -> list[dict[str, Any]]:
    """把 hit_ids 翻成人能读的标题：doc / conv / entity 三张表各查一次，批量 IN。"""
    if not hit_ids:
        return []
    meta: dict[str, dict[str, Any]] = {}
    docs = _rows(
        "select path, kind, title from mem.doc where path = any(%s)",
        (hit_ids,),
    )
    for r in docs:
        meta[r["path"]] = {"type": "doc", "kind": r["kind"], "title": r["title"] or r["path"]}
    convs_ = _rows(
        "select id, title, message_count from mem.conv where id = any(%s)",
        (hit_ids,),
    )
    for r in convs_:
        meta[r["id"]] = {"type": "conv", "kind": "conversation",
                         "title": r["title"] or r["id"], "message_count": r["message_count"]}
    ents = _rows("select id, kind, name from mem.entity where id = any(%s)", (hit_ids,))
    for r in ents:
        meta[r["id"]] = {"type": "entity", "kind": r["kind"], "title": r["name"] or r["id"]}
    out = []
    for i, hid in enumerate(hit_ids):
        m = meta.get(hid) or {"type": "unknown", "kind": "", "title": "（表中未命中：可能已删或来自公共库）"}
        out.append({
            "n": i + 1,
            "id": hid,
            "score": scores[i] if i < len(scores) else None,
            **m,
        })
    return out


def ctx_detail(request: Request) -> JSONResponse:
    try:
        gid = int(request.query_params.get("id") or 0)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    gate = _one(
        f"""
        select id, {_TS.format(col='created_at')} as created_at, user_id, source, bot,
               conversation_id, turn, query, hit_ids, hit_scores, count,
               injected_chars, latency_ms, ok, error
        from mem.gate_log where id = %s
        """,
        (gid,),
    )
    if not gate:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    hits = _resolve_hits(list(gate.get("hit_ids") or []), list(gate.get("hit_scores") or []))
    # ctx_trace 目前 0 行；有数据时这里会返回，页面自动从「未采集」变成实测值。
    traces = _rows(
        f"""
        select id, {_TS.format(col='created_at')} as created_at, session_id, turn, step,
               model, kind, injected_tokens, history_tokens, system_tokens,
               ctx_tokens, ctx_limit, ctx_pct, prompt_tokens, completion_tokens,
               cache_read_tokens, cache_pct, compaction, latency_ms, ok, error
        from mem.ctx_trace
        where session_id = %s
        order by id desc limit 5
        """,
        (gate.get("conversation_id") or "",),
    ) if gate.get("conversation_id") else []
    # 这一轮前后的对话：按注入时刻在 conv_message 里取最近的 3 条。
    around: list[dict[str, Any]] = []
    if gate.get("conversation_id"):
        around = _rows(
            f"""
            select seq, role, kind, {_TS.format(col='created_at')} as created_at,
                   left(text, 600) as text
            from mem.conv_message
            where conversation_id = %s
              and created_at <= (select created_at from mem.gate_log where id = %s) + interval '2 minutes'
            order by seq desc limit 6
            """,
            (gate["conversation_id"], gid),
        )
        around.reverse()
    chars = int(gate.get("injected_chars") or 0)
    return JSONResponse({
        "ok": True,
        "gate": gate,
        "hits": hits,
        # 估算口径写在字段名里，别让人误当实测
        "injected_tokens_est": round(chars / 1.7) if chars else 0,
        "tokens_est_rule": "round(injected_chars / 1.7)，中文约 1.7 字符/token；仅估算，非实测",
        "ctx_traces": traces,
        "ctx_trace_rows_in_db": _count("select count(*) as n from mem.ctx_trace"),
        "around": around,
    })


# ── 视图三：门控日志 ─────────────────────────────────────────────────────────
def gate_log(request: Request) -> JSONResponse:
    source = (request.query_params.get("source") or "").strip()
    ok = (request.query_params.get("ok") or "").strip()
    params: list[Any] = []
    where = ["true"]
    if source:
        where.append("source = %s")
        params.append(source)
    if ok in ("t", "f", "true", "false"):
        where.append("ok = %s")
        params.append(ok in ("t", "true"))
    clause = " and ".join(where)
    total = _count(f"select count(*) as n from mem.gate_log where {clause}", tuple(params))
    rows = _rows(
        f"""
        select id, {_TS.format(col='created_at')} as created_at, conversation_id,
               source, bot, turn, count, injected_chars, latency_ms, ok, error,
               left(query, 300) as q, hit_ids
        from mem.gate_log
        where {clause}
        order by id desc
        limit %s offset %s
        """,
        (*params, _limit(request), _offset(request)),
    )
    sources = _rows("select source, count(*) as n from mem.gate_log group by 1 order by 2 desc")
    return JSONResponse({"ok": True, "total": total, "rows": rows, "sources": sources})


# ── 视图四：记忆与题库 ───────────────────────────────────────────────────────
def docs(request: Request) -> JSONResponse:
    like = _like(request)
    kind = (request.query_params.get("kind") or "").strip()
    where = "(path like %s or coalesce(title,'') like %s)"
    params: list[Any] = [like, like]
    if kind:
        where += " and kind = %s"
        params.append(kind)
    total = _count(f"select count(*) as n from mem.doc where {where}", tuple(params))
    rows = _rows(
        f"""
        select path, kind, title, length(body) as body_len,
               {_TS.format(col='updated_at')} as updated_at
        from mem.doc
        where {where}
        order by updated_at desc
        limit %s offset %s
        """,
        (*params, _limit(request), _offset(request)),
    )
    kinds = _rows("select kind, count(*) as n from mem.doc group by 1 order by 2 desc")
    return JSONResponse({"ok": True, "total": total, "rows": rows, "kinds": kinds})


def doc_detail(request: Request) -> JSONResponse:
    path = (request.query_params.get("path") or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
    row = _one(
        f"""
        select path, kind, title, body, hash,
               {_TS.format(col='updated_at')} as updated_at
        from mem.doc where path = %s
        """,
        (path,),
    )
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    body = row.get("body") or ""
    return JSONResponse({"ok": True, "doc": row, "body_len": len(body),
                         "body": body[:20000], "truncated": len(body) > 20000})


def quiz(request: Request) -> JSONResponse:
    total = _count("select count(*) as n from mem.quiz_bank")
    rows = _rows(
        """
        select qid, scope, pool, qtype, question, answer, difficulty, active, fact_as_of,
               evidence
        from mem.quiz_bank
        order by qid
        limit %s offset %s
        """,
        (_limit(request), _offset(request)),
    )
    return JSONResponse({"ok": True, "total": total, "rows": rows})


# ── 视图五：MCP 服务（谁连上来、调了什么） ─────────────────────────────────
# 数据源是 app/mcp_audit.py 写的本地 JSONL，不碰库：库挂的时候，这一页仍然能用。
_SERVER: Any = None


def _client_rows(events: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """按「客户端 + 用户 + 密钥 + IP」聚合；同一台机器重连多次只算一行。"""
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for event in events:
        key = (
            str(event.get("c") or "（未知客户端）"),
            str(event.get("cv") or ""),
            str(event.get("u") or "") + "|" + str(event.get("kf") or ""),
            str(event.get("ip") or ""),
        )
        row = groups.get(key)
        if row is None:
            row = {
                "client": key[0],
                "version": key[1],
                "user": str(event.get("u") or ""),
                "user_name": str(event.get("un") or ""),
                "key_fp": str(event.get("kf") or ""),
                "auth": str(event.get("auth") or ""),
                "ip": key[3],
                "ua": str(event.get("ua") or ""),
                "sessions": 0,
                "calls": 0,
                "errors": 0,
                "lists": 0,
                "calls_24h": 0,
                "errors_24h": 0,
                "last_tool": "",
                "first_ms": 0,
                "last_ms": 0,
                "first": "",
                "last": "",
            }
            groups[key] = row
        ms = int(event.get("ms") or 0)
        if ms and (row["first_ms"] == 0 or ms < row["first_ms"]):
            row["first_ms"] = ms
            row["first"] = str(event.get("t") or "")
        if ms > row["last_ms"]:
            row["last_ms"] = ms
            row["last"] = str(event.get("t") or "")
        kind = str(event.get("k") or "")
        if kind == "session":
            row["sessions"] += 1
        elif kind == "tool":
            row["calls"] += 1
            row["last_tool"] = str(event.get("tool") or row["last_tool"])
            if ms >= now_ms - 86_400_000:
                row["calls_24h"] += 1
        elif kind == "list":
            row["lists"] += 1
        if event.get("ok") is False:
            row["errors"] += 1
            if ms >= now_ms - 86_400_000:
                row["errors_24h"] += 1
        if not row["ua"] and event.get("ua"):
            row["ua"] = str(event.get("ua"))
    rows = list(groups.values())
    for row in rows:
        row["active"] = bool(row["last_ms"] >= now_ms - 5 * 60_000)
    rows.sort(key=lambda item: item["last_ms"], reverse=True)
    return rows


async def mcp_service(request: Request) -> JSONResponse:
    now_ms = int(time.time() * 1000)
    events, scan = load_events()
    clients = _client_rows(events, now_ms)
    calls_24h = sum(1 for e in events if e.get("k") == "tool" and int(e.get("ms") or 0) >= now_ms - 86_400_000)
    errors_24h = sum(
        1
        for e in events
        if e.get("ok") is False and int(e.get("ms") or 0) >= now_ms - 86_400_000
    )
    sessions_24h = sum(
        1 for e in events if e.get("k") == "session" and int(e.get("ms") or 0) >= now_ms - 86_400_000
    )
    tools = 0
    if _SERVER is not None:
        try:
            tools = len(await _SERVER.list_tools(run_middleware=False))
        except Exception:
            tools = 0
    limit = _limit(request, default=200, cap=500)
    newest = list(reversed(events))[:limit]
    return JSONResponse({
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "server": {
            "endpoint": str(request.base_url).rstrip("/") + "/mcp",
            "transport": "streamable HTTP",
            "auth": "x-api-key",
            "require_key": REQUIRE_KEY,
            "tools": tools,
            "env": SERVICE_ENV,
            "db": DB_LABEL,
        },
        "summary": {
            "calls_24h": calls_24h,
            "errors_24h": errors_24h,
            "sessions_24h": sessions_24h,
            "clients": len(clients),
            "active": sum(1 for row in clients if row["active"]),
            "events": scan["kept"],
        },
        "clients": clients,
        "events": newest,
        "scan": scan,
        "audit": audit_info(),
    })


# ── 注册 ────────────────────────────────────────────────────────────────────
def _page(_: Request) -> HTMLResponse:
    return HTMLResponse(_html())


def register_console(mcp) -> None:
    """把控制台挂到服务本体的同源路径上（不走 DSH、不另起端口）。"""
    global _SERVER
    _SERVER = mcp
    mcp.custom_route("/console", methods=["GET"])(_page)
    mcp.custom_route("/console/api/overview", methods=["GET"])(overview)
    mcp.custom_route("/console/api/convs", methods=["GET"])(convs)
    mcp.custom_route("/console/api/conv", methods=["GET"])(conv_detail)
    mcp.custom_route("/console/api/ctx", methods=["GET"])(ctx_list)
    mcp.custom_route("/console/api/ctx/detail", methods=["GET"])(ctx_detail)
    mcp.custom_route("/console/api/gate", methods=["GET"])(gate_log)
    mcp.custom_route("/console/api/docs", methods=["GET"])(docs)
    mcp.custom_route("/console/api/doc", methods=["GET"])(doc_detail)
    mcp.custom_route("/console/api/quiz", methods=["GET"])(quiz)
    mcp.custom_route("/console/api/mcp", methods=["GET"])(mcp_service)
