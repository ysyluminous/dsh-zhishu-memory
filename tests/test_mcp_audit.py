"""MCP 审计的纯逻辑测试：不连数据库，只验错误判定、脱敏、读写与坏行容错。

运行：python tests/test_mcp_audit.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="mcp-audit-")
os.environ["MEM_AUDIT_DIR"] = _TMP
os.environ["MEM_MCP_AUDIT"] = "1"

from fastmcp.tools.base import ToolResult  # noqa: E402

from app import mcp_audit  # noqa: E402


def check_verdict() -> None:
    assert mcp_audit._verdict(ToolResult(structured_content={"ok": True}))[0] is True
    ok, error = mcp_audit._verdict(ToolResult(structured_content={"ok": False, "error": "坏 key"}))
    assert ok is False and error == "坏 key", (ok, error)
    ok, error = mcp_audit._verdict(ToolResult(structured_content={"error": "库不可用"}))
    assert ok is False and error == "库不可用", (ok, error)


def check_redact() -> None:
    text = mcp_audit._args_summary({"query": "上次的方案", "api_key": "zm_secret", "limit": 3})
    assert "zm_secret" not in text, text
    assert "api_key=***" in text, text
    assert "query=" in text and "limit=3" in text, text


def check_roundtrip() -> None:
    # 先放一行坏数据：读的时候跳过，但不能让整份日志不可用。
    with open(mcp_audit.AUDIT_FILE, "a", encoding="utf-8") as fh:
        fh.write("这不是 JSON\n")
    mcp_audit.record({"k": "session", "c": "WorkBuddy", "cv": "5.11.3",
                      "u": "owner", "kf": "", "ip": "127.0.0.1", "ok": True, "d": 3})
    mcp_audit.record({"k": "tool", "c": "WorkBuddy", "tool": "search",
                      "args": 'query="x"', "ok": False, "e": "boom", "d": 12})
    events, scan = mcp_audit.load_events()
    assert len(events) == 2, events
    assert scan["scanned"] == 3 and scan["kept"] == 2, scan
    assert events[-1]["tool"] == "search" and events[-1]["ok"] is False
    assert mcp_audit.load_events(limit=1)[0][0]["k"] == "tool"
    assert mcp_audit.audit_info()["bytes"] > 0


def main() -> None:
    cases = [check_verdict, check_redact, check_roundtrip]
    try:
        for case in cases:
            case()
            print(f"PASS {case.__name__}")
        print(f"PASS {len(cases)} cases")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
