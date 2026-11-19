from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
# 多实例并存时用 MEM_ENV_FILE 指定各自的 env 文件，避免两个实例共用一份配置串库。
# 例：MEM_ENV_FILE=D:\code\dsh-zhishu-memory\.env.local-dev（本地开发实例）
# 注意：.env 现在是**可选**的；主配置在 DSH 面板（settings.json），见下面 _setting()。
load_dotenv(os.getenv("MEM_ENV_FILE") or (BASE / ".env"))

# DSH 面板的设置文件（四行插件共用）。服务自己也能读它，这样即使启动它的宿主
# 没把变量传进来、或者 .env 已经不用了，配置依然只有一处真源。
PANEL_SETTINGS = Path(
    os.getenv("DSH_ZHISHU_SETTINGS")
    or (Path.home() / ".dsh" / "zhishu-memory" / "settings.json")
)


def _setting(key: str):
    """Read one panel value; missing / empty / unparsable settings count as absent."""
    try:
        data = json.loads(PANEL_SETTINGS.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if isinstance(value, str):
        value = value.strip()
    return value if value not in (None, "") else None


def _value(env_name: str, setting_key: str, default: str) -> str:
    """优先级：进程环境（宿主显式传入）> 面板设置 > 默认值。"""
    return os.getenv(env_name) or str(_setting(setting_key) or default)


MEM_DSN = _value("MEM_DSN", "memDsn", "postgresql://postgres:postgres@127.0.0.1:5432/mem")
ZHISHU_DSN = _value("ZHISHU_DSN", "zhishuDsn", "postgresql://postgres:postgres@127.0.0.1:5432/zhishu")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "41012"))
DEFAULT_USER = _value("MEM_DEFAULT_USER", "memDefaultUser", "owner")
_KEY_FLAG = os.getenv("MEM_REQUIRE_KEY")
if _KEY_FLAG is None:
    _panel_require = _setting("memRequireKey")
    _KEY_FLAG = "1" if _panel_require is True else "0"
REQUIRE_KEY = _KEY_FLAG == "1"
# 实例身份：prod（线上库）/ local-dev（本地库）。出现在 /health、启动日志、控制台页头，
# 为的是两个实例并存时一眼能看出「我现在连的是哪个库」。
SERVICE_ENV = _value("DSH_ENV", "memEnv", "prod")
# 库标识（不含密码），只用来显示，例：127.0.0.1:41023/mem
DB_LABEL = _value("MEM_DB_LABEL", "memDbLabel", "")

# 文件型存储（audit / observations / issues）的根目录：默认 <repo>/data。
# 面板里的「文件型存储目录」对应 MEM_DATA_DIR；两个实例可以指到同一份数据。
_DATA_DIR = os.getenv("MEM_DATA_DIR") or str(_setting("memDataDir") or "")
DATA_DIR = Path(_DATA_DIR) if _DATA_DIR else BASE / "data"

# 审核令牌：/admin/review、/admin/supersede 用（原 .env 的 MEM_REVIEW_TOKEN）。
REVIEW_TOKEN = os.getenv("MEM_REVIEW_TOKEN") or str(_setting("memReviewToken") or "")
