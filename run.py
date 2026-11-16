from __future__ import annotations

import uvicorn

from app.config import HOST, PORT
from app.mcp_server import mcp

app = mcp.http_app(path="/mcp")

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
