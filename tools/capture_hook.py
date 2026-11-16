"""Universal turn-boundary capture hook.

Reads the JSON a CLI hook sends on stdin, extracts the user prompt or the final
assistant answer, and posts it to the memory service immediately.

    python tools/capture_hook.py --source claude-code
    python tools/capture_hook.py --source cursor --print

Supported shapes: Claude Code, Codex, Cursor, Gemini CLI. Unknown hooks are
ignored. It never blocks the tool: on any error it exits 0 (unless --strict).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

INGEST = os.getenv("MEM_INGEST_URL", "http://127.0.0.1:8770/ingest")
TIMEOUT = float(os.getenv("MEM_INGEST_TIMEOUT", "6"))
LOG = Path(os.path.expanduser("~")) / ".ai-native-capture" / "errors.log"

USER_EVENTS = {"userpromptsubmit", "beforesubmitprompt", "beforeagent", "user_prompt_submit"}
ASSISTANT_EVENTS = {"stop", "afteragent", "afteragentresponse", "agentresponse",
                    "agent-turn-complete", "agent_turn_complete"}


def log_error(text: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {text}\n")
    except OSError:
        pass


def as_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(as_text(v) for v in value)
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if key in value:
                return as_text(value[key])
    return ""


def pick(event: dict, *keys: str) -> str:
    for key in keys:
        if key in event and event[key] not in (None, ""):
            return as_text(event[key])
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--url", default=INGEST)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--gate", action="store_true", help="retrieve context before the model answers")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    raw = sys.stdin.read()
    if not raw.strip():
        return
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        log_error(f"bad json: {raw[:200]}")
        return

    name = str(event.get("hook_event_name") or event.get("hook_event") or
               event.get("event") or event.get("type") or "").strip().lower()
    normalized = name.replace("-", "").replace("_", "").replace(".", "")

    role = None
    text = ""
    if normalized in USER_EVENTS:
        role = "user"
        text = pick(event, "prompt", "text", "message")
    elif normalized in ASSISTANT_EVENTS:
        role = "assistant"
        text = pick(event, "last_assistant_message", "last-assistant-message",
                    "prompt_response", "text", "response")
    if not role or not text.strip():
        return

    session = pick(event, "session_id", "sessionId", "conversation_id",
                   "conversationId", "thread_id")
    if not session:
        seed = pick(event, "transcript_path", "cwd") or "adhoc"
        session = "adhoc-" + hashlib.sha256(seed.encode()).hexdigest()[:16]
    conversation_id = session if ":" in session else f"{args.source}:{session}"

    title = pick(event, "session_title", "sessionTitle", "title")
    if not title:
        cwd = pick(event, "cwd")
        title = os.path.basename(cwd.rstrip("\\/")) if cwd else ""

    created = pick(event, "timestamp", "created_at", "time")
    payload = {
        "source": args.source,
        "conversation_id": conversation_id,
        "role": role,
        "text": text,
        "title": title,
        "created_at": created,
        "meta": {"hook": name, "cwd": pick(event, "cwd"),
                 "model": pick(event, "model"), "raw_keys": sorted(event)[:20]},
    }
    if args.print:
        print(json.dumps(payload, ensure_ascii=False)[:1000])

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_payload = payload
    target = args.url
    if args.gate and role == "user":
        target = args.url.rsplit("/", 1)[0] + "/gate"
        request_payload = {"source": args.source, "conversation_id": conversation_id,
                           "query": text, "role": role, "limit": 4}
        data = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        target, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            log_error(f"ingest rejected: {body}")
        elif args.gate and role == "user" and body.get("context"):
            context = body["context"]
            if args.print:
                print(context[:1000])
            if args.source in {"claude-code", "codex", "gemini-cli"}:
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": event.get("hook_event_name") or name,
                        "additionalContext": context,
                    }
                }, ensure_ascii=False))
            # Cursor/Codex variants that cannot inject context still keep the trace.
    except Exception as e:  # never block the host tool
        log_error(f"capture failed: {e}")
        if args.strict:
            raise


if __name__ == "__main__":
    main()
