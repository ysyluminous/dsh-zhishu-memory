"""临时本地验收：起一个假的 OpenAI 兼容端点，跑通「规则失败 -> 模型兜底 -> 确认落库」。

跑完自己清理写进去的行。只在本地库跑，不碰线上。
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app import growth_llm  # noqa: E402
from app.config import ZHISHU_DSN  # noqa: E402
from app.growth_log import growth_day, growth_log, growth_parse  # noqa: E402

USER = "nightly-llm-check"
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)

ANSWER = {
    "kind": "purchase",
    "title": "客户那顿饭",
    "amount": "两百六",
    "capital": "relation",
    "start": (NOW - timedelta(days=1)).replace(hour=18, minute=30, second=0).isoformat(),
    "end": None,
    "planned": False,
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        ok = body.get("model") == "fake-e2e"
        content = json.dumps(ANSWER, ensure_ascii=False) if ok else "{}"
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


def cleanup() -> int:
    with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute("delete from growth.time_entry where user_id = %s", (USER,))
            deleted = cur.rowcount
        c.commit()
    return deleted


def main() -> None:
    cleanup()  # 以前跑残的
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    growth_llm.LLM_BASE = f"http://127.0.0.1:{port}/v1"
    growth_llm.LLM_MODEL = "fake-e2e"
    growth_llm.LLM_KEY = ""
    try:
        # 1) 不配端点时，模型路径完全跳过
        growth_llm.LLM_BASE = ""
        growth_llm.LLM_MODEL = ""
        r = growth_parse("客户那顿饭 两百六")
        assert r["parser"] == "todo" and r["recognized"] is False, r
        print("1) 无模型端点：退回待办 PASS")

        # 2) 配上端点：解析出 llm，金额 260，不落库
        growth_llm.LLM_BASE = f"http://127.0.0.1:{port}/v1"
        growth_llm.LLM_MODEL = "fake-e2e"
        r = growth_parse("客户那顿饭 两百六")
        assert r["parser"] == "llm", r
        assert r["draft"]["amount"] == 260.0 and r["draft"]["capital"] == "relation", r
        with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("select count(*) from growth.time_entry where user_id = %s", (USER,))
                assert cur.fetchone()[0] == 0
        print("2) 模型兜底出草稿、未落库 PASS")

        # 3) confirm 才落库，字段与草稿一致
        d = growth_log(USER, "客户那顿饭 两百六", confirm=False)
        assert d["mode"] == "draft" and d["parser"] == "llm", d
        s = growth_log(USER, "客户那顿饭 两百六", confirm=True)
        assert s["mode"] == "saved" and s["parser"] == "llm", s
        assert s["entry"]["amount"] == 260.0, s
        with psycopg.connect(ZHISHU_DSN, connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute(
                    """select kind, title, amount::float8, capital, source, status,
                              to_char(actual_start at time zone 'Asia/Shanghai','YYYY-MM-DD HH24:MI')
                       from growth.time_entry where user_id = %s""",
                    (USER,),
                )
                rows = cur.fetchall()
        assert len(rows) == 1, rows
        kind, title, amount, capital, source, status, started = rows[0]
        assert (kind, title, amount, capital, source) == ("purchase", "客户那顿饭", 260.0, "relation", "ai")
        print(f"3) 确认落库 PASS  {kind}/{title}/{amount}/{capital}/{source}/{status}/{started}")

        # 4) 按发生日能读到这条（假模型给的是昨天 18:30）
        day = growth_day(USER, (NOW - timedelta(days=1)).strftime("%Y-%m-%d"))
        assert day["count"] == 1 and day["items"][0]["amount"] == 260.0, day
        print("4) growth_day 读回 PASS")

        # 5) 模型挂了也不丢输入：退回待办并保留原文
        growth_llm.LLM_MODEL = "wrong-model"  # 假端点对别的模型返回 400
        t = growth_log(USER, "客户那顿饭 两百六", confirm=False)
        assert t["mode"] == "stored_todo" and t["parser"] == "todo", t
        assert t["entry"]["note"] == "客户那顿饭 两百六"
        print("5) 模型失败退回待办、原文保留 PASS")
    finally:
        srv.shutdown()
        removed = cleanup()
        print(f"cleanup: 删除测试行 {removed} 条")


if __name__ == "__main__":
    main()
