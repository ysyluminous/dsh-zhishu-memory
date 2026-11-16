"""Key 自助的纯逻辑测试：不连数据库，只验校验与格式化。

运行：python memory-mcp/tests/test_keys.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.keys import (  # noqa: E402
    KEY_PREFIX,
    clean_name,
    fingerprint,
    revoke_key,
    session_user,
)


class FakeRequest:
    def __init__(self, cookie: str = "", path: str = "/keys") -> None:
        self.headers = {"cookie": cookie}
        self.method = "GET"
        self.url = type("U", (), {"path": path})()


def check_format() -> None:
    digest = "a" * 64
    assert fingerprint(digest) == KEY_PREFIX + "a" * 8, fingerprint(digest)
    assert fingerprint(digest).startswith("zm_")
    assert len(fingerprint(digest)) == 11


def check_names() -> None:
    assert clean_name("  ") == "默认设备"
    assert clean_name(None) == "默认设备"
    assert clean_name("  MacBook   Air ") == "MacBook Air"
    assert len(clean_name("x" * 80)) == 40


def check_revoke_rejects_bad_id() -> None:
    assert revoke_key("owner", "") is False
    assert revoke_key("owner", "not-a-hash") is False
    assert revoke_key("owner", "a" * 63) is False


def check_no_cookie_401() -> None:
    result = asyncio.run(session_user(FakeRequest()))
    assert result.get("status") == 401, result
    assert "未登录" in result.get("error", ""), result


def main() -> None:
    cases = [check_format, check_names, check_revoke_rejects_bad_id, check_no_cookie_401]
    for case in cases:
        case()
        print(f"PASS {case.__name__}")
    print(f"PASS {len(cases)} cases")


if __name__ == "__main__":
    main()
