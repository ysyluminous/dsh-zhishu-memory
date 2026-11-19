# dsh-zhishu-memory

> 一个给 AI agent 用的**长期记忆服务**：MCP 协议 + REST 镜像，PostgreSQL 存储，按 API key 做用户隔离。
> 目标不是"能存能搜"，而是**记得住、查得回、写得进、改不了历史**。

## 为什么会有这个项目

AI 越用越碎：同一个结论散落在聊天记录、文档、代码注释里；换个会话就要重讲一遍；讲完了还没人保证它下次能被找回来。

这个项目把这些"碎"收成一处：**一个库、一套接口、一份可追溯的写入历史**，让不同的 agent（DSH / Claude Code / Codex / Cursor / 任意 MCP 客户端）共享同一份记忆。

## 当前能力

| 工具 | 作用 |
|---|---|
| `search(query, limit, scope)` | `personal` = 个人文档与会话，`public` = 内容库/X/协调数据，`auto` = 两者合并 |
| `gate(query)` | 回答前的强制闸门：先取回相关上下文，再允许作答 |
| `remember(text, kind, title, topics)` | 写入一条记忆/决策，按用户隔离 |
| `stats()` / `whoami()` | 计数与身份；未知指标返回 `null`，绝不编数 |
| `idea_*` / `todo_*` / `ledger_*` | 想法池、待办、账本 |
| `related(node_id, depth)` | 关联节点 |
| `consume(metric, amount)` | 配额扣减（search / ai_chat / download 等） |

REST 镜像：`/search`、`/remember`、`/stats`、`/whoami`、`/health`。
MCP 入口：`POST /mcp`。

任意 MCP 客户端（DSH / WorkBuddy / Claude Code / Codex / Cursor）都可以直接连 `POST /mcp`
（Streamable HTTP，不需要 stdio 桥）。WorkBuddy 的 `mcp.json` 写法、带 key 的配置和排错见
[`docs/mcp-clients.md`](docs/mcp-clients.md)。

控制台：`http://127.0.0.1:41012/console`。除了会话 / 上下文 / 门控 / 记忆，
`#mcp` 页回答「谁连上来了、调了什么、成没成」——客户端名/版本、key 指纹、IP、
每次 `initialize` / `tools/call` 的耗时与结果，审计写本机 JSONL，库不可用时也能看。

## 架构

```
agent（DSH / Claude Code / Cursor / 任意 MCP 客户端）
        │  MCP over HTTP
        ▼
   memory-mcp（FastAPI，单进程，无状态）
        │
        ▼
   PostgreSQL（schema `mem`）
     mem.doc        文档/记忆块（path 即身份，天然幂等）
     mem.conv       会话原文
     mem.edge/entity 图谱
     mem.idea/todo/ledger/queue  想法、待办、账本、入库队列
```

**几个刻意的选择：**

- **path 即状态**：`mem.doc.path` 就是唯一键，写入用 `on conflict (path) do update` 幂等，重复写不会产生两份；
- **单进程无状态**：服务本身不存业务状态，重启即恢复，所有状态在库里；
- **失败要响**：连接类错误返回 503 且 `retryable: true`，参数错返回 400，冲突 409，全部经过脱敏，不把上游错误正文透出去；
- **检索有闸门**：`gate` 是"答之前必须先查"，不是一个可选项。

## 快速开始

```bash
git clone <this repo>
cd dsh-zhishu-memory
python -m pip install -r requirements.txt
cp .env.example .env      # 改成本地库连接
python run.py             # 默认监听 127.0.0.1，MCP 在 /mcp
```

`.env` 关键项（**示例值，真实值只写在本机**）：

```ini
# 记忆库（本项目的独占库）
MEM_DSN=postgresql://postgres:change-me@127.0.0.1:5432/mem
# 宿主业务库（只读用途）
ZHISHU_DSN=postgresql://postgres:change-me@127.0.0.1:5432/zhishu
HOST=127.0.0.1
PORT=8770
MEM_DEFAULT_USER=owner
```

初始化表结构：

```bash
psql "$MEM_DSN" -f schema-lifecycle.sql
psql "$MEM_DSN" -f schema-ideas-todos-ledger.sql
# 其余 schema-*.sql 按需
```

## 路线图（按"先修正确性、再修成本、最后修追溯"排序）

- **v1 · review-gated 写入**（进行中）：`remember` 默认写入待审区（`path` 含 `/pending/`），**检索默认排除待审区**；`review` 做三项检查——去重（同主题近期是否冲突）、出处（会话 id / 文件:行号 / 命令原文）、`kind` 白名单。通过才移入正式区。**核心价值：没审过的结论永远进不了检索，库里不会越写越脏。**
- **v2 · token ledger**：每次 `gate` / `search` / `remember` 记 token 用量，与模型调用账本拼成一本账，让"记忆的成本"可见。
- **v3 · immutable audit**：哈希链 append-only 审计，能回答"这条结论是什么时候、由谁、基于什么写进来的"。

## 设计原则

1. **不编数**：没有数据源的字段返回 `null` 或空，绝不模拟。
2. **宁缺不猜**：缺字段就留空，不用启发式填。
3. **失败要能定位**：日志带 `request_id` 与拒绝原因，区分"服务不可用"与"你没权限"。
4. **密钥不进库、不进日志、不进仓库**：只从环境变量读。

## 许可

MIT（待定，见 `LICENSE`）。
