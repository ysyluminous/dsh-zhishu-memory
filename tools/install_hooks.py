"""Merge the turn-capture hook into a tool's config, with a backup.

    python tools/install_hooks.py --tool codex --dry-run
    python tools/install_hooks.py --tool codex

Supported: claude-code, codex, cursor, gemini-cli.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
SCRIPT = Path(__file__).resolve().parent / "capture_hook.py"
CMD = f'python "{SCRIPT}" --source {{source}}'

TARGETS = {
    "claude-code": HOME / ".claude" / "settings.json",
    "codex": HOME / ".codex" / "hooks.json",
    "cursor": HOME / ".cursor" / "hooks.json",
    "gemini-cli": HOME / ".gemini" / "settings.json",
}
EVENTS = {
    "claude-code": ("UserPromptSubmit", "Stop"),
    "codex": ("UserPromptSubmit", "Stop"),
    "cursor": ("beforeSubmitPrompt", "afterAgentResponse"),
    "gemini-cli": ("BeforeAgent", "AfterAgent"),
}


def hook_entry(tool: str) -> dict:
    return {"type": "command", "command": CMD.format(source=tool), "timeout": 5}


def merge(doc: dict, tool: str) -> dict:
    doc.setdefault("hooks", {})
    nested = tool in {"claude-code", "codex", "gemini-cli"}
    for event in EVENTS[tool]:
        items = doc["hooks"].setdefault(event, [])
        if nested:
            already = any(
                any("capture_hook.py" in str(h.get("command", "")) for h in (item.get("hooks") or []))
                for item in items
            )
            if not already:
                items.append({"hooks": [hook_entry(tool)]})
        else:
            already = any("capture_hook.py" in str(item.get("command", "")) for item in items)
            if not already:
                items.append({"command": CMD.format(source=tool)})
    if tool == "cursor":
        doc.setdefault("version", 1)
    return doc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", required=True, choices=sorted(TARGETS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = TARGETS[args.tool]
    doc = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"{path} is not valid JSON: {e}")
            sys.exit(1)
    merged = merge(doc, args.tool)
    text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    if args.dry_run:
        print(f"--- {path} ---")
        print(text[:2000])
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + f".bak-{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(path, backup)
        print(f"backup {backup}")
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"updated {path}")
    if args.tool == "codex":
        print("Codex: open /hooks once to review and trust the new hook.")


if __name__ == "__main__":
    main()
