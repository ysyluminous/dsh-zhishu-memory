from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from . import growth_llm
from .config import ZHISHU_DSN

TZ = timezone(timedelta(hours=8))

# 一句话记一笔：先解析成草稿，confirm=true 才写 growth.time_entry。
# 解析不出来就退回一条普通待办，原文一条字都不丢。

_DAY_WORDS = [
    ("大前天", -3),
    ("前天", -2),
    ("昨天", -1),
    ("昨日", -1),
    ("今天", 0),
    ("今日", 0),
    ("明天", 1),
    ("明日", 1),
    ("后天", 2),
]

_PERIOD_HOUR = [
    ("凌晨", 5),
    ("早上", 8),
    ("早晨", 7),
    ("上午", 9),
    ("中午", 12),
    ("下午", 14),
    ("傍晚", 18),
    ("晚上", 20),
    ("夜里", 22),
    ("夜晚", 21),
    ("深夜", 23),
    ("半夜", 0),
]

_CAPITAL_WORDS = [
    ("健康", "health"),
    ("身体", "health"),
    ("关系", "relation"),
    ("人情", "relation"),
    ("社交", "relation"),
    ("钱", "money"),
    ("财务", "money"),
    ("理财", "money"),
    ("消费", "money"),
    ("成长", "growth"),
    ("学习", "growth"),
    ("认知", "growth"),
]

_CAPITAL_LABEL = {"health": "健康资本", "money": "钱资本", "relation": "关系资本", "growth": "成长资本"}
_KIND_LABEL = {
    "task": "待办", "event": "事件", "habit": "习惯", "routine": "日常", "sop": "流程",
    "purchase": "消费", "investment": "投资", "review": "复盘", "sleep": "睡眠", "meal": "吃饭",
}
_DURATION_MIN = {
    "meal": 90, "purchase": 60, "investment": 30, "sleep": 480, "habit": 45,
    "routine": 60, "sop": 60, "review": 45, "event": 120, "task": 60,
}

_CLOCK_COLON = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)")
_CLOCK_DIAN = re.compile(r"(?<!\d)(\d{1,2})\s*点\s*(半|\d{1,2})?\s*分?")
_AMOUNT_UNIT = re.compile(r"(?<![\d.])(\d+(?:\.\d{1,2})?)\s*(?:块钱|块|元|人民币|RMB|rmb)")
_AMOUNT_PREFIX = re.compile(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)")
_AMOUNT_CN = re.compile(r"([零一二两三四五六七八九十百千万]+)\s*(?:块钱|块|元)")

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

_CLOCK_TEXT = re.compile(
    r"(\d{1,2}\s*[:：]\s*\d{1,2}|\d{1,2}\s*点\s*(?:半|\d{1,2})?\s*分?)"
)
_DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_DURATION_TEXT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:个)?\s*(小时|钟头|分钟|min|h)(?![a-z])")


def _cn_to_num(text: str) -> float | None:
    """中文金额换算与模型兜底共用一份，避免两套口径。"""
    return growth_llm.cn_to_num(text)


def _extract_amount(text: str) -> tuple[float | None, str | None]:
    m = _AMOUNT_UNIT.search(text)
    if m:
        return float(m.group(1)), m.group(0)
    m = _AMOUNT_PREFIX.search(text)
    if m:
        return float(m.group(1)), m.group(0)
    m = _AMOUNT_CN.search(text)
    if m:
        value = _cn_to_num(m.group(1))
        if value:
            return value, m.group(0)
    return None, None


def _extract_clocks(text: str) -> list[tuple[int, int, tuple[int, int]]]:
    found: list[tuple[int, int, tuple[int, int]]] = []
    covered: list[tuple[int, int]] = []
    for m in _CLOCK_COLON.finditer(text):
        found.append((int(m.group(1)), int(m.group(2)), m.span()))
        covered.append(m.span())
    for m in _CLOCK_DIAN.finditer(text):
        if any(m.start() >= a and m.end() <= b for a, b in covered):
            continue
        minute = 30 if m.group(2) == "半" else (int(m.group(2)) if m.group(2) else 0)
        found.append((int(m.group(1)), minute, m.span()))
    found.sort(key=lambda item: item[2][0])
    return found


_KIND_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("investment", ("投资", "基金", "股票", "定投", "加仓", "买入")),
    ("sleep", ("睡觉", "午睡", "睡眠", "睡了")),
    ("meal", ("吃饭", "午餐", "晚餐", "早餐", "早饭", "午饭", "晚饭", "咖啡", "喝酒", "聚餐")),
    ("review", ("复盘", "回顾", "总结")),
    ("habit", ("健身", "跑步", "打球", "游泳", "散步", "瑜伽")),
    ("sop", ("流程", "步骤", "SOP", "sop")),
]


def _kind_hits(text: str) -> list[str]:
    """句子里命中了哪几类关键词，按种类优先级返回；金额是否压过它们由 _detect_kind 决定。"""
    return [kind for kind, words in _KIND_WORDS if any(w in text for w in words)]


def _detect_kind(text: str, amount: float | None) -> str:
    hits = _kind_hits(text)
    if "investment" in hits:
        return "investment"
    if amount is not None:
        return "purchase"
    return hits[0] if hits else "task"


def _confidence(
    *,
    amount: float | None,
    has_time: bool,
    kind: str,
    hits: list[str],
    clocks: list[tuple[int, int, tuple[int, int]]],
    period_hour: int | None,
    explicit_day,
    title: str,
) -> tuple[str, list[str]]:
    """规则解析的可信度：只有真会记错的风险才降成 low。

    low 的意思是「这条先别直接信」：配了模型端点就交给模型复核一遍，没配就留给人确认。
    """
    reasons: list[str] = []
    if amount is not None and not has_time:
        reasons.append("只认出金额、没认出时间，暂按现在的时间记")
    if kind == "purchase" and any(h in ("meal", "sleep", "habit", "review") for h in hits):
        reasons.append("句子里有吃饭/睡觉/运动的字样，但因为有金额记成了消费")
    if clocks and period_hour is None and explicit_day is None and clocks[0][0] < 6:
        reasons.append("只写了 1–5 点，没说是凌晨还是下午")
    if len((title or "").strip()) < 2:
        reasons.append("去掉时间和金额后标题几乎为空")
    return ("low" if reasons else "high"), reasons


def _detect_capital(text: str) -> str | None:
    if "资本" not in text:
        return None
    for word, key in _CAPITAL_WORDS:
        if word in text:
            return key
    return None


def _title_body(text: str, amount_text: str | None) -> str:
    """去掉时间词、金额、资本口径后的正文；可能为空（只剩时间和钱的情况）。"""
    title = text
    for word, _ in _DAY_WORDS:
        title = title.replace(word, " ")
    for word, _ in _PERIOD_HOUR:
        title = title.replace(word, " ")
    title = _CLOCK_TEXT.sub(" ", title)
    title = _DATE_MD.sub(" ", title)
    title = _DATE_ISO.sub(" ", title)
    title = _DURATION_TEXT.sub(" ", title)
    title = re.sub(r"(^|\s)[到至\-~—]\s*", " ", title)
    if amount_text:
        title = title.replace(amount_text, " ")
    title = re.sub(r"[，,。;；]?\s*算(?:进|作|成)?[^，,。;；]{0,8}资本", " ", title)
    title = re.sub(r"[，,。;；]?\s*记(?:在|到|进)?[^，,。;；]{0,8}资本", " ", title)
    return re.sub(r"\s+", " ", title).strip(" ，,。;；、-—")


def _clean_title(text: str, amount_text: str | None) -> str:
    return _title_body(text, amount_text)[:80] or text.strip()[:80]


def _parse(raw: str, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    amount, amount_text = _extract_amount(raw)
    day_offset = 0
    day_word = None
    for word, offset in _DAY_WORDS:
        if word in raw:
            day_off, day_word = offset, word
            break
    else:
        day_off = 0
    day_offset = day_off

    period_hour = None
    for word, hour in _PERIOD_HOUR:
        if word in raw:
            period_hour = hour
            break

    clocks = _extract_clocks(raw)

    explicit_day = None
    m = _DATE_ISO.search(raw)
    if m:
        try:
            explicit_day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            explicit_day = None
    if explicit_day is None:
        m = _DATE_MD.search(raw)
        if m:
            try:
                explicit_day = datetime(now.year, int(m.group(1)), int(m.group(2))).date()
            except ValueError:
                explicit_day = None

    duration_min = None
    dm = _DURATION_TEXT.search(raw)
    if dm:
        value = float(dm.group(1))
        duration_min = int(round(value * 60)) if dm.group(2) in ("小时", "钟头", "h") else int(round(value))

    has_time = bool(clocks) or period_hour is not None or day_word is not None or explicit_day is not None
    recognized = bool(has_time or amount is not None)

    hits = _kind_hits(raw)
    kind = _detect_kind(raw, amount)
    capital = _detect_capital(raw)
    title_body = _title_body(raw, amount_text)
    title = title_body[:80] or raw.strip()[:80]
    if recognized:
        confidence, review = _confidence(
            amount=amount, has_time=has_time, kind=kind, hits=hits, clocks=clocks,
            period_hour=period_hour, explicit_day=explicit_day, title=title_body,
        )
    else:
        confidence, review = "none", []

    base_day = explicit_day or (now + timedelta(days=day_offset)).date()
    day_offset = (base_day - now.date()).days
    span = duration_min or _DURATION_MIN.get(kind, 60)
    if clocks:
        hour, minute = clocks[0][0], clocks[0][1]
        if period_hour is not None:
            if period_hour >= 12 and hour < 12:
                hour += 12
            elif period_hour >= 18 and hour == 12:
                hour = 0
        elif hour < 6 and now.hour >= 12:
            # 没有时段词时，把 1-5 点当成凌晨而不是下午
            pass
        start = datetime(base_day.year, base_day.month, base_day.day, hour % 24, minute, tzinfo=TZ)
        if len(clocks) > 1:
            end_hour, end_minute = clocks[1][0], clocks[1][1]
            if period_hour is not None and period_hour >= 12 and end_hour < 12:
                end_hour += 12
            end = datetime(base_day.year, base_day.month, base_day.day, end_hour % 24, end_minute, tzinfo=TZ)
            if end <= start:
                end += timedelta(hours=12)
        else:
            end = start + timedelta(minutes=span)
    elif period_hour is not None:
        start = datetime(base_day.year, base_day.month, base_day.day, period_hour % 24, 0, tzinfo=TZ)
        end = start + timedelta(minutes=span)
    elif has_time:
        if explicit_day is not None and explicit_day != now.date():
            start = datetime(base_day.year, base_day.month, base_day.day, 9, 0, tzinfo=TZ)
        else:
            start = now.replace(second=0, microsecond=0)
        end = start + timedelta(minutes=span)
    else:
        start = end = None

    planned = False
    if start is not None and (day_offset > 0 or start > now + timedelta(minutes=30)):
        planned = True

    return {
        "recognized": recognized,
        "kind": kind,
        "title": title,
        "note": raw,
        "capital": capital,
        "amount": amount,
        "currency": "CNY",
        "start": start,
        "end": end,
        "planned": planned,
        "day_offset": day_offset,
        "confidence": confidence,
        "review": review,
    }


_CAPITAL_ALIAS = {
    "health": "health", "健康": "health", "身体": "health",
    "money": "money", "钱": "money", "财务": "money",
    "relation": "relation", "关系": "relation", "人情": "relation",
    "growth": "growth", "成长": "growth", "认知": "growth",
}
_TIME_ONLY = re.compile(r"^(\d{1,2}):(\d{2})$")
_DATE_ONLY = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def _parse_when(value: str, now: datetime) -> datetime | None:
    """调用方给的时间：只写 08:30 按今天算，只写日期按当天 09:00 算，其余按 ISO 解析。"""
    text = (value or "").strip()
    if not text:
        return None
    m = _TIME_ONLY.match(text)
    if m:
        return now.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)),
                           second=0, microsecond=0)
    m = _DATE_ONLY.match(text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        9, 0, tzinfo=TZ)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"时间认不出来：{text}（要 ISO，如 2026-11-14T08:00:00+08:00）") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def build_manual(*, kind: str, title: str, amount: float | None = None,
                 capital: str = "", currency: str = "CNY",
                 start: str = "", end: str = "", status: str = "",
                 note: str = "", now: datetime | None = None) -> dict[str, Any]:
    """调用方（任何接进来的 AI）已经解析好的标准字段 -> 内部草稿。

    这里不做自然语言解析：谁调用 MCP，谁负责把「客户那顿饭两百六」变成
    kind=purchase / title=客户那顿饭 / amount=260。服务端只校验和落库，
    因此不需要配任何模型端点。
    """
    now = (now or datetime.now(TZ)).astimezone(TZ)
    kind_key = (kind or "").strip().lower()
    if kind_key not in _KIND_LABEL:
        raise ValueError(f"kind 只认：{', '.join(sorted(_KIND_LABEL))}")

    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("title 不能为空")
    clean_title = clean_title[:80]

    value = None
    if amount is not None and str(amount).strip() != "":
        try:
            value = float(amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("amount 要数字，比如 260") from exc
        if value < 0:
            raise ValueError("amount 不能是负数")

    cap_key = None
    raw_cap = (capital or "").strip()
    if raw_cap:
        cap_key = _CAPITAL_ALIAS.get(raw_cap.lower()) or _CAPITAL_ALIAS.get(raw_cap)
        if cap_key is None:
            raise ValueError("capital 只认 health/money/relation/growth（或 健康/钱/关系/成长）")

    begins = _parse_when(start, now)
    ends = _parse_when(end, now)
    if begins is None and ends is not None:
        begins = ends - timedelta(minutes=_DURATION_MIN.get(kind_key, 60))
    if begins is not None and ends is None:
        ends = begins + timedelta(minutes=_DURATION_MIN.get(kind_key, 60))
    if begins is not None and ends is not None and ends <= begins:
        raise ValueError("end 要晚于 start")

    state = (status or "").strip().lower()
    if state and state not in ("planned", "done"):
        raise ValueError("status 只认 planned 或 done")
    if state:
        planned = state == "planned"
    else:
        planned = bool(begins is not None and begins > now + timedelta(minutes=30))

    return {
        "recognized": True,
        "kind": kind_key,
        "title": clean_title,
        "note": (note or "").strip() or clean_title,
        "capital": cap_key,
        "amount": value,
        "currency": (currency or "CNY").strip().upper() or "CNY",
        "start": begins,
        "end": ends,
        "planned": planned,
        "day_offset": (begins.date() - now.date()).days if begins else 0,
        "confidence": "high",
        "review": [],
        "parser": "manual",
    }


def resolve_draft(raw: str, now: datetime | None = None) -> dict[str, Any]:
    """先走规则；规则认不出、或者认出来但没底，再交给模型复核。

    draft 里带 `parser`：rule / llm。模型只在规则失败或低置信度时被调用，
    所以配了端点也不会让每条记录都变慢、变贵；模型回不出合规草稿就保留规则结果。
    """
    now = (now or datetime.now(TZ)).astimezone(TZ)
    draft = _parse(raw, now)
    draft["parser"] = "rule"
    if draft["confidence"] == "high":
        return draft
    alt = growth_llm.parse(raw, now)
    if alt is not None and alt.get("recognized"):
        alt["confidence"] = "high"
        alt["review"] = []
        return alt
    return draft


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _ledger_lookup(user_id: str) -> str | None:
    """查映射表；没有这一行返回 None。"""
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute(
                "select ledger_user_id from growth.ledger_identity where login_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    return row[0] if row else None


def _resolve_ledger(user_id: str) -> str:
    """把 Key 的 user_id 映射成账本 user_id。

    网页端写的是登录账号 id，Key 端写的是 mem.api_key.user_id；同一个人要落到
    同一本账，靠 growth.ledger_identity 映射。没有映射就各自成账（默认隔离）。
    账本表还没建出来的旧部署上按原值用，避免整条链路不可用。
    """
    try:
        return _ledger_lookup(user_id) or user_id
    except psycopg.errors.UndefinedTable:
        return user_id


def _insert_entry(user_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    planned = draft["planned"]
    status = "planned" if planned else "done"
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute(
                """insert into growth.time_entry
                   (user_id, kind, title, note, planned_start, planned_end,
                    actual_start, actual_end, status, capital, amount, currency, source)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ai')
                   returning id, created_at""",
                (
                    user_id, draft["kind"], draft["title"], draft["note"],
                    draft["start"] if planned else None,
                    draft["end"] if planned else None,
                    None if planned else draft["start"],
                    None if planned else draft["end"],
                    status, draft["capital"], draft["amount"], draft["currency"],
                ),
            )
            row = cur.fetchone()
        c.commit()
    out = {
        "id": row[0],
        "user_id": user_id,
        "kind": draft["kind"],
        "title": draft["title"],
        "note": draft["note"],
        "status": status,
        "capital": draft["capital"],
        "amount": draft["amount"],
        "currency": draft["currency"],
        "source": "ai",
        "actual_start": None if planned else _iso(draft["start"]),
        "actual_end": None if planned else _iso(draft["end"]),
        "planned_start": _iso(draft["start"]) if planned else None,
        "planned_end": _iso(draft["end"]) if planned else None,
        "created_at": row[1].isoformat(),
    }
    return out


def _insert_todo(user_id: str, raw: str) -> dict[str, Any]:
    title = raw.strip()[:80]
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute(
                """insert into growth.time_entry (user_id, kind, title, note, status, source)
                   values (%s,'task',%s,%s,'planned','ai')
                   returning id, created_at""",
                (user_id, title, raw),
            )
            row = cur.fetchone()
        c.commit()
    return {
        "id": row[0], "user_id": user_id, "kind": "task", "title": title,
        "note": raw, "status": "planned", "capital": None, "amount": None,
        "currency": "CNY", "source": "ai",
        "actual_start": None, "actual_end": None,
        "planned_start": None, "planned_end": None,
        "created_at": row[1].isoformat(),
    }


def _dedup(user_id: str, draft: dict[str, Any]) -> dict[str, Any] | None:
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute(
                """select id, status, created_at from growth.time_entry
                   where user_id = %s and source = 'ai' and kind = %s and title = %s
                     and amount is not distinct from %s
                     and actual_start is not distinct from %s
                     and planned_start is not distinct from %s
                     and created_at > now() - interval '10 minutes'
                   order by id desc limit 1""",
                (user_id, draft["kind"], draft["title"], draft["amount"],
                 None if draft["planned"] else draft["start"],
                 draft["start"] if draft["planned"] else None),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "status": row[1], "created_at": row[2].isoformat(), "deduped": True}


def _describe(draft: dict[str, Any]) -> str:
    start = draft["start"]
    when = ""
    if start:
        labels = {-3: "大前天", -2: "前天", -1: "昨天", 0: "今天", 1: "明天", 2: "后天"}
        label = labels.get(draft["day_offset"], start.strftime("%Y-%m-%d"))
        when = f"{label} {start.strftime('%H:%M')}"
        if draft["end"]:
            when += f"–{draft['end'].strftime('%H:%M')}"
    parts = [_KIND_LABEL.get(draft["kind"], draft["kind"]), draft["title"]]
    if when:
        parts.append(when)
    if draft["amount"] is not None:
        parts.append(f"{draft['amount']:g} 元")
    if draft["capital"]:
        parts.append(_CAPITAL_LABEL.get(draft["capital"], draft["capital"]))
    return " · ".join(parts)


def _public_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """草稿对外形状：规则和模型两条路产出一致，前端和 MCP 客户端不用分叉。"""
    return {
        "kind": draft["kind"],
        "title": draft["title"],
        "note": draft["note"],
        "capital": draft["capital"],
        "amount": draft["amount"],
        "currency": draft["currency"],
        "status": "planned" if draft["planned"] else "done",
        "actual_start": None if draft["planned"] else _iso(draft["start"]),
        "actual_end": None if draft["planned"] else _iso(draft["end"]),
        "planned_start": _iso(draft["start"]) if draft["planned"] else None,
        "planned_end": _iso(draft["end"]) if draft["planned"] else None,
        "source": "ai",
        "confidence": draft.get("confidence", "high"),
        "review": draft.get("review") or [],
    }


def growth_parse(text: str, now: datetime | None = None) -> dict[str, Any]:
    """只解析不落库，也不查库：用来核对一句话会被记成什么、以及模型兜底是否生效。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("说一句话才能解析，text 不能为空")
    draft = resolve_draft(raw, now)
    if not draft["recognized"]:
        return {
            "ok": True, "parser": "todo", "recognized": False,
            "confidence": "none", "review": [],
            "message": "没识别出时间和金额，确认时会存为待办（原文保留）",
            "entry_preview": {"kind": "task", "title": raw[:80]},
        }
    review = draft.get("review") or []
    message = f"识别为：{_describe(draft)}"
    if review:
        message += "。低置信度：" + "；".join(review) + "。确认前请先核对，或告诉我正确的时间和分类"
    return {
        "ok": True,
        "parser": draft.get("parser", "rule"),
        "recognized": True,
        "confidence": draft.get("confidence", "high"),
        "review": review,
        "message": message,
        "draft": _public_draft(draft),
    }


def growth_log(user_id: str, text: str, confirm: bool = False,
               now: datetime | None = None) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("说一句话才能记，text 不能为空")

    ledger = _resolve_ledger(user_id)
    draft = resolve_draft(raw, now)
    parser = draft.get("parser", "rule")
    if not draft["recognized"]:
        entry = _insert_todo(ledger, raw)
        key = re.sub(r"\s+", "", raw)
        message = "没听清，已存为待办" if len(key) <= 2 else "没识别出时间和金额，已存为待办"
        return {"ok": True, "mode": "stored_todo", "parser": "todo", "ledger_user": ledger,
                "message": message + "（原文已保留）", "entry": entry}

    summary = _describe(draft)
    public_draft = _public_draft(draft)
    review = draft.get("review") or []

    if not confirm:
        message = f"识别为：{summary}。确认后才会落库。"
        if review:
            message += "（低置信度：" + "；".join(review) + "，落库前请先核对）"
        return {
            "ok": True,
            "mode": "draft",
            "parser": parser,
            "ledger_user": ledger,
            "confidence": draft.get("confidence", "high"),
            "needs_review": bool(review),
            "review": review,
            "message": message,
            "draft": public_draft,
            "next": "确认无误后调用 growth_log(text, confirm=true) 保存",
        }

    existing = _dedup(ledger, draft)
    if existing:
        return {"ok": True, "mode": "saved", "deduped": True, "parser": parser,
                "ledger_user": ledger, "confidence": draft.get("confidence", "high"),
                "message": f"这条刚记过（#{existing['id']}），没有重复写入。", "entry": existing}

    entry = _insert_entry(ledger, draft)
    return {"ok": True, "mode": "saved", "parser": parser, "ledger_user": ledger,
            "confidence": draft.get("confidence", "high"),
            "message": f"已记下：{summary}", "entry": entry}


def growth_add(user_id: str, *, kind: str, title: str, amount: float | None = None,
               capital: str = "", start: str = "", end: str = "", status: str = "",
               note: str = "", currency: str = "CNY", confirm: bool = False,
               now: datetime | None = None) -> dict[str, Any]:
    """收已经解析好的标准字段并落库；解析由调用方完成，服务端不需要模型。"""
    draft = build_manual(kind=kind, title=title, amount=amount, capital=capital,
                         start=start, end=end, status=status, note=note,
                         currency=currency, now=now)
    ledger = _resolve_ledger(user_id)
    summary = _describe(draft)

    if not confirm:
        return {
            "ok": True,
            "mode": "draft",
            "parser": "manual",
            "ledger_user": ledger,
            "confidence": "high",
            "needs_review": False,
            "review": [],
            "message": f"将会记成：{summary}。confirm=true 才落库。",
            "draft": _public_draft(draft),
            "next": "确认无误后带 confirm=true 再调一次",
        }

    existing = _dedup(ledger, draft)
    if existing:
        return {"ok": True, "mode": "saved", "deduped": True, "parser": "manual",
                "ledger_user": ledger, "confidence": "high",
                "message": f"这条刚记过（#{existing['id']}），没有重复写入。", "entry": existing}

    entry = _insert_entry(ledger, draft)
    return {"ok": True, "mode": "saved", "parser": "manual", "ledger_user": ledger,
            "confidence": "high", "message": f"已记下：{summary}", "entry": entry}


def growth_day(user_id: str, day: str = "", limit: int = 100) -> dict[str, Any]:
    """一天的时间块：读合并视图 v_day_blocks（新 activity_log + 历史 time_entry）。"""
    target = day.strip() or datetime.now(TZ).strftime("%Y-%m-%d")
    limit = max(1, min(limit, 200))
    ledger = _resolve_ledger(user_id)
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute(
                """select b.src, b.ref_id, b.activity_id,
                          coalesce(a.name, b.activity_id) as title,
                          b.start_at, b.end_at, b.duration_min, b.status, b.note,
                          b.data_anomaly
                   from growth.v_day_blocks b
                   left join growth.activity a
                     on a.user_id = b.user_id and a.id = b.activity_id
                   where b.user_id = %s
                     and (b.start_at at time zone 'Asia/Shanghai')::date = %s::date
                   order by b.start_at nulls last, b.ref_id
                   limit %s""",
                (ledger, target, limit),
            )
            rows = cur.fetchall()
    items = [
        {
            "id": r[1], "kind": r[0], "activity_id": r[2], "title": r[3],
            "start_at": r[4].isoformat() if r[4] else None,
            "end_at": r[5].isoformat() if r[5] else None,
            "duration_min": r[6], "status": r[7], "note": r[8],
            "data_anomaly": r[9],
        }
        for r in rows
    ]
    return {"date": target, "user_id": ledger, "count": len(items), "items": items,
            "source": "growth.v_day_blocks（activity_log + 历史 time_entry 合并读）"}


# ---------------- 时间块（activity_log，唯一写入真源） ----------------

_BLOCK_STATUS = {"计划", "实做", "未记"}
_ACTIVITY_MAX = 30


def _activity_id(path: str) -> str:
    """`运动>球类>羽毛球` -> `运动.球类.羽毛球`（稳定、可读、可当主键）。"""
    segs = [s.strip() for s in re.split(r"[>／/]", path) if s.strip()]
    return ".".join(segs)


def _ensure_activity(cur, user_id: str, path: str) -> str:
    """按路径补全活动实体（先父后子），返回叶子 id。超 30 条直接报错。"""
    segs = [s.strip() for s in re.split(r"[>／/]", path) if s.strip()]
    if not segs:
        raise ValueError("activity_path 不能为空")
    parent = None
    for seg in segs:
        aid = _activity_id(">".join((parent.split(".") + [seg]) if parent else [seg]))
        cur.execute(
            "select name from growth.activity where user_id=%s and id=%s",
            (user_id, aid),
        )
        if cur.fetchone() is None:
            cur.execute(
                "select count(*) from growth.activity where user_id=%s and archived_at is null",
                (user_id,),
            )
            if cur.fetchone()[0] >= _ACTIVITY_MAX:
                raise ValueError(f"activity 实体已达上限 {_ACTIVITY_MAX} 条，请先归档或复用")
            cur.execute(
                """insert into growth.activity (id, user_id, name, parent_id)
                   values (%s,%s,%s,%s) on conflict (user_id, id) do nothing""",
                (aid, user_id, seg, parent),
            )
        parent = aid
    return parent


def growth_block_add(user_id: str, said: str = "", activity_path: str = "",
                     start_at: str = "", end_at: str = "", status: str = "实做",
                     note: str = "", tags: dict | None = None) -> dict[str, Any]:
    """一句话一段：写一个时间块进 growth.activity_log（唯一真源）。

    结束时间口径（2026-11-16）：
      - 只有 `status=实做` 必须给 end_at；缺了就报「计划块可以没有结束时间；收工后补 end_at」；
      - `status=计划` **和 `status=未记`** 都允许没有 end_at：计划块是还没收工，未记块是本就记不清，
        逼填只会让人编一个数，等于往库里埋假数据；
      - 只要给了 end_at，无论什么 status，都仍然校验 end_at > start_at。
    """
    if not activity_path.strip():
        raise ValueError("activity_path 必填，例：运动>球类>羽毛球")
    if not start_at.strip():
        raise ValueError("start_at 必填（ISO 时间）")
    status = (status or "实做").strip()
    if status not in _BLOCK_STATUS:
        raise ValueError(f"status 只能是 {'/'.join(sorted(_BLOCK_STATUS))}")
    end_raw = end_at.strip()
    if not end_raw and status == "实做":
        raise ValueError("计划块可以没有结束时间；收工后补 end_at")
    ledger = _resolve_ledger(user_id)
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute(
                """select %s::timestamptz, %s::timestamptz""",
                (start_at, end_raw or None),
            )
            start, end = cur.fetchone()
            # 有结束时间才比大小；没有结束时间的计划块不参与比较（库侧同样放宽）。
            if end is not None and end <= start:
                raise ValueError("end_at 必须晚于 start_at")
            activity_id = _ensure_activity(cur, ledger, activity_path)
            cur.execute(
                """insert into growth.activity_log
                     (user_id, activity_id, start_at, end_at, status, note, source)
                   values (%s,%s,%s,%s,%s,%s,'mcp')
                   returning id, duration_min""",
                (ledger, activity_id, start, end, status, note),
            )
            log_id, duration = cur.fetchone()
            tag_ids = []
            for dimension, names in (tags or {}).items():
                for name in names or []:
                    cur.execute(
                        """insert into growth.tag (user_id, dimension, name)
                           values (%s,%s,%s)
                           on conflict (user_id, dimension, name)
                           do update set name = excluded.name
                           returning id""",
                        (ledger, dimension, str(name)),
                    )
                    tag_ids.append(cur.fetchone()[0])
            for tid in tag_ids:
                cur.execute(
                    """insert into growth.activity_log_tag (user_id, log_id, tag_id)
                       values (%s,%s,%s) on conflict do nothing""",
                    (ledger, log_id, tid),
                )
        c.commit()
    return {"ok": True, "id": log_id, "activity_id": activity_id,
            "duration_min": duration, "status": status, "tags": len(tag_ids),
            "src": "activity_log", "said": said}
