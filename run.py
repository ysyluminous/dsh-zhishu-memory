from __future__ import annotations

import uvicorn

from app.config import DB_LABEL, HOST, PORT, SERVICE_ENV
from app.mcp_server import mcp

app = mcp.http_app(path="/mcp")

if __name__ == "__main__":
    # 一行横幅写清「这个实例连的是哪」。2026-11-18 老大定：非 prod 必须自报本地副本，
    # 别让人把 41016 的本地快照当成线上真源（41023 是本地 Docker PG，不是转发）。
    note = "" if SERVICE_ENV == "prod" else " note=本地快照，不服务线上 mem"
    print(
        f"[zhishu-memory] env={SERVICE_ENV} host={HOST} port={PORT} "
        f"db={DB_LABEL or '(未标注)'}{note}",
        flush=True,
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
