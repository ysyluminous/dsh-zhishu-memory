"""服务端「上下文组装/压缩」接口：POST /context/plan

和 /gate 是两件事，别混：
- /gate    = 往外找记忆（检索注入），产出「该注入哪几条」；
- /context/plan = 往内裁历史（整理上下文），产出「该丢哪几轮、用什么摘要替换、省多少 token」。

第一段（今晚）只算不动：返回 plan，同时把决策写进 mem.ctx_trace 留痕。
真正的替换由 DSH 插件在 agent/pre-step 里做（第二段，开关 MEM_CTX_SWAP=1）。

trace 字段口径（2026-11-17 账号主定，改历史必须可复盘）：
- compaction.before_hash / after_hash：编排前、编排后 messages 的 sha256；
- compaction.dropped_turns：被丢弃的轮次序号 + 该轮首条消息下标 + 轮内消息数；
- compaction.tokens_before / tokens_after / tokens_saved；
- compaction.summary_source：摘要来自哪（stub / llm / manual）；
- compaction.trigger：pressure（压力阈值）/ overflow（溢出兜底）/ manual；
- payload：完整决策（预算、阈值、保留轮数、每一轮的角色与字符数）。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from starlette.requests import Request
# 统一序列化出口：ctx_pct 是 numeric，psycopg 取回来是 Decimal，
# 用 starlette 原生 JSONResponse 会 500（app/json_out.py）。
from .json_out import SafeJSONResponse as JSONResponse

from .config import DEFAULT_USER, MEM_DSN
from .service import _conn

# token 估算口径：中文约 1.7 字符/token。全链路统一用这一个式子，
# 别在插件和控制台各写一套，否则两边数对不上就会被当成「压没压」的假差异。
CHARS_PER_TOKEN = 1.7


def est_tokens(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def _msg_chars(m: dict[str, Any]) -> int:
    content = m.get("content")
    if isinstance(content, str):
        return len(content)
    if content is None:
        return 0
    try:
        return len(json.dumps(content, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(content))


def _digest(messages: list[dict[str, Any]]) -> str:
    blob = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(blob.encode("utf-8"))


def sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def split_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 user 消息切轮；system/developer 前缀不归属任何轮，永远不丢。

    返回结构：[{kind: 'system'|'turn', indices: [消息下标...], first_idx, role}]
    """
    units: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for i, m in enumerate(messages):
        role = str(m.get("role") or "").lower()
        if role in ("system", "developer"):
            if cur:
                units.append(cur)
                cur = None
            units.append({"kind": "system", "indices": [i], "first_idx": i})
            continue
        if role == "user":
            if cur:
                units.append(cur)
            cur = {"kind": "turn", "indices": [i], "first_idx": i, "role": role}
        else:
            if cur is None:
                # 会话不是以 user 开头（罕见）：这一串挂成独立单元，按可丢处理
                cur = {"kind": "turn", "indices": [i], "first_idx": i, "role": role}
            else:
                cur["indices"].append(i)
    if cur:
        units.append(cur)
    return units


def _unit_chars(messages: list[dict[str, Any]], unit: dict[str, Any]) -> int:
    return sum(_msg_chars(messages[i]) for i in unit["indices"])


def plan(
    messages: list[dict[str, Any]],
    budget_tokens: int,
    keep_recent_turns: int = 6,
    threshold: float = 0.70,
    target: float = 0.55,
    min_drop_turns: int = 2,
    trigger: str = "pressure",
    pressure_override: float | None = None,
    pressure_source: str | None = None,
) -> dict[str, Any]:
    """算出「该丢哪几轮」，只算不动。

    保守规则（第一版，宁可少丢）：
    - system/developer 永远不丢；
    - 第一轮（锚点，含最初的任务交代）永远不丢；
    - 最近 keep_recent_turns 轮永远不丢；
    - 压力低于 threshold 时建议 keep；
    - 压力高时从最旧可丢轮开始丢，直到降到 target 以下，且至少丢 min_drop_turns 轮。

    压力口径（2026-11-17 账号主定）：判定「要不要压」只用宿主 tokenMeter 给的
    projectedTokens / contextWindow（`pressure_override`）；本模块按 1.7 字符/token
    估出来的数只用于「选哪几段丢」的相对排序，两者差约 2.35 倍，绝不能互相比较。
    """
    units = split_turns(messages)
    total_chars = sum(_msg_chars(m) for m in messages)

    turn_units = [u for u in units if u["kind"] == "turn"]
    protected = set()
    if turn_units:
        protected.add(turn_units[0]["first_idx"])  # 第一轮锚点
    for u in turn_units[-keep_recent_turns:] if keep_recent_turns > 0 else []:
        protected.add(u["first_idx"])

    # 可丢轮，按时间从旧到新
    droppable = [u for u in turn_units if u["first_idx"] not in protected]

    # 重要性保护：命中决策/结论类词的轮，最多保护可丢轮的 40%（按命中数排）。
    # 不设上限就会「什么都不肯丢」；设了上限才既保关键、又能真降压力。
    high_value: list[dict[str, Any]] = []
    if droppable:
        scored = [(_importance(messages, u), u) for u in droppable]
        scored = [(s, u) for s, u in scored if s >= 2]
        scored.sort(key=lambda item: item[0], reverse=True)
        cap = max(1, int(len(droppable) * 0.4))
        high_value = [u for _, u in scored[:cap]]
        keep_set = {u["first_idx"] for u in high_value}
        droppable = [u for u in droppable if u["first_idx"] not in keep_set]

    tokens_before_est = est_tokens(total_chars)
    pressure_est = (tokens_before_est / budget_tokens) if budget_tokens > 0 else 0.0
    # 判定口径：有宿主给的实测占比就用它，没有才退回自己的估算并显式标注。
    # 来源字符串由调用方给（dsh-tokenmeter:pressureTokens 这种），要能分清是哪个字段。
    pressure = pressure_override if pressure_override is not None else pressure_est
    pressure_source = (
        pressure_source if pressure_override is not None and pressure_source
        else ("dsh-tokenmeter" if pressure_override is not None else "est:chars/1.7")
    )
    action = "keep"
    reason = f"pressure {pressure*100:.1f}% < threshold {threshold*100:.0f}%，不丢"

    dropped: list[dict[str, Any]] = []
    if budget_tokens > 0 and pressure >= threshold and droppable:
        remaining = total_chars
        idx = 0
        # 丢到哪：用「比例」算，不拿两套 token 口径互相比较。
        # 判定口径是 pressure（可能来自 DSH），target 是同一口径的目标，
        # 两者之比就是该保留的字符比例——token 与字符线性相关，所以直接乘字符总数即可。
        # 2026-11-17 踩过：这里原来用 est_tokens(remaining)/budget 去比 target，
        # 结果 pressure=75% 触发后，est=47% 已经低于 target 55%，while 一次不进、一轮没丢。
        target_chars = total_chars * (target / pressure) if pressure > 0 else 0
        while idx < len(droppable) and remaining > target_chars:
            u = droppable[idx]
            chars = _unit_chars(messages, u)
            dropped.append({
                "turn": idx + 1,                        # 可丢序列里的第几轮（1 起）
                "first_idx": u["first_idx"],            # 在原 messages 里的下标
                "msg_count": len(u["indices"]),
                "indices": u["indices"],
                "chars": chars,
                "tokens_est": est_tokens(chars),
                "head": _head(messages[u["first_idx"]]),
            })
            remaining -= chars
            idx += 1
        if dropped:
            action = "drop"
            reason = (f"pressure {pressure*100:.1f}% ≥ threshold {threshold*100:.0f}%，"
                      f"丢弃最旧 {len(dropped)} 轮以回到 {target*100:.0f}% 以下")
        else:
            reason = "压力高但没有可丢的轮（都被保护），保持原样"

    tokens_after_est = max(0, tokens_before_est - sum(d["tokens_est"] for d in dropped))
    summary = _build_summary(messages, dropped)
    plan_body = {
        "action": action,
        "trigger": trigger,
        "reason": reason,
        "budget_tokens": budget_tokens,
        "threshold": threshold,
        "target": target,
        "keep_recent_turns": keep_recent_turns,
        # 判定口径：这是唯一可以拿来和 threshold 比较的数。
        "pressure_pct": round(pressure * 100, 3),
        "pressure_source": pressure_source,
        # 参考口径：仅用于选段和「省了多少」的相对大小，和上面差约 2.35 倍。
        "pressure_pct_est": round(pressure_est * 100, 3),
        "tokens_before": tokens_before_est,
        "tokens_after": tokens_after_est,
        "tokens_saved": tokens_before_est - tokens_after_est,
        "tokens_source": "est:chars/1.7",
        "chars_before": total_chars,
        "dropped_turns": dropped,
        "keep_units": [
            {"kind": u["kind"], "first_idx": u["first_idx"], "indices": u["indices"]}
            for u in units
            if u["kind"] == "system" or u["first_idx"] == (turn_units[0]["first_idx"] if turn_units else -1)
            or u["first_idx"] in protected
        ],
        "summary": summary,
    }
    return plan_body


def _head(m: dict[str, Any], size: int = 120) -> str:
    content = m.get("content")
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    text = " ".join((text or "").split())
    return text[:size]


# 轮次重要性：命中的词越多，这轮越可能是「决策/结论/口径」而不是过程。
# 「只按时间裁」的缺陷就是会把这类轮当最旧的删掉，所以它必须进保护名单。
_PROTECT_PATTERN = re.compile(
    r"(结论|决定|定死|否决|批准|唯一真源|验收|根因|口径|截止|依赖|红线|不许|必须|规则|已定|拍板)"
)


def _importance(messages: list[dict[str, Any]], unit: dict[str, Any]) -> int:
    text = " ".join(str(messages[i].get("content") or "") for i in unit["indices"])
    return len(_PROTECT_PATTERN.findall(text))


def _build_summary(messages: list[dict[str, Any]], dropped: list[dict[str, Any]]) -> dict[str, Any]:
    """第一版只给 stub 摘要：让「替换成什么」有具体内容可看，但明确标非 LLM 生成。

    第二段接 LLM 摘要后，source 变成 'llm:<model>'，text 换成真摘要；调用方按 source
    判断能不能真用来替换（stub 只用于观测，不许上线替换）。
    """
    if not dropped:
        return {"needed": False, "text": "", "source": "none", "chars": 0}
    lines = []
    for d in dropped:
        lines.append(f"[第 {d['turn']} 轮·{d['msg_count']} 条·≈{d['tokens_est']} tok] {d['head']}")
    text = ("[已压缩的早期对话·stub，未接入 LLM 摘要]\n" + "\n".join(lines))
    return {
        "needed": True,
        "text": text,
        "chars": len(text),
        "source": "stub",
        "replaces_turns": [d["turn"] for d in dropped],
    }


# ── 落痕 ─────────────────────────────────────────────────────────────────────
def write_trace(
    user_id: str,
    session_id: str,
    bot: str,
    turn: int | None,
    step: int | None,
    model: str,
    kind: str,
    plan_body: dict[str, Any],
    messages: list[dict[str, Any]],
    latency_ms: int,
    ok: bool = True,
    error: str = "",
    payload_extra: dict[str, Any] | None = None,
) -> int:
    """把一次编排决策写进 ctx_trace，返回 trace id。

    留痕优先：这一步自身失败不能让整个 pre-step 失败（best-effort），
    但失败会被调用方看到（返回 -1）。
    """
    before_hash = _digest(messages)
    dropped = plan_body.get("dropped_turns") or []
    after_hash = before_hash  # 第一段不替换；第二段由调用方传入实际 after messages 的 hash
    compaction = {
        "before_hash": before_hash,
        "after_hash": after_hash,
        "swapped": False,
        "trigger": plan_body.get("trigger") or "pressure",
        "action": plan_body.get("action"),
        "tokens_before": plan_body.get("tokens_before"),
        "tokens_after": plan_body.get("tokens_after"),
        "tokens_saved": plan_body.get("tokens_saved"),
        "dropped_turns": [
            {"turn": d["turn"], "first_idx": d["first_idx"],
             "msg_count": d["msg_count"], "tokens_est": d["tokens_est"]}
            for d in dropped
        ],
        "summary_source": (plan_body.get("summary") or {}).get("source"),
        "summary_chars": (plan_body.get("summary") or {}).get("chars"),
        "reason": plan_body.get("reason"),
    }
    payload = {
        "budget_tokens": plan_body.get("budget_tokens"),
        "pressure_pct": plan_body.get("pressure_pct"),
        # 两个口径都留，字段名按账号主 2026-11-17 定：ctx_pct_dsh 判定用，
        # ctx_pct_est 只供选段参考，两者差约 2.35 倍，不许互相比较。
        "ctx_pct_dsh": plan_body.get("pressure_pct"),
        "ctx_pct_est": plan_body.get("pressure_pct_est"),
        "pressure_pct_est": plan_body.get("pressure_pct_est"),
        "pressure_source": plan_body.get("pressure_source"),
        "keep_recent_turns": plan_body.get("keep_recent_turns"),
        "coverage": plan_body.get("coverage"),
        "message_count": len(messages),
        "roles": [str(m.get("role") or "") for m in messages],
        "chars_by_msg": [_msg_chars(m) for m in messages],
    }
    if payload_extra:
        payload.update(payload_extra)
    try:
        with _conn(MEM_DSN) as c:
            with c.cursor() as cur:
                cur.execute(
                    """insert into mem.ctx_trace
                       (user_id, session_id, bot, turn, step, model, kind,
                        history_chars, history_tokens, ctx_tokens, ctx_limit, ctx_pct,
                        compaction, payload, latency_ms, ok, error)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)
                       returning id""",
                    (
                        user_id, session_id or "", bot or "", turn, step, model or "", kind,
                        int(plan_body.get("chars_before") or 0),
                        int(plan_body.get("tokens_before") or 0),
                        int(plan_body.get("tokens_after") or 0),
                        int(plan_body.get("budget_tokens") or 0) or None,
                        float(plan_body.get("pressure_pct") or 0.0),
                        json.dumps(compaction, ensure_ascii=False),
                        json.dumps(payload, ensure_ascii=False),
                        int(latency_ms), bool(ok), (error or "")[:500],
                    ),
                )
                trace_id = cur.fetchone()[0]
            c.commit()
        return int(trace_id)
    except Exception:
        return -1


# ── HTTP ─────────────────────────────────────────────────────────────────────
async def http_context_plan(request: Request) -> JSONResponse:
    """POST /context/plan

    入参：{"messages": [...], "budget_tokens": 128000, "conversation_id": "...",
           "session_id": "...", "bot": "...", "turn": 3, "step": 1, "model": "..."}
    出参：{"ok": true, "trace_id": 12, "plan": {...}}
    """
    t0 = time.monotonic()
    body: dict[str, Any] = {}
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse({"ok": False, "error": "messages 必须是非空数组"}, status_code=400)
    if not all(isinstance(m, dict) for m in messages):
        return JSONResponse({"ok": False, "error": "messages 每一项必须是对象"}, status_code=400)

    try:
        budget = int(body.get("budget_tokens") or 128000)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "budget_tokens 必须是整数"}, status_code=400)
    try:
        keep_recent = int(body.get("keep_recent_turns") or 6)
        threshold = float(body.get("threshold") or 0.70)
        target = float(body.get("target") or 0.55)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "数值参数不合法"}, status_code=400)

    # 压力判定口径：宿主 tokenMeter 的实测 token / contextWindow（百分数）。
    # 传了就只用它判定；没传才退回估算并在响应里标 pressure_source。
    pressure_override: float | None = None
    raw_pressure = body.get("pressure_pct")
    if raw_pressure is not None:
        try:
            pressure_override = float(raw_pressure) / 100.0
        except (TypeError, ValueError):
            pressure_override = None
    pressure_source = str(body.get("pressure_source") or "") or None

    try:
        plan_body = plan(
            messages,
            budget_tokens=budget,
            keep_recent_turns=max(1, min(keep_recent, 40)),
            threshold=max(0.05, min(threshold, 0.99)),
            target=max(0.05, min(target, 0.9)),
            trigger=str(body.get("trigger") or "pressure"),
            pressure_override=pressure_override,
            pressure_source=pressure_source,
        )
        # 本次决策基于多少条、总共多少条：截断时两者不同，必须分开记，
        # 否则将来没法解释「为什么它没压那段本该压的」。
        plan_body["coverage"] = {
            "messages_used": int(body.get("messages_used") or len(messages)),
            "messages_total": int(body.get("messages_total") or len(messages)),
            "truncated": bool(body.get("truncated")),
            # 插件没上报时这里是 len(messages)/len(messages)/false，看起来像「看了全量」，
            # 其实只是默认值。必须标来源，否则又是一条假数据当真（2026-11-17 踩过）。
            "source": "plugin" if body.get("messages_total") is not None else "default:not-reported",
        }
    except Exception as exc:  # 算法异常也要留痕，不能静默
        plan_body = {"action": "error", "reason": str(exc)}
        trace_id = write_trace(
            DEFAULT_USER, str(body.get("session_id") or ""), str(body.get("bot") or ""),
            body.get("turn"), body.get("step"), str(body.get("model") or ""),
            "ctx-plan", plan_body, messages, int((time.monotonic() - t0) * 1000),
            ok=False, error=str(exc),
        )
        return JSONResponse({"ok": False, "error": str(exc), "trace_id": trace_id}, status_code=500)

    latency = int((time.monotonic() - t0) * 1000)
    trace_id = write_trace(
        DEFAULT_USER,
        str(body.get("session_id") or body.get("conversation_id") or ""),
        str(body.get("bot") or ""),
        body.get("turn"), body.get("step"), str(body.get("model") or ""),
        "ctx-plan", plan_body, messages, latency,
    )
    return JSONResponse({
        "ok": True,
        "trace_id": trace_id,
        "plan": plan_body,
        "latency_ms": latency,
        "estimate_rule": f"tokens = round(chars / {CHARS_PER_TOKEN})，仅估算",
    })


async def http_context_complete(request: Request) -> JSONResponse:
    """POST /context/trace/complete —— 插件补写一次「是否真替换 + 结果」。

    /context/plan 先落「计划」；真替换发生在插件里，替换完必须回来补这一笔，
    否则 trace 只能证明「我们想过」，证明不了「我们做了」。
    入参：{trace_id, swapped, after_hash?, status, shadowed_token_count?,
           start_seq?, end_seq?, error?, duration_ms?}
    """
    body: dict[str, Any] = {}
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    try:
        trace_id = int(body.get("trace_id") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "trace_id 必须是整数"}, status_code=400)
    if trace_id <= 0:
        return JSONResponse({"ok": False, "error": "trace_id 必填"}, status_code=400)

    patch = {
        "swapped": bool(body.get("swapped")),
        "status": str(body.get("status") or "unknown")[:64],
        "after_hash": str(body.get("after_hash") or ""),
        "shadowed_token_count": body.get("shadowed_token_count"),
        "start_seq": body.get("start_seq"),
        "end_seq": body.get("end_seq"),
        "duration_ms": body.get("duration_ms"),
        "error": str(body.get("error") or "")[:500],
    }
    ok = patch["status"] in ("ok", "planned", "skipped", "keep")
    try:
        with _conn(MEM_DSN) as c:
            with c.cursor() as cur:
                # 只合并 compaction 一个子对象，不覆盖计划阶段写的其余字段。
                cur.execute(
                    """update mem.ctx_trace
                       set compaction = coalesce(compaction, '{}'::jsonb) || %s::jsonb,
                           ok = %s,
                           error = %s,
                           latency_ms = coalesce(latency_ms, 0) + %s
                       where id = %s
                       returning id""",
                    (json.dumps(patch, ensure_ascii=False), ok,
                     ("" if ok else str(body.get("error") or "")[:500]),
                     int(body.get("duration_ms") or 0), trace_id),
                )
                row = cur.fetchone()
            c.commit()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)
    if not row:
        return JSONResponse({"ok": False, "error": "trace not found"}, status_code=404)
    return JSONResponse({"ok": True, "trace_id": trace_id})


async def http_context_trace(request: Request) -> JSONResponse:
    try:
        limit = max(1, min(int(request.query_params.get("limit", 20)), 200))
    except ValueError:
        limit = 20
    with _conn(MEM_DSN) as c:
        with c.cursor() as cur:
            cur.execute(
                """select id, to_char(created_at at time zone 'Asia/Shanghai','MM-DD HH24:MI:SS') ts,
                          session_id, turn, step, kind, history_tokens, ctx_tokens, ctx_limit,
                          ctx_pct, compaction, ok, latency_ms,
                          -- 复验要看的两条口径：判定用的 token 来源、历史覆盖是否真实上报。
                          payload->>'pressure_source' as pressure_source,
                          payload->'coverage'->>'source' as coverage_source,
                          payload->>'ctx_pct_dsh' as ctx_pct_dsh,
                          payload->>'messages_total' as messages_total
                   from mem.ctx_trace order by id desc limit %s""",
                (limit,),
            )
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return JSONResponse({"ok": True, "count": len(rows), "rows": rows})


def register_context(mcp) -> None:
    mcp.custom_route("/context/plan", methods=["POST"])(http_context_plan)
    mcp.custom_route("/context/trace/complete", methods=["POST"])(http_context_complete)
    mcp.custom_route("/context/trace", methods=["GET"])(http_context_trace)
