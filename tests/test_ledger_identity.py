"""账本身份映射单测：不连数据库，只验证「同一账号两条身份并账、陌生身份各自成账」。

运行：python memory-mcp/tests/test_ledger_identity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import growth_log as growth  # noqa: E402

OWNER = "1858097717771890690"
ALIASES = {OWNER: "owner"}


def fake_lookup(user_id: str) -> str | None:
    return ALIASES.get(user_id)


def main() -> None:
    original = growth._ledger_lookup
    growth._ledger_lookup = fake_lookup
    try:
        # 账号主：网页登录 id 和旧 Key 的 owner 是同一本账
        assert growth._resolve_ledger(OWNER) == "owner"
        assert growth._resolve_ledger("owner") == "owner"
        # 陌生身份：没有映射就用自己，互相看不到
        assert growth._resolve_ledger("someone-else") == "someone-else"
    finally:
        growth._ledger_lookup = original

    print("PASS 3 cases")


if __name__ == "__main__":
    main()
