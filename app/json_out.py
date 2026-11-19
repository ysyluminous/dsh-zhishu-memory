"""统一的 JSON 序列化出口。

2026-11-17 修：`GET /context/trace` 500，根因是 psycopg 从 numeric 列取回
`decimal.Decimal`，Starlette 的 JSONResponse 用默认 json.dumps，遇到 Decimal
直接抛 `TypeError: Object of type Decimal is not JSON serializable`。

问题不是某一处，而是「所有把数据库行原样塞进响应」的地方都可能踩：
`mem.ctx_trace.ctx_pct` 是 numeric(6,3)，`console.py` 里 overview / ctx /
ctx_detail / gate / docs 同样把行 dict 直接返回。所以不在每个 handler 里补，
而是在**响应类这一层**统一兜住——以后新加接口只要用 SafeJSONResponse 就自动安全。

口径（账号主 2026-11-17 定，改历史必须可复盘）：
- Decimal → float，不让数值变字符串（前端要能直接参与计算、比较）；
- datetime / date / time → ISO 8601 字符串；
- bytes → utf-8（非法字节替换，不抛）；
- UUID → 字符串；
- 集合类 → list；
- 其它未知类型 → str()，宁可丢精度也不让整个响应 500。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from starlette.responses import JSONResponse


def json_default(value: Any) -> Any:
    """json.dumps(default=...) 的兜底转换，只处理「数据库原样值」。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def dumps(content: Any) -> str:
    """和 SafeJSONResponse 同口径的字符串形式，给需要自己拼 body 的地方用。"""
    return json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=json_default,
    )


class SafeJSONResponse(JSONResponse):
    """替换 starlette 的 JSONResponse：除默认类型外，数据库原样值也能序列化。"""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return dumps(content).encode("utf-8")
