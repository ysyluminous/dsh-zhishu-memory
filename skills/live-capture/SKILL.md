---
name: live-capture
description: Mirror each user prompt and final answer into the personal memory service as it happens. Use in AI tools that support MCP but have no turn-boundary hook.
---

# Live capture

Use this when the host tool has the `zhishu-memory` MCP server but no automatic
prompt/answer hook.

Before doing any work on a user message, call `gate`:

- `source`: the host tool name, lowercase (`chatgpt`, `claude`, `cursor`, ...)
- `conversation_id`: stable id for this chat; if the host has none, use
  `<source>:<session-id>` or a UUID you keep for the conversation
- `query`: the user's message verbatim
- `role`: `user`

Use the returned `context` as reference; ignore irrelevant results. `gate` also records the prompt, so do not call `ingest_event` for the user message separately.

After the final answer is ready, call `ingest_event` with `role: assistant` and
the final answer text. It writes synchronously in milliseconds; if it fails,
continue anyway.

Rules:

- One call per user turn and one per completed assistant turn.
- Do not capture system prompts, tool output, or hidden reasoning.
- Keep the same `conversation_id` for the whole conversation.
- The call is idempotent; repeating it with the same text is safe.
- For streaming answers, capture only the final text, not each token.

If the host has native hooks, prefer the hook adapter; this skill is the
fallback for MCP-only hosts.
