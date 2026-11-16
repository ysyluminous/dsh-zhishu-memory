"""一句话记一笔的模型兜底：规则解析认不出来时，交给兼容 OpenAI 的模型端点。

设计约束：
- 没配端点（LLM_BASE_URL / LLM_MODEL）就完全不调用，返回 None，调用方退回规则结果或待办。
- 模型输出一律当不可信输入：只接受白名单字段与取值，越界就整条丢弃，绝不半信半疑地写库。
- 任何异常（超时、非 200、坏 JSON）都吞掉返回 None，一句话记一笔不能因为模型挂了就丢输入。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

TZ = timezone(timedelta(hours=8))

LLM_BASE = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_KEY = os.getenv("LLM_API_KEY", "")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "8"))

KINDS = {
    "task", "event", "habit", "routine", "sop", "purchase",
    "investment", "review", "sleep", "meal",
}
CAPITALS = {"health", "money", "relation", "growth"}
DEFAULT_SPAN_MIN = {
    "meal": 90, "purchase": 60, "investment": 30, "sleep": 480, "habit": 45,
    "routine": 60, "sop": 60, "review": 45, "event": 120, "task": 60,
}

_SYSTEM = (
    "你是一个中文口语时间账本解析器。把用户的一句话解析成一个 JSON 对象，只输出 JSON，不要解释、不要代码块。"
    "字段：kind（task|event|habit|routine|sop|purchase|investment|review|sleep|meal）、"
    "title（去掉时间词和金额的短标题，15 字以内）、"
    "amount（数字金额，人民币，不带单位；没有就 null）、"
    "capital（health|money|relation|growth 或 null）、"
    "start（ISO 8601 本地时间 YYYY-MM-DDTHH:MM:SS，没有时间词就 null）、"
    "end（同上，可 null）、planned（布尔，未来要做为 true，已经发生为 false）。"
    "规则：金额中文写法要换算（两百六=260，一千二=1200）；吃饭/聚餐类即使有金额 kind 也是 purchase；"
    "「算关系资本」这类话只用于判定 capital，不进标题；判断不了就给 task、start 给 null，不要编造时间。"
)


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn_to_num(text: str) -> float | None:
    """中文金额换算，含口语缩写：两百六=260、一千二=1200、一万五=15000。"""
    total = section = num = 0
    last_unit = None
    trailing: int | None = None
    zero_seen = False
    seen = False
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
            trailing = num
            seen = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            last_unit = unit
            trailing = None
            if unit == 10000:
                section = (section + num) * unit
                total += section
                section = 0
            else:
                section += (num or 1) * unit
            num = 0
            seen = True
        elif ch == "零":
            zero_seen = True
        else:
            return None
    if not seen:
        return None
    value = total + section + num
    if trailing is not None and last_unit and last_unit >= 100 and not zero_seen:
        value += trailing * (last_unit // 10) - trailing
    return float(value)


def available() -> bool:
    """端点是否配置齐。测试和降级都靠这个判断。"""
    return bool(LLM_BASE and LLM_MODEL)


def _post_json(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """单独拆出来，测试里替换掉它就能离线跑模型路径。"""
    headers = {"Content-Type": "application/json"}
    if LLM_KEY:
        headers["Authorization"] = f"Bearer {LLM_KEY}"
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_json(text: str) -> dict[str, Any] | None:
    """模型爱在 JSON 外面包一层 ```json，宽松地抠出第一个对象。"""
    raw = (text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str):
        cleaned = re.sub(r"[^\d.]", "", value)
        if cleaned:
            try:
                out = float(cleaned)
            except ValueError:
                return None
        else:
            out = cn_to_num(re.sub(r"[\s块钱元人民币RMBrmb]", "", value)) or None
            if out is None:
                return None
    else:
        return None
    if out < 0 or out > 10_000_000:
        return None
    return round(out, 2)


def _clock(value: Any, now: datetime) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    parsed = parsed.astimezone(TZ)
    # 模型偶尔给出离谱年份；超出前后一年就当没解析出来
    if abs((parsed.date() - now.date()).days) > 366:
        return None
    return parsed.replace(second=0, microsecond=0)


def normalize(payload: dict[str, Any], raw: str, now: datetime) -> dict[str, Any] | None:
    """把模型输出收敛成和规则解析同构的草稿；不合格返回 None。"""
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    title = re.sub(r"\s+", " ", title).strip()[:80]

    kind = payload.get("kind")
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    if kind not in KINDS:
        kind = "task"

    capital = payload.get("capital")
    capital = capital.strip().lower() if isinstance(capital, str) else None
    if capital not in CAPITALS:
        capital = None

    raw_amount = payload.get("amount")
    amount = _num(raw_amount)
    if raw_amount not in (None, "") and amount is None:
        # 给了金额却换算不出来（负值、乱码、超上限）：整条当不可信丢掉
        return None
    start = _clock(payload.get("start"), now)
    end = _clock(payload.get("end"), now)
    if start and end and end <= start:
        end = None
    if start and not end:
        end = start + timedelta(minutes=DEFAULT_SPAN_MIN.get(kind, 60))

    planned = payload.get("planned")
    if not isinstance(planned, bool):
        planned = bool(start and start > now + timedelta(minutes=30))
    elif start is None:
        planned = False

    # 模型说已发生但给了未来时间，以时间为准
    if start and start > now + timedelta(minutes=30):
        planned = True
    if start and start <= now:
        planned = False

    day_offset = (start.date() - now.date()).days if start else 0
    return {
        "recognized": True,
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
        "parser": "llm",
    }


def parse(raw: str, now: datetime | None = None) -> dict[str, Any] | None:
    """调模型解析一句话。没端点、超时、输出不合规都返回 None。"""
    if not available():
        return None
    now = (now or datetime.now(TZ)).astimezone(TZ)
    prompt = (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}，星期{'一二三四五六日'[now.weekday()]}。"
        f"用户说：{raw}"
    )
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        resp = _post_json(payload, LLM_TIMEOUT)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    except Exception:
        return None

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    data = _pick_json(content if isinstance(content, str) else "")
    if not data:
        return None
    try:
        return normalize(data, raw, now)
    except Exception:
        return None
