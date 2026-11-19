from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
# 多实例并存时用 MEM_ENV_FILE 指定各自的 env 文件，避免两个实例共用一份配置串库。
# 例：MEM_ENV_FILE=D:\code\dsh-zhishu-memory\.env.local-dev（本地开发实例）
load_dotenv(os.getenv("MEM_ENV_FILE") or (BASE / ".env"))

MEM_DSN = os.getenv("MEM_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/mem")
ZHISHU_DSN = os.getenv("ZHISHU_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/zhishu")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "41012"))
DEFAULT_USER = os.getenv("MEM_DEFAULT_USER", "owner")
REQUIRE_KEY = os.getenv("MEM_REQUIRE_KEY", "0") == "1"
# 实例身份：prod（线上库）/ local-dev（本地库）。出现在 /health、启动日志、控制台页头，
# 为的是两个实例并存时一眼能看出「我现在连的是哪个库」。默认 prod，由各实例的 env 文件覆盖。
SERVICE_ENV = os.getenv("DSH_ENV", "prod")
# 库标识（不含密码），只用来显示，例：127.0.0.1:41023/mem
DB_LABEL = os.getenv("MEM_DB_LABEL", "")

# 文件型存储（audit / observations / issues）的根目录：默认 <repo>/data。
# 两个实例并存时用 MEM_DATA_DIR 指向同一份数据（S3 对齐期）。
DATA_DIR = Path(os.getenv("MEM_DATA_DIR", str(BASE / "data")))
