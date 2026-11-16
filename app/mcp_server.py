from __future__ import annotations

import asyncio
import re
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import service
from .config import REQUIRE_KEY
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
def search(query: str, limit: int = 10, scope: str = "auto") -> dict[str, Any]:
    """Search personal memory (scope=personal), public CMS/X (scope=public), or both (auto)."""
    try:
        user = _need_user()
        return service.search(query, user["id"], limit=limit, scope=scope)
    except Exception as e:
        return {"error": str(e), "results": []}


@mcp.tool
def remember(text: str, kind: str = "event", title: str = "", topics: str = "") -> dict[str, Any]:
    """Save one memory/decision for the current user. Returns the new path/id."""
    try:
        user = _need_user()
        return service.remember(user["id"], text, kind=kind, title=title, topics=topics)
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


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "zhishu-memory", "require_key": REQUIRE_KEY})


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
    """List conversations by time window (since/until: 2h, today, ISO); newest first."""
    try:
        return service.conv_list(since, until, source, limit)
    except Exception as e:
        return {"error": str(e), "conversations": []}


@mcp.tool
def conv_get(conv_id: str, since: str = "", until: str = "", limit: int = 100,
             offset: int = 0) -> dict[str, Any]:
    """Read a conversation's messages from omnihist by id and optional time window."""
    try:
        return service.conv_get(conv_id, since, until, limit, offset)
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
         role: str = "user", limit: int = 5, turn: int | None = None) -> dict[str, Any]:
    """Blocking pre-answer gate: capture the prompt and return retrieved context."""
    try:
        user = _need_user()
        return service.gate(query, user["id"], source, conversation_id, role, limit, turn)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.custom_route("/gate", methods=["POST"])
async def http_gate(request: Request) -> JSONResponse:
    user = service.resolve_user(request.headers.get("x-api-key"))
    if not user:
        return JSONResponse({"ok": False, "error": "invalid or missing api key"}, status_code=401)
    body = await request.json()
    try:
        return JSONResponse(await asyncio.to_thread(
            service.gate,
            str(body.get("query", "")), user["id"],
            str(body.get("source", "gate")), str(body.get("conversation_id", "")),
            str(body.get("role", "user")), int(body.get("limit", 5)),
            int(body["turn"]) if body.get("turn") not in (None, "") else None,
        ))
    except Exception as e:
        return _fail(e)


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
