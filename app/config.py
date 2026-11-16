from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")

MEM_DSN = os.getenv("MEM_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/mem")
ZHISHU_DSN = os.getenv("ZHISHU_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/zhishu")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "41012"))
DEFAULT_USER = os.getenv("MEM_DEFAULT_USER", "owner")
REQUIRE_KEY = os.getenv("MEM_REQUIRE_KEY", "0") == "1"
