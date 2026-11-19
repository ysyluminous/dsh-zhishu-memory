from __future__ import annotations

import asyncio
import re
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import service
from .config import DEFAULT_USER, REQUIRE_KEY, SERVICE_ENV, DB_LABEL
from . import growth_log as growth
from . import keys as keys_svc

mcp = FastMCP(
    "zhishu-memory",
    instructions=(
        "Personal memory and public knowledge for zhishu. Use `search` with scope=personal for "
        "the user's own decisions/conversations, scope=public for CMS/X/coord data. Write with "
        "`remember`; never invent counts. Every call is isolated by the API key's user_id."
    ),
)

# 谁连上来了、调了什么：写本机 JSONL，不依赖库（库挂了时最需要看它）。
# 控制台「MCP 服务」页读同一份文件。
from .mcp_audit import McpAuditMiddleware  # noqa: E402

mcp.add_middleware(McpAuditMiddleware())



# ── 失败分类（2026-11-16 P0 复盘）：库不可用曾被统一包成 400，客户端只会当
#    「请求写错了」；当天 9 条写回被误判成 FAIL 就是这么来的。按类型分档：
#    连接类 → 503 可重试；约束冲突 → 409；其余 → 400。
_RETRYABLE_ERRORS: tuple[type[BaseException], ...] = ()
try:  # psycopg 版本差异：ConnectionTimeout 可能不存在
    from psycopg import InterfaceError as _PgInterfaceError
    from psycopg import OperationalError as _PgOperationalError

    _RETRYABLE_ERRORS = (_PgOperationalError, _PgInterfaceError)
    try:
        from psycopg import ConnectionTimeout as _PgConnectionTimeout

        _RETRYABLE_ERRORS = (*_RETRYABLE_ERRORS, _PgConnectionTimeout)
    except ImportError:
        pass
    from psycopg import IntegrityError as _PgIntegrityError
except ImportError:  # pragma: no cover
    _PgIntegrityError = None  # type: ignore[assignment]

_LEAK_PATTERNS = (
    re.compile(r"(postgres(?:ql)?://)\S+"),
    re.compile(r"@[\w.\-]+:\d+"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"),
)


def _scrub(msg: str) -> str:
    """兜底脱敏：错误串里不许出现连接串、主机名、IP。

    503 分支已不回传任何异常文本；这里是其余异常走 400 时的兜底。
    """
    for pat in _LEAK_PATTERNS:
        msg = pat.sub("[redacted]", msg)
    return msg


def _fail(exc: BaseException, status_code: int = 400) -> JSONResponse:
    """唯一的失败出口：不把 DSN / 主机名 / IP 带进响应体。"""
    if _RETRYABLE_ERRORS and isinstance(exc, _RETRYABLE_ERRORS):
        return JSONResponse(
            {"ok": False, "error": "db_unavailable", "retryable": True},
            status_code=503,
        )
    if _PgIntegrityError is not None and isinstance(exc, _PgIntegrityError):
        return JSONResponse(
            {"ok": False, "error": "conflict", "retryable": False},
            status_code=409,
        )
    return JSONResponse(
        {"ok": False, "error": _scrub(str(exc)), "retryable": False},
        status_code=status_code,
    )


def _key_from_headers() -> str | None:
    try:
        headers = get_http_headers()
        if not headers:
            return None
        for name in ("x-api-key", "X-API-Key"):
            if headers.get(name):
                return headers.get(name)
    except Exception:
        return None
    return None


def _user() -> dict[str, str] | None:
    return service.resolve_user(_key_from_headers())


def _need_user() -> dict[str, str]:
    user = _user()
    if not user:
        raise PermissionError("无效或缺少 API key")
    return user


@mcp.tool
def search(query: str, limit: int = 10, scope: str = "auto", include_pending: bool = False,
           include_conversations: bool = True) -> dict[str, Any]:
    """Search personal memory (scope=personal), public CMS/X (scope=public), or both (auto).

    v1 review-gated: 默认只搜「已通过审核」的正式区；待审区要显式 include_pending=True 才看得到。
    返回 results（type=doc|conversation）与 conversation_hits（消息级命中，带 session/seq/at/anchor）。
    """
    try:
        user = _need_user()
        return service.search(query, user["id"], limit=limit, scope=scope,
                              include_pending=include_pending,
                              include_conversations=include_conversations)
    except Exception as e:
        return {"error": str(e), "results": []}


@mcp.tool
def remember(text: str, kind: str = "event", title: str = "", topics: str = "",
             source: str = "", anchor: str = "", supersedes: str = "",
             force_new: bool = False) -> dict[str, Any]:
    """Save one memory/decision for the current user. Returns the new path/id.

    写入前先做 Observation 判定（2026-11-18，账号主定），返回的 observation.action 就是处置：
      create   新事项 → 新建一行
      evidence 同一条事实 → 只补 evidence，不新增行
      update   更正/状态变化 → 新建一行并把旧行置 superseded（supersedes 显式传，或正文带「改成/更正/推翻」自动判）
      noop     没有值得补充的内容 → 保持不变
    anchor 写来源锚点（文件:行号 / session id / 命令原文）；source 写来源类型（session/file/url）。
    """
    try:
        user = _need_user()
        return service.remember(user["id"], text, kind=kind, title=title, topics=topics,
                                source=source, anchor=anchor, supersedes=supersedes,
                                force_new=force_new)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def stats() -> dict[str, Any]:
    """Per-user document/conversation counts, tools used, and kinds. Unknown metrics are null."""
    try:
        user = _need_user()
        return service.stats(user["id"])
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
def whoami() -> dict[str, Any]:
    """Return the user resolved from the API key."""
    try:
        user = _need_user()
        return service.whoami(user["id"])
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
def help(topic: str = "") -> dict[str, Any]:
    """工具选型引导：不知道用 search / conv_search / conv_get 哪个时先调它。

    topic 可选 search（会话检索链路）、conversation、write、review；留空返回全量。
    """
    try:
        return service.help_text(topic)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def capabilities() -> dict[str, Any]:
    """返回服务能力清单（工具、检索链路、身份口径），给客户端做选型。"""
    try:
        return service.capabilities()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "zhishu-memory", "require_key": REQUIRE_KEY})


# 只读控制台：同源挂在服务端口（/console + /console/api/*）。
# 2026-11-17 账号主定：控制台是服务自带的独立 HTML 页，不嵌 DSH、不另起端口。
from .console import register_console  # noqa: E402  （放在 mcp 实例之后，避免循环导入）

register_console(mcp)

# 上下文编排接口（/context/plan）：和 /gate 是两件事——gate 往外找记忆，这个往内裁历史。
from .context_plan import register_context  # noqa: E402

register_context(mcp)


@mcp.custom_route("/search", methods=["GET"])
async def http_search(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    q = request.query_params.get("q", "")
    scope = request.query_params.get("scope", "auto")
    limit = int(request.query_params.get("limit", "10"))
    return JSONResponse(await asyncio.to_thread(service.search, q, user["id"], limit, scope))


@mcp.custom_route("/remember", methods=["POST"])
async def http_remember(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    try:
        result = await asyncio.to_thread(
            service.remember,
            user["id"],
            str(body.get("text", "")),
            str(body.get("kind", "event")),
            str(body.get("title", "")),
            str(body.get("topics", "")),
            # Observation 写入判定要用的四个入参（2026-11-18）：
            # 漏传 source/anchor 会让「补证据」永远判成 noop，漏传 supersedes 会把「更正」降级成「补证据」。
            str(body.get("source", "")),
            str(body.get("anchor", "")),
            str(body.get("supersedes", "")),
            bool(body.get("force_new", False)),
            str(body.get("idem_key", "")),
            str(body.get("conversation_id", "")),
        )
        return JSONResponse(result)
    except Exception as e:
        return _fail(e)


@mcp.custom_route("/stats", methods=["GET"])
async def http_stats(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(service.stats, user["id"]))


@mcp.custom_route("/whoami", methods=["GET"])
async def http_whoami(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(service.whoami, user["id"]))


@mcp.tool
def idea_add(title: str, body: str = "", priority: int = 2, parent_id: str = "") -> dict[str, Any]:
    """Record an idea for the current user (status=open)."""
    try:
        return service.idea_add(_need_user()["id"], title, body, priority, parent_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def idea_list(status: str = "", limit: int = 50) -> dict[str, Any]:
    """List ideas, optionally by status (open/planning/planned/executing/review/done)."""
    try:
        return service.idea_list(_need_user()["id"], status, limit)
    except Exception as e:
        return {"error": str(e), "ideas": []}


@mcp.tool
def idea_breakdown(idea_id: str, steps: list[str]) -> dict[str, Any]:
    """Append breakdown steps to an idea and move it to planned."""
    try:
        return service.idea_breakdown(_need_user()["id"], idea_id, steps)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def idea_decide(idea_id: str, decision: str, status: str = "planned") -> dict[str, Any]:
    """Record the user's decision for an idea; status planned/executing/review/done."""
    try:
        return service.idea_decide(_need_user()["id"], idea_id, decision, status)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def idea_update(idea_id: str, status: str) -> dict[str, Any]:
    """Move an idea to another status."""
    try:
        return service.idea_update(_need_user()["id"], idea_id, status)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def idea_steps(idea_id: str) -> dict[str, Any]:
    """List breakdown steps for one idea."""
    try:
        return service.idea_steps(_need_user()["id"], idea_id)
    except Exception as e:
        return {"error": str(e), "steps": []}


@mcp.tool
def idea_to_todos(idea_id: str) -> dict[str, Any]:
    """Turn pending idea steps into todos."""
    try:
        return service.idea_to_todos(_need_user()["id"], idea_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def todo_add(title: str, detail: str = "", due_at: str = "", priority: int = 2, project: str = "", source: str = "", owner: str = "human") -> dict[str, Any]:
    """Add a todo. due_at is ISO time, e.g. 2026-11-14T09:00:00+08:00.

    owner: human=账号主本人（默认），agent=某个 bot 名。扫描器入池用 agent。
    """
    try:
        return service.todo_add(_need_user()["id"], title, detail, due_at, priority, project, source, owner)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def todo_list(status: str = "", limit: int = 50) -> dict[str, Any]:
    """List todos, optionally by status (pending/doing/done/canceled)."""
    try:
        return service.todo_list(_need_user()["id"], status, limit)
    except Exception as e:
        return {"error": str(e), "todos": []}


@mcp.tool
def todo_update(todo_id: str, status: str = "", title: str = "", detail: str = "",
                due_at: str = "", priority: int | None = None, project: str = "") -> dict[str, Any]:
    """Update a todo: status/title/detail/due_at/priority/project."""
    try:
        return service.todo_update(_need_user()["id"], todo_id, status, title, detail, due_at, priority, project)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def ledger_add(amount: float, note: str = "", category: str = "", occurred_at: str = "",
               merchant: str = "", currency: str = "CNY", source: str = "") -> dict[str, Any]:
    """Record an expense/income. amount can be negative for refunds; occurred_at is ISO time."""
    try:
        return service.ledger_add(_need_user()["id"], amount, note, category, occurred_at, merchant, currency, source)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def ledger_list(since: str = "", until: str = "", limit: int = 50) -> dict[str, Any]:
    """List ledger entries, newest first."""
    try:
        return service.ledger_list(_need_user()["id"], since, until, limit)
    except Exception as e:
        return {"error": str(e), "entries": []}


@mcp.tool
def ledger_summary(period: str = "today") -> dict[str, Any]:
    """Sum expenses for today/month/all, with category breakdown."""
    try:
        return service.ledger_summary(_need_user()["id"], period)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
def related(node_id: str, depth: int = 1, limit: int = 20) -> dict[str, Any]:
    """Permission-aware related nodes (article/book/resource/personal doc)."""
    try:
        return service.related(_need_user()["id"], node_id, depth, limit)
    except Exception as e:
        return {"error": str(e), "related": []}


@mcp.custom_route("/related", methods=["GET"])
async def http_related(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    node_id = request.query_params.get("id", "")
    depth = int(request.query_params.get("depth", "1"))
    limit = int(request.query_params.get("limit", "20"))
    return JSONResponse(await asyncio.to_thread(service.related, user["id"], node_id, depth, limit))


@mcp.tool
def me_entitlements() -> dict[str, Any]:
    """Current user's plan, capabilities, quotas and expiry."""
    try:
        return service.entitlements(_need_user()["id"])
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
def check_access(content_type: str, content_id: str, region: str = "") -> dict[str, Any]:
    """Check whether the current user may view a content item; returns reason/upgrade."""
    try:
        return service.check_access(_need_user()["id"], content_type, content_id, region)
    except Exception as e:
        return {"allowed": False, "error": str(e)}


@mcp.tool
def consume(metric: str, amount: int = 1) -> dict[str, Any]:
    """Atomically consume one quota (search/ai_chat/download/article_read)."""
    try:
        return service.consume(_need_user()["id"], metric, amount)
    except Exception as e:
        return {"allowed": False, "error": str(e)}


@mcp.custom_route("/me/entitlements", methods=["GET"])
async def http_entitlements(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(service.entitlements, user["id"]))


@mcp.custom_route("/access/check", methods=["GET"])
async def http_access(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(
        service.check_access, user["id"],
        request.query_params.get("content_type", ""),
        request.query_params.get("content_id", ""),
        request.query_params.get("region", ""),
    ))


@mcp.custom_route("/usage/consume", methods=["POST"])
async def http_consume(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    return JSONResponse(await asyncio.to_thread(
        service.consume, user["id"], str(body.get("metric", "")), int(body.get("amount", 1)),
    ))


@mcp.tool
def queue_add(source_type: str, source_id: str, title: str, summary: str = "",
              priority: int = 2, tags: list[str] | None = None) -> dict[str, Any]:
    """Add one item to the unified ingestion queue (x_post/cms_resource/article/book/conversation/doc)."""
    try:
        return service.queue_add(_need_user()["id"], source_type, source_id, title, summary, priority, tags)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def queue_list(status: str = "", source_type: str = "", limit: int = 50) -> dict[str, Any]:
    """List queue items; status pending/processing/ready/failed/skipped/canceled."""
    try:
        return service.queue_list(_need_user()["id"], status, source_type, limit)
    except Exception as e:
        return {"error": str(e), "items": []}


@mcp.tool
def queue_update(item_id: str, status: str = "", priority: int | None = None) -> dict[str, Any]:
    """Change queue status or priority (1 highest, 5 lowest; cancel with status=canceled)."""
    try:
        return service.queue_update(_need_user()["id"], item_id, status, priority)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def queue_stats() -> dict[str, Any]:
    """Queue totals by status and source type."""
    try:
        return service.queue_stats(_need_user()["id"])
    except Exception as e:
        return {"error": str(e)}


@mcp.custom_route("/queue", methods=["GET"])
async def http_queue(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(
        service.queue_list, user["id"],
        request.query_params.get("status", ""),
        request.query_params.get("source_type", ""),
        int(request.query_params.get("limit", "50")),
    ))


@mcp.custom_route("/queue/add", methods=["POST"])
async def http_queue_add(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    try:
        return JSONResponse(await asyncio.to_thread(
            service.queue_add, user["id"], str(body.get("source_type", "")),
            str(body.get("source_id", "")), str(body.get("title", "")),
            str(body.get("summary", "")), int(body.get("priority", 2)),
            body.get("tags") or None,
        ))
    except Exception as e:
        return _fail(e)


@mcp.custom_route("/queue/update", methods=["POST"])
async def http_queue_update(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    priority = body.get("priority")
    try:
        return JSONResponse(await asyncio.to_thread(
            service.queue_update, user["id"], str(body.get("id", "")),
            str(body.get("status", "")), int(priority) if priority is not None else None,
        ))
    except Exception as e:
        return _fail(e)


@mcp.custom_route("/queue/stats", methods=["GET"])
async def http_queue_stats(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(service.queue_stats, user["id"]))


@mcp.tool
def prompt_list(source_type: str = "") -> dict[str, Any]:
    """List extraction prompt templates, optionally by source type."""
    try:
        return service.prompt_list(source_type)
    except Exception as e:
        return {"error": str(e), "prompts": []}


@mcp.tool
def prompt_get(key: str, version: int | None = None) -> dict[str, Any]:
    """Read one extraction prompt template (latest version by default)."""
    try:
        return service.prompt_get(key, version)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
def prompt_upsert(key: str, source_type: str, title: str, description: str, template: str,
                  output_schema: dict[str, Any] | None = None, model: str = "",
                  temperature: float = 0.2, enabled: bool = True) -> dict[str, Any]:
    """Publish a new prompt template version; the previous version is disabled when enabled."""
    try:
        return service.prompt_upsert(key, source_type, title, description, template,
                                     output_schema, model, temperature, enabled)
    except Exception as e:
        return {"error": str(e)}


@mcp.custom_route("/prompts", methods=["GET"])
async def http_prompts(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(
        service.prompt_list, request.query_params.get("source_type", "")))


@mcp.custom_route("/prompts/get", methods=["GET"])
async def http_prompt_get(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    version = request.query_params.get("version")
    try:
        return JSONResponse(await asyncio.to_thread(
            service.prompt_get, request.query_params.get("key", ""),
            int(version) if version else None))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@mcp.custom_route("/prompts/upsert", methods=["POST"])
async def http_prompt_upsert(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    try:
        return JSONResponse(await asyncio.to_thread(
            service.prompt_upsert, str(body.get("key", "")), str(body.get("source_type", "")),
            str(body.get("title", "")), str(body.get("description", "")),
            str(body.get("template", "")), body.get("output_schema") or {},
            str(body.get("model", "")), float(body.get("temperature", 0.2)),
            bool(body.get("enabled", True)),
        ))
    except Exception as e:
        return _fail(e)


@mcp.tool
def conv_list(since: str = "24h", until: str = "", source: str = "", limit: int = 50) -> dict[str, Any]:
    """List conversations by time window (since/until: 2h, today, ISO); newest first.

    每条带 last_synced_at / unreconciled / sync_status（live|reconciled）。
    """
    try:
        return service.conv_list(since, until, source, limit)
    except Exception as e:
        return {"error": str(e), "conversations": []}


@mcp.tool
def conv_search(keyword: str, conv_id: str = "", since: str = "", until: str = "",
                limit: int = 20, context: int = 1) -> dict[str, Any]:
    """Search inside conversation messages by keyword; returns seq/role/at/snippet/context/anchor.

    不知道在哪条会话时留空 conv_id；已知会话时传 conv_id 只搜这一条。
    context=命中前后各几条（0-3）。结果按 user_id 隔离，不跨用户。
    """
    try:
        user = _need_user()
        return service.conv_search(keyword, conv_id=conv_id, since=since, until=until,
                                   limit=limit, context=context, user_id=user["id"])
    except Exception as e:
        return {"error": str(e), "matches": [], "conversations": []}


@mcp.tool
def conv_get(conv_id: str, since: str = "", until: str = "", limit: int = 100,
             offset: int = 0, compact: bool = False, keyword: str = "",
             max_chars: int = 20000) -> dict[str, Any]:
    """Read a conversation's messages from omnihist by id and optional time window.

    compact=True 只回片段（先看轮廓）；keyword 在会话内先过滤；max_chars 控制单条上限。
    返回 last_synced_at / sync_status / 每条 anchor（session+seq+时间）。
    """
    try:
        return service.conv_get(conv_id, since, until, limit, offset,
                                compact=compact, keyword=keyword, max_chars=max_chars)
    except Exception as e:
        return {"error": str(e)}


@mcp.custom_route("/conversations", methods=["GET"])
async def http_conversations(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(
        service.conv_list,
        request.query_params.get("since", "24h"),
        request.query_params.get("until", ""),
        request.query_params.get("source", ""),
        int(request.query_params.get("limit", "50")),
    ))


@mcp.custom_route("/conversations/search", methods=["GET"])
async def http_conversation_search(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    try:
        return JSONResponse(await asyncio.to_thread(
            service.conv_search,
            request.query_params.get("q", ""),
            request.query_params.get("id", ""),
            request.query_params.get("since", ""),
            request.query_params.get("until", ""),
            int(request.query_params.get("limit", "20")),
            int(request.query_params.get("context", "1")),
            user["id"],
        ))
    except Exception as e:
        return _fail(e)


@mcp.custom_route("/conversations/get", methods=["GET"])
async def http_conversation_get(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"error": "invalid or missing api key"}, status_code=401)
    try:
        return JSONResponse(await asyncio.to_thread(
            service.conv_get,
            request.query_params.get("id", ""),
            request.query_params.get("since", ""),
            request.query_params.get("until", ""),
            int(request.query_params.get("limit", "100")),
            int(request.query_params.get("offset", "0")),
            request.query_params.get("compact", "").lower() in ("1", "true", "yes", "on"),
            request.query_params.get("keyword", ""),
            int(request.query_params.get("max_chars", "20000")),
        ))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@mcp.tool
def ingest_event(source: str, conversation_id: str, role: str, text: str,
                 title: str = "", seq: int | None = None, created_at: str = "",
                 event_id: str = "", meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Capture one turn boundary (user prompt or assistant answer) immediately."""
    try:
        return service.ingest_event(source, conversation_id, role, text, title,
                                    seq, created_at, event_id, meta)
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.custom_route("/ingest", methods=["POST"])
async def http_ingest(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    try:
        return JSONResponse(await asyncio.to_thread(
            service.ingest_event,
            str(body.get("source", "unknown")),
            str(body.get("conversation_id", "")),
            str(body.get("role", "user")),
            str(body.get("text", "")),
            str(body.get("title", "")),
            body.get("seq"),
            str(body.get("created_at", "")),
            str(body.get("event_id", "")),
            body.get("meta") or None,
        ))
    except Exception as e:
        return _fail(e)


@mcp.tool
def gate(query: str, source: str = "mcp", conversation_id: str = "",
         role: str = "user", limit: int = 5, turn: int | None = None,
         include_pending: bool = False) -> dict[str, Any]:
    """Blocking pre-answer gate: capture the prompt and return retrieved context.

    v1 review-gated: 默认不把待审区的内容注入（没审过的结论不该影响判断）。
    """
    try:
        user = _need_user()
        return service.gate(query, user["id"], source, conversation_id, role, limit, turn, include_pending)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _gate_error_response(e: Exception) -> JSONResponse:
    """把 gate 失败变成**可区分**的结构化错误（2026-11-18）。

    客户端 10s abort 之后服务端还在跑，插件只看到 timeout，分不清是「库连不上」还是
    「库慢」还是「我们自己的 bug」。这里按异常类型给 code，插件与 mem-health 可以据此分类：
      gate_db_timeout      → 连库超时（psycopg ConnectionTimeout，connect_timeout=3s）
      gate_db_unavailable  → 库不可达/认证失败
      gate_internal        → 其它
    """
    name = type(e).__name__
    msg = str(e)
    low = msg.lower()
    if "ConnectionTimeout" in name or "connection timeout" in low:
        return JSONResponse({"ok": False, "error": {"code": "gate_db_timeout", "type": name,
                                                    "message": msg}}, status_code=503)
    if "OperationalError" in name or "could not connect" in low or "connection failed" in low:
        return JSONResponse({"ok": False, "error": {"code": "gate_db_unavailable", "type": name,
                                                    "message": msg}}, status_code=503)
    return JSONResponse({"ok": False, "error": {"code": "gate_internal", "type": name,
                                                "message": msg}}, status_code=500)


@mcp.custom_route("/gate", methods=["POST"])
async def http_gate(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    # log_sink：把 gate_log 行 id 从线程里带回来，客户端断开时补 client_gone 标记。
    # 不改 HTTP 响应体结构，插件无感。
    sink: dict[str, Any] = {}
    try:
        res = await asyncio.to_thread(
            service.gate,
            str(body.get("query", "")), user["id"],
            str(body.get("source", "gate")), str(body.get("conversation_id", "")),
            str(body.get("role", "user")), int(body.get("limit", 5)),
            int(body["turn"]) if body.get("turn") not in (None, "") else None,
            bool(body.get("include_pending", False)),
            sink,
        )
    except Exception as e:
        return _gate_error_response(e)
    log_id = int(sink.get("id") or 0)
    if log_id:
        try:
            # ASGI 不会因客户端断开立刻取消在跑的请求：跑完了才发现「客户端早走了」。
            # 这种行按客户端视角算失败，标 client_gone，不要混进成功里。
            if await request.is_disconnected():
                await asyncio.to_thread(service.mark_gate_client_gone, log_id)
        except Exception:
            pass
    return JSONResponse(res)


@mcp.custom_route("/gate/stats", methods=["POST"])
async def http_gate_stats(request: Request) -> JSONResponse:
    """gate 健康聚合（方案 C：POST + body）。

    body {} 或 {"hours": 24} →
    {window_hours,total,ok,failed,timeout,slow_ge_9500,p50_ms,p95_ms,p99_ms,max_ms,stage:{...}}
    """
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    body: dict[str, Any] = {}
    try:
        raw = await request.body()
        if raw:
            body = await request.json()
    except Exception:
        body = {}
    try:
        hours = int(body.get("hours", 24) or 24)
    except Exception:
        hours = 24
    try:
        return JSONResponse(await asyncio.to_thread(service.gate_stats, hours))
    except Exception as e:
        return _gate_error_response(e)


@mcp.tool
def growth_log(text: str, confirm: bool = False) -> dict[str, Any]:
    """One sentence into the time ledger. Returns a draft; confirm=true saves it.

    The draft carries `confidence` (high/low) and `review` hints; when confidence is
    low or needs_review=true, check the kind/time/amount with the user before confirming.
    """
    try:
        user = _need_user()
        return growth.growth_log(user["id"], text, confirm=confirm)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def growth_add(kind: str, title: str, amount: float | None = None,
               capital: str = "", start: str = "", end: str = "",
               status: str = "", note: str = "", confirm: bool = False) -> dict[str, Any]:
    """Save an already-parsed ledger entry — the caller does the language parsing, not this server.

    Use this instead of growth_log when the calling AI has already turned a sentence into
    structured fields; it needs no model endpoint and costs nothing. Fields:
    kind = task/event/habit/routine/sop/purchase/investment/review/sleep/meal,
    amount = number for money entries, capital = health/money/relation/growth,
    start/end = ISO time or HH:MM or YYYY-MM-DD, status = planned/done.
    Returns a draft unless confirm=true.
    """
    try:
        user = _need_user()
        return growth.growth_add(
            user["id"], kind=kind, title=title, amount=amount, capital=capital,
            start=start, end=end, status=status, note=note, confirm=confirm,
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.custom_route("/growth/add", methods=["POST"])
async def http_growth_add(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    try:
        return JSONResponse(await asyncio.to_thread(
            growth.growth_add, user["id"],
            kind=str(body.get("kind", "")), title=str(body.get("title", "")),
            amount=body.get("amount"), capital=str(body.get("capital", "")),
            start=str(body.get("start", "")), end=str(body.get("end", "")),
            status=str(body.get("status", "")), note=str(body.get("note", "")),
            currency=str(body.get("currency", "CNY")),
            confirm=bool(body.get("confirm", False)),
        ))
    except Exception as e:
        return _fail(e)


@mcp.tool
def growth_day(day: str = "") -> dict[str, Any]:
    """List the time ledger for one day (YYYY-MM-DD, default today)."""
    try:
        user = _need_user()
        return growth.growth_day(user["id"], day)
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}


@mcp.tool
def growth_parse(text: str) -> dict[str, Any]:
    """Dry-run the one-sentence parser: shows what would be recorded, writes nothing.

    Returns `confidence` (high/low) and `review` hints for ambiguous input.
    """
    try:
        _need_user()
        return growth.growth_parse(text)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.custom_route("/growth/parse", methods=["POST"])
async def http_growth_parse(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    try:
        return JSONResponse(await asyncio.to_thread(
            growth.growth_parse, str(body.get("text", "")),
        ))
    except Exception as e:
        return _fail(e)


@mcp.custom_route("/growth/log", methods=["POST"])
async def http_growth_log(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    try:
        return JSONResponse(await asyncio.to_thread(
            growth.growth_log,
            user["id"], str(body.get("text", "")), bool(body.get("confirm", False)),
        ))
    except Exception as e:
        return _fail(e)


@mcp.custom_route("/growth/day", methods=["GET"])
async def http_growth_day(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    try:
        return JSONResponse(await asyncio.to_thread(
            growth.growth_day,
            user["id"], request.query_params.get("date", ""),
            int(request.query_params.get("limit", "100")),
        ))
    except Exception as e:
        return _fail(e)


@mcp.tool
def growth_block_add(said: str = "", activity_path: str = "", start_at: str = "",
                     end_at: str = "", status: str = "实做", note: str = "",
                     tags: dict | None = None) -> dict[str, Any]:
    """记一个时间块（一句话一段，写 growth.activity_log，唯一真源）。

    activity_path 形如「运动>球类>羽毛球」，不存在会自动补全实体（上限 30 条）。
    start_at/end_at 用 ISO 时间；status 只能是 计划/实做/未记。
    """
    try:
        user = _need_user()
        return growth.growth_block_add(
            user["id"], said=said, activity_path=activity_path,
            start_at=start_at, end_at=end_at, status=status, note=note, tags=tags,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.custom_route("/growth/blocks", methods=["POST"])
async def http_growth_blocks(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    try:
        return JSONResponse(await asyncio.to_thread(
            growth.growth_block_add,
            user["id"],
            str(body.get("said", "")),
            str(body.get("activity_path", "")),
            str(body.get("start_at", "")),
            str(body.get("end_at", "")),
            str(body.get("status", "实做")),
            str(body.get("note", "")),
            body.get("tags") or None,
        ))
    except Exception as e:
        return _fail(e)


async def _session_or_error(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    who = await keys_svc.session_user(request)
    if "error" in who:
        # 本地模式（MEM_REQUIRE_KEY=0，DSH 插件自己托管的那份）：没有企业登录态，
        # 用默认账号管 key 就够了；一旦开了 REQUIRE_KEY，就必须走真会话。
        if not REQUIRE_KEY:
            return {"id": DEFAULT_USER, "label": DEFAULT_USER}, None
        return None, JSONResponse(
            {"ok": False, "error": who["error"]}, status_code=int(who.get("status", 401))
        )
    return who, None


@mcp.custom_route("/keys", methods=["GET"])
async def http_keys_list(request: Request) -> JSONResponse:
    who, error = await _session_or_error(request)
    if error:
        return error
    rows = await asyncio.to_thread(keys_svc.list_keys, who["id"])
    return JSONResponse({
        "ok": True,
        "user": {"id": who["id"], "label": who["label"]},
        "keys": rows,
        "max_keys": keys_svc.MAX_KEYS,
        "mcp_url": keys_svc.PUBLIC_MCP_URL,
    })


@mcp.custom_route("/keys", methods=["POST"])
async def http_keys_create(request: Request) -> JSONResponse:
    who, error = await _session_or_error(request)
    if error:
        return error
    body: dict[str, Any] = {}
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        body = {}
    try:
        created = await asyncio.to_thread(
            keys_svc.create_key, who["id"], str(body.get("name", ""))
        )
    except ValueError as exc:
        return _fail(exc)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, **created, "mcp_url": keys_svc.PUBLIC_MCP_URL}, status_code=201)


@mcp.custom_route("/keys/revoke", methods=["POST"])
async def http_keys_revoke(request: Request) -> JSONResponse:
    who, error = await _session_or_error(request)
    if error:
        return error
    body: dict[str, Any] = {}
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        body = {}
    removed = await asyncio.to_thread(
        keys_svc.revoke_key, who["id"], str(body.get("id", ""))
    )
    if not removed:
        return JSONResponse({"ok": False, "error": "没有找到这把 Key"}, status_code=404)
    return JSONResponse({"ok": True})


# 审核（review）**不在 MCP 面暴露**：agent 自己写、自己批 = 闸门形同虚设。
# 参考 memory-eternal 的做法：MCP 面对审核只读，批准只能走管理端 + 独立 admin token。
# 管理端：POST /admin/review（header: x-review-token: $MEM_REVIEW_TOKEN）
@mcp.custom_route("/admin/review", methods=["POST"])
async def http_admin_review(request: Request) -> JSONResponse:
    import hmac as _hmac
    import os as _os
    expected = (_os.getenv("MEM_REVIEW_TOKEN") or "").strip()
    got = (request.headers.get("x-review-token") or "").strip()
    if not expected or not got or not _hmac.compare_digest(got, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        doc_id = str(body.get("doc_id") or "").strip()
        if not doc_id:
            return JSONResponse({"ok": False, "error": "doc_id required"}, status_code=400)
        user = service.resolve_user(request.headers.get("x-api-key"))
        uid = user["id"] if user else (body.get("user_id") or "owner")
        res = await asyncio.to_thread(
            service.review, uid, doc_id,
            str(body.get("decision") or "pass"), str(body.get("reason") or ""), "admin",
        )
        return JSONResponse(res)
    except Exception as exc:  # 把栈带进响应体，别再留「偶发 500」过夜
        import traceback
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                             "trace": traceback.format_exc()[-1200:]}, status_code=500)


# 管理端：POST /admin/supersede（header: x-review-token: $MEM_REVIEW_TOKEN）
# 语义：把 superseded_id 从检索面隐藏（不是删除），并记一条 doc_history event=supersede。
@mcp.custom_route("/admin/supersede", methods=["POST"])
async def http_admin_supersede(request: Request) -> JSONResponse:
    import hmac as _hmac
    import os as _os
    expected = (_os.getenv("MEM_REVIEW_TOKEN") or "").strip()
    got = (request.headers.get("x-review-token") or "").strip()
    if not expected or not got or not _hmac.compare_digest(got, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        sid = str(body.get("superseded_id") or "").strip()
        by_id = str(body.get("by_id") or "").strip()
        if not sid or not by_id:
            return JSONResponse({"ok": False, "error": "superseded_id / by_id required"},
                                status_code=400)
        from . import supersede as _sup
        res = _sup.add(sid, by_id, str(body.get("reason") or ""), "admin")
        return JSONResponse(res)
    except Exception as exc:
        import traceback
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                             "trace": traceback.format_exc()[-1200:]}, status_code=500)


@mcp.tool
def pending() -> dict[str, Any]:
    """Pending backlog: how many memories wait for review and how old the oldest one is."""
    try:
        user = _need_user()
        return service.pending_summary(user["id"])
    except Exception as e:
        return {"error": str(e)}

@mcp.tool
def doc_history(doc_id: str, limit: int = 20) -> dict[str, Any]:
    """Read one document's change timeline (append-only JSONL + hash chain, v3 雏形)."""
    try:
        from . import history as _history
        return _history.for_doc(doc_id, limit)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
def history_verify() -> dict[str, Any]:
    """Verify the whole doc_history hash chain; returns first_bad when someone tampered with a line."""
    try:
        from . import history as _history
        return _history.verify()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool
def feedback(doc_id: str, useful: bool, note: str = "",
             relevance: int | None = None, completeness: int | None = None,
             freshness: int | None = None) -> dict[str, Any]:
    """Record whether a memory doc was useful (P3, append-only JSONL). Shows up as stats().feedback_7d.

    P2：除了 useful，还可选 0-5 分项评分 relevance（相关性）/ completeness（完整性）/ freshness（时效性）。
    """
    try:
        from . import quality as _quality
        return _quality.add_feedback(doc_id, useful, note,
                                     relevance=relevance, completeness=completeness,
                                     freshness=freshness)
    except Exception as e:
        return {"ok": False, "error": str(e)}
@mcp.tool
def core(doc_id: str = "", action: str = "list") -> dict[str, Any]:
    """Mark a memory as always-on in gate() (P5, zero-DDL JSONL registry).

    action='add' marks doc_id as core (max 20, least-recently-hit evicted);
    action='remove' unmarks; action='list' shows the current core set.
    """
    try:
        from . import core as _core
        if action == "list":
            return _core.listing()
        if action not in ("add", "remove"):
            return {"ok": False, "error": "action must be add/remove/list"}
        return _core.mark(doc_id, action == "add")
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def observation(obs_id: str = "", action: str = "list") -> dict[str, Any]:
    """Inspect the Observation store (2026-11-18): what is active, superseded, and why.

    action='list'  → 当前 active 的 Observation（含证据条数、何时更新）
    action='stats' → 总数 / active / superseded / 事件数
    action='get'   → 单条（含 status、evidence[]、supersedes、superseded_by），obs_id 必填
    action='history' → 单条的全部事件（什么时候建、补过几次证据、被谁取代），obs_id 必填
    """
    try:
        from . import observation as _obs
        user = _need_user()
        if action == "stats":
            return {"ok": True, **_obs.stats(user["id"])}
        if action in ("get", "history"):
            if not obs_id:
                return {"ok": False, "error": "obs_id 必填"}
            if action == "history":
                return {"ok": True, "obs_id": obs_id, "events": _obs.history(obs_id)}
            got = _obs.get(obs_id, user["id"])
            return {"ok": bool(got), "observation": got}
        rows = _obs.active(user["id"])
        rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return {"ok": True, "count": len(rows), "items": rows}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------------------------------------------------------------------
# 事项实体（2026-11-18）：HTTP 只读接口 + 四格工作台首页
# 口径见 app/issues.py 顶部；这里是薄薄的传输层，判定逻辑一律不放路由里。
# ---------------------------------------------------------------------------

@mcp.custom_route("/issues", methods=["GET"])
async def http_issues(request: Request) -> JSONResponse:
    try:
        from . import issues as _issues
        status = request.query_params.get("status", "active")
        project = request.query_params.get("project", "")
        limit = int(request.query_params.get("limit", 50))
        return JSONResponse(await asyncio.to_thread(_issues.list_issues, status, project, "", limit))
    except Exception as exc:
        return _fail(exc)


@mcp.custom_route("/issues/assign", methods=["POST"])
async def http_issue_assign(request: Request) -> JSONResponse:
    """干跑一次判定（不写任何东西），用来复现「这句话会被归到哪」。"""
    try:
        from . import issues as _issues
        body = await request.json()
        text = str(body.get("text") or "")
        kind = str(body.get("kind") or "event")
        return JSONResponse({"ok": True, "verdict": _issues.decide_issue(text, kind)})
    except Exception as exc:
        return _fail(exc)


@mcp.custom_route("/issues/link", methods=["POST"])
async def http_issue_link(request: Request) -> JSONResponse:
    try:
        from . import issues as _issues
        body = await request.json()
        res = _issues.link(str(body.get("issue_id") or ""), str(body.get("entity_id") or ""),
                           str(body.get("relation") or "belongs"),
                           str(body.get("method") or "manual"),
                           float(body.get("confidence") or 1.0))
        return JSONResponse(res)
    except Exception as exc:
        return _fail(exc)


@mcp.custom_route("/issues/report", methods=["GET"])
async def http_issue_report(request: Request) -> JSONResponse:
    try:
        from . import issues as _issues
        return JSONResponse({"ok": True, "report": await asyncio.to_thread(_issues.degrade_report)})
    except Exception as exc:
        return _fail(exc)


@mcp.custom_route("/todo/query", methods=["POST"])
async def http_todo_query(request: Request) -> JSONResponse:
    """待办只读查询（方案 C：POST + body）。给工作台与外部脚本共用，没有写能力。

    body: {"status":"pending","owner":"ai","priority":1,"project":"zhishu","limit":100}
    → {"ok":true,"count":N,"todos":[{id,title,status,priority,due_at,project,detail,owner,created_at}],
       "by_priority":{"p0":..,"p1":..,"p2":..}}
    数据来自 growth.time_entry（kind='task'），与 MCP todo_add/list 同一张表。
    """
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    body: dict[str, Any] = {}
    try:
        raw = await request.body()
        if raw:
            body = await request.json()
    except Exception:
        body = {}
    pr = body.get("priority")
    try:
        pr = int(pr) if pr not in (None, "") else None
    except Exception:
        pr = None
    try:
        res = await asyncio.to_thread(
            service.todo_query, user["id"],
            str(body.get("status") or ""), str(body.get("owner") or ""), pr,
            str(body.get("project") or ""), int(body.get("limit") or 100),
        )
        return JSONResponse({"ok": True, **res})
    except Exception as exc:
        return _fail(exc)


@mcp.custom_route("/workbench", methods=["GET"])
async def http_workbench(request: Request) -> JSONResponse:
    """首页就四格：汇集 → 抽取 → 关联 → 维护。点一格下钻列表，别一上来堆七八个列表。"""
    try:
        from . import issues as _issues
        data = await asyncio.to_thread(_issues.workbench)
        report = await asyncio.to_thread(_issues.degrade_report)
        issues = await asyncio.to_thread(_issues.list_issues, "active", "", "", 20)
        todos = await asyncio.to_thread(service.todo_query, "owner", "", "", None, "", 50)
        if (request.query_params.get("format") or "") == "json":
            return JSONResponse({"ok": True, "workbench": data,
                                 "report": report, "issues": issues, "todos": todos})
    except Exception as exc:
        return _fail(exc)

    c, e, l, m = data["collect"], data["extract"], data["link"], data["maintain"]
    tips = "".join(f"<li>{t}</li>" for t in report.get("tips") or [])
    rows = "".join(
        f"<tr><td>{i['title']}</td><td>{i.get('project') or '-'}</td>"
        f"<td>{i.get('status')}</td><td>{(i.get('updated_at') or '')[:19]}</td></tr>"
        for i in issues["items"]
    ) or "<tr><td colspan=4 class=muted>还没有事项</td></tr>"

    # 待办区块（2026-11-18 老大定：工作台必须看得到 todo，读 growth.time_entry，只读）
    t = todos or {"todos": [], "by_priority": {}}
    tp = t.get("by_priority") or {}
    trows = "".join(
        f"<tr><td>{x['id']}</td><td>{x['title']}</td><td>{x.get('project') or '-'}</td>"
        f"<td>{x.get('owner') or '-'}</td><td>{x.get('status')}</td>"
        f"<td>{(x.get('due_at') or '')[:10] or '-'}</td></tr>"
        for x in t["todos"]
    ) or "<tr><td colspan=6 class=muted>还没有待办</td></tr>"

    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>智枢工作台</title>
<style>
:root{{--bg:#f7f8fa;--card:#fff;--line:#e3e6eb;--ink:#1d2129;--ink2:#606770;--accent:#2f6fed;--warn:#b26a00;--good:#1f7a45}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
header{{padding:14px 20px;background:var(--card);border-bottom:1px solid var(--line)}}
h1{{font-size:16px;margin:0}}h1 small{{color:var(--ink2);font-weight:400;margin-left:8px}}
.funnel{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:16px 20px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.card .step{{color:var(--ink2);font-size:12px}}.card .num{{font-size:30px;font-weight:600;margin:4px 0}}
.card .sub{{color:var(--ink2);font-size:12px}}.card a{{color:var(--accent);text-decoration:none;font-size:12px}}
section{{padding:0 20px 20px}}.panel{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);font-size:13px}}
th{{color:var(--ink2);font-weight:500}}.muted{{color:var(--ink2)}}ul{{margin:6px 0 0 18px;padding:0}}
</style></head><body>
<header><h1>智枢工作台<small>四步漏斗 · 只读 · 数据来自 memory-mcp</small></h1></header>
<div class="funnel">
  <div class="card"><div class="step">① 汇集</div><div class="num">{c['entities']}</div>
    <div class="sub">{c['note']}</div><a href="/issues?status=all">下钻：全部实体/事项 →</a></div>
  <div class="card"><div class="step">② 抽取</div><div class="num">{e['kinds']['issues']}</div>
    <div class="sub">事项 {e['kinds']['issues']} · 关联 {e['kinds']['links']} · 观测 {e['kinds']['observations']}</div>
    <a href="/issues/report">下钻：退化检查 →</a></div>
  <div class="card"><div class="step">③ 关联</div><div class="num">{l['reuse'] + l['create']}</div>
    <div class="sub">复用 {l['reuse']} · 新建 {l['create']} · 待归并 {l['orphan'] + l['ambiguous']}</div>
    <a href="/issues?status=active">下钻：active 事项 →</a></div>
  <div class="card"><div class="step">④ 维护</div><div class="num">{m['pending']}</div>
    <div class="sub">待审 {m['pending']} · 归档事项 {m['archived_issues']} · 退化命中 {sum(m['degrade'].values())}</div>
    <a href="/issues/report">下钻：退化报告 →</a></div>
</div>
<section><div class="panel"><strong>待办（来自 growth.time_entry，只读）</strong>
<div class="sub">共 {t.get('count', 0)} 条 · 高优先 {tp.get('p0', 0)} · 中 {tp.get('p1', 0)} · 低 {tp.get('p2', 0)} · 查询端点 POST /todo/query</div>
<table><thead><tr><th>#</th><th>标题</th><th>项目</th><th>owner</th><th>状态</th><th>到期</th></tr></thead><tbody>{trows}</tbody></table></div></section>
<section><div class="panel"><strong>最近 active 事项</strong>
<table><thead><tr><th>事项</th><th>项目</th><th>状态</th><th>更新</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section><div class="panel"><strong>维护提示</strong><ul>{tips}</ul></div></section>
</body></html>"""
    from starlette.responses import HTMLResponse
    return HTMLResponse(html)
