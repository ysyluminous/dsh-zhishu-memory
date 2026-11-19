# 把记忆服务挂到 WorkBuddy / 其他 MCP 客户端

记忆服务本身就是 MCP server：`POST http://127.0.0.1:41012/mcp`（Streamable HTTP）。
WorkBuddy 5.x 支持 `"type": "streamableHttp"` 的远程 MCP，所以**不需要 stdio 桥**，直接在
`~/.workbuddy/mcp.json` 里加一条即可。

## 1. WorkBuddy：最简配置（当前未强制 key）

编辑 `~/.workbuddy/mcp.json`（WorkBuddy 里也可以走「插件 → MCP 服务器 → 配置 MCP」）：

```json
{
  "mcpServers": {
    "zhishu-memory": {
      "type": "streamableHttp",
      "url": "http://127.0.0.1:41012/mcp",
      "disabled": false
    }
  }
}
```

保存后**重启 WorkBuddy**（或在 MCP 面板里重连）。它会先发 `initialize`，再 `tools/list`，
之后模型就能调用 `search` / `gate` / `remember` / `stats` / `whoami` 等工具。

说明：

- 服务当前 `MEM_REQUIRE_KEY=0`，不传 key 也会落到 `MEM_DEFAULT_USER`（默认 `owner`），
  和 DSH 插件读的是同一份个人记忆；因此**不加 key 也能共享查询/写入**。
- 客户端名和版本会出现在控制台的「MCP 服务」页，能分清是 WorkBuddy 还是别的客户端。
- WorkBuddy 第一次调用某个 MCP 工具时可能弹确认，选择允许/信任即可。

## 2. 开了鉴权之后：带 key

服务端设 `MEM_REQUIRE_KEY=1` 后，必须带 `x-api-key`。先生成一把只给 WorkBuddy 用的 key：

```bash
python tools/make_key.py --user owner --name WorkBuddy
```

命令只打印一次明文 key，存进 WorkBuddy 配置：

```json
{
  "mcpServers": {
    "zhishu-memory": {
      "type": "streamableHttp",
      "url": "http://127.0.0.1:41012/mcp",
      "headers": { "x-api-key": "zm_..." },
      "disabled": false
    }
  }
}
```

key 存在 `mem.api_key`，服务只存 sha256；审计日志里只出现 `zm_` + 前 8 位指纹。

## 3. 验证连通

先用 curl 走一遍握手（不需要 key 时去掉 `x-api-key`）：

```bash
curl -sS -X POST http://127.0.0.1:41012/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'x-api-key: zm_...' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0.1"}}}'
```

返回里有 `serverInfo.name = zhishu-memory` 就说明通了。接着用响应头里的 `mcp-session-id`
发 `tools/list` / `tools/call`，或直接看控制台。

## 4. 控制台「MCP 服务」页

地址：`http://127.0.0.1:41012/console#mcp`

能回答：

- **谁连上来了**：客户端名/版本、用户、key 指纹、IP、首连/最后活动、连接次数、调用次数、错误数、最近工具；
- **连接日志**：每次 `initialize` / `tools/list` / `tools/call` 的时间、耗时、成功/失败、参数摘要、UA、会话 id；
- **接入片段**：页面里直接给出当前端点对应的 `mcp.json`，复制即用。

实现口径：

- 审计写 `logs/mcp-audit.jsonl`（8MB × 3 轮转），**不写库**——库挂的时候这一页仍然能看；
- 失败也记：工具把错误包成正常结果（`{"error": ...}` / `{"ok": false}`）时，服务端会再判一次；
- 不记 key 原文、不记完整大文本：参数摘要截断并对 `token/secret/password` 等字段打码。

## 5. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| WorkBuddy 里看不到工具 | 改完 `mcp.json` 没重启；或 MCP 面板里该 server 处于 disabled |
| 连接 404 / 405 | 地址写成 `http://127.0.0.1:41012` 了，要带 `/mcp` |
| `无效或缺少 API key` | 服务开了 `MEM_REQUIRE_KEY=1` 但没带 key，或 key 不在 `mem.api_key` |
| 能连上但 `search` 没结果 | 查的是 `personal` 且该用户名下没有文档；用 `whoami` 确认身份 |
| 想确认是不是 WorkBuddy 在调 | 控制台「MCP 服务」→ 客户端列，看 client/version 与 UA |
