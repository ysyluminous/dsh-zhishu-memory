# 记忆 MCP 与业务 MCP 的分界 + 渐进式合并路线

> 2026-11-19 定案：**两个 MCP 各自独立**，一个叫 `zhishu-memory`，一个叫 `zhishu`。
> 本文是执行清单，按 ROI 排序，每一段都能单独收口、单独回滚。

## 1. 定名与边界（先把这个钉死）

| 名字 | 仓库 / 目录 | 端口 | 职责 | 谁连 |
|---|---|---|---|---|
| **`zhishu-memory`** | `D:\code\dsh-zhishu-memory`（`ysyluminous/dsh-zhishu-memory`） | 41012 | 记忆：`search` / `remember` / `gate` / `conv_*` / `idea` / `todo` / `ledger` / `queue` / `growth`；控制台 `/console`、`/keys`、`#mcp` 审计 | DSH 插件、WorkBuddy、任意 MCP 客户端（带 key） |
| **`zhishu`** | `D:\code\yunxiao\zhishu`（`ysyluminous/zhishu`） | 8765 | 业务：文章 / TCM / CRM 检索（`search` / `get_document` / `chat` / `freshness`） | 业务侧、需要业务数据的 agent |
| ~~`memory-mcp`~~ | `D:\code\yunxiao\zhishu\memory-mcp` | 41012（现状） | **不再是独立身份**：它的定位是「`zhishu-memory` 的实现来源之一」，合并完成后归档 | — |

护栏：

- 两个 README 的第一行分别写清「这是 `zhishu-memory`，记忆 MCP」和「这是 `zhishu`，业务 MCP」；
- DSH 插件设置里的 `memoryMcpDir` **只允许**指向 `D:\code\dsh-zhishu-memory`（S4 切换之后）；
- WorkBuddy 的 `~/.workbuddy/mcp.json` 里那条 `zhishu-memory` 指向 `zhishu-memory` 的端口，不再指向业务仓；
- 业务功能（文章/TCM/CRM）**永远不进** `zhishu-memory`，记忆功能也**永远不进** `zhishu`。

## 2. 现状（2026-11-19 实测）

- 41012 上跑的是 `D:\code\yunxiao\zhishu\memory-mcp`（pid 40512，13:48:25 启动，`serviceDir` 由 mem-float 起）。
- WorkBuddy 的 `mcp.json` → `http://127.0.0.1:41012/mcp`，和 DSH 插件**共用同一个进程**。
- `D:\code\dsh-zhishu-memory` 是 `zhishu-memory` 的家：插件包 `dsh-plugin/` + 一份自洽的记忆服务（`service.py` 1411 行）+ **已经写好但没在跑**的控制台：
  - `app/console.py` / `console.html`：`/console` + `/console/api/*`（overview / convs / conv / ctx / gate / docs / doc / quiz / **mcp**）
  - `app/mcp_audit.py`：客户端名/版本、key 指纹、IP、`initialize` / `tools/call` 耗时与结果，落本机 JSONL
  - `app/keys.py` + `mcp_server.py`：`GET/POST /keys`、`POST /keys/revoke`
- 冒烟（临时 41112 端口、本机库）：`/health` 200、`/console` 200（30,760 B）、`/console/api/mcp` 200。控制台与审计**不依赖库**，库不可用也能看。
- 能力差距：`memory-mcp` 的引擎更新（`conv_search`、`_snippet_around` / `_anchor` / `_match_info`、review gate、observation、supersede、quality、visibility、history、core），`zhishu-memory` 目前没有。

## 3. 目标形态

```
DSH 插件（mem-float）
  ├─ 启停 / 探活 / 设置            →  zhishu-memory  41012
  ├─ 工作台「MCP 服务」页签        →  状态、Key、审计（调 /health、/keys、/console/api/mcp）
  └─ 「打开控制台」                →  http://127.0.0.1:41012/console
        │
        ▼
  zhishu-memory（D:\code\dsh-zhishu-memory）── PostgreSQL: mem
        ▲
        │ 任意 MCP 客户端（WorkBuddy / Claude Code / Cursor）带 key 连 41012/mcp

  zhishu（D:\code\yunxiao\zhishu\search，8765）—— 业务检索，与记忆无关
```

## 4. ROI 排序的执行阶段

| # | 阶段 | 做什么 | 为什么（ROI） | 成本 | 风险 | 验收 |
|---|---|---|---|---|---|---|
| S0 | 定名/边界（**已完成 2026-11-19**） | 本文档；README 首行定名；41112 冒烟证明 `zhishu-memory` 能独立跑 | 零风险、立刻消除“改错地方” | 0.5h | 无 | `/health` `/console` `/console/api/mcp` 三个 200 |
| S1 | 检索能力回迁（**已完成 2026-11-19**） | `conv_search` + `_conv_terms` / `_snippet_around` / `_anchor` / `_attach_contexts` 已进 `app/service.py`，MCP 工具 + `GET /conversations/search` 已注册，`tests/test_conv_search_contract.py` 3/3 | 你点名的功能；以新增函数为主，改动面小 | 0.5–1d | 低 | 契约测试 3/3；`GET /conversations/search?q=报告` 返回带 anchor 的命中 |
| S2 | 插件「MCP 服务」页签（**已完成 2026-11-19**） | 整页新增「MCP 服务」页（状态 / MCP 地址 / Key 生成吊销 / 24h 调用审计 / 打开控制台）；mem-float 新增 `/mcp/status`、`/mcp/keys`、`/mcp/keys/revoke` 转发；本地模式放行 Key 管理 | 你列的“装上就能在设置里启动/看状态/加 key/看日志” | 1d | 低 | 代理联调 8/8（生成→列表→吊销→列表消失）；页面渲染 13/13 |
| S3 | 引擎对齐（**已完成 2026-11-19**） | 把 `memory-mcp` 的引擎整块接进 `zhishu-memory`：`service.py` + `core/history/issues/observation/projects/quality/supersede/visibility/todo_store/growth_*` + `mcp_server.py`（46 个工具、全部 REST 路由），再重新挂回 DSH 自己的 `McpAuditMiddleware` / `register_console` / `register_context`，并保留本地模式 Key 放行。文件型存储收敛到 `config.DATA_DIR`（默认 `<repo>/data`，`MEM_DATA_DIR` 可指向业务仓 `data/`） | 行为与现在一致，才敢切 | 2–3d（实际 1 轮） | 中 | 见下：REST 5/5 与文件型 7/7 对齐 |
| S4 | 切换（**已完成 2026-11-19**） | `memoryMcpDir` → `D:\code\dsh-zhishu-memory`；建 `zhishu-memory/.env`（沿用原 DSN + `MEM_DATA_DIR=D:\code\yunxiao\zhishu\data` + `MEM_DB_LABEL`）；停/起 41012；WorkBuddy 地址不变 | 单一记忆 MCP，名字与仓库一致 | 0.5d | 中 | `/console` 200、`/keys` 200、`/console/api/mcp` 200、46 工具、`/conversations/search` 200；面板 `/mcp/status`、`/mcp/keys` 都有数据 |
| S5 | 收尾 | ① 把 `data/`（audit/observations/issues）迁进 `zhishu-memory` 并去掉 `MEM_DATA_DIR`；② 本机 `zhishu-mem-pg` 补 trgm 索引（本机库 personal 检索现在会超时）；③ `history.verify()` 的断点（第 541 行）由账号主定怎么修；④ 备份/回滚演练 | 可持续维护 | 1–2d | 低 | `tools/verify.mjs` 全绿；回滚演练一次；本机库检索 <1s |
| S6 | 发布（让“别人装了就能用”） | 把今天这些改动提交 + 推到 `ysyluminous/dsh-zhishu-memory`（含 `dsh-plugin/`，它目前还是 untracked）；确认一键安装默认 Git 地址能拉到含本版服务的仓库 | 别人装插件 → 一键安装 → 自动启动 | 0.5d | 低 | 全新 profile/新机器上：装插件 → 一键装环境 → 胶囊绿灯、MCP 页可用 |

### 为什么是这个顺序

1. **S0/S1 的收益/成本最高**：立即可见（控制台已经能开、功能是你点名的），且不动引擎，失败也只影响新增的几个函数。
2. **S2 排在 S3 前**：插件页签只依赖已有的 `/keys`、`/console/api/mcp`，先做能让“日常使用”先成立，同时给 S3 提供观察窗（能看谁在调、调得对不对）。
3. **S3 是最贵的一段**：它决定切过去之后记忆行为是否等价，必须单独一段时间、带回归，不接受“顺手合并”。
4. **S4 必须等 S3 绿**：切换本身只是改两个配置 + 换端口，但它是不可逆的用户可见动作，前置条件必须全部满足。

## 5. 下一步（可直接开工）

- ~~先做 S1~~（已完成）：`conv_search` 一组函数 + 契约测试。
- ~~再做 S2~~（已完成）：工作台「MCP 服务」页签 + mem-float 代理。
- ~~S3 引擎对齐~~（已完成）：同一个远端库、同一个 `MEM_DATA_DIR` 下，`/search`、`/conversations/search`、`/stats`、`/issues`、`/issues?status=done` **5/5 字节级一致**；`core.listing` / `observation.active` / `quality.feedback_stats` / `supersede.superseded_ids` / `history.verify` / `service.pending_summary` **7/7 可读**。
- **S4 切换**：改 `memoryMcpDir` + WorkBuddy `mcp.json`，给服务的环境加 `MEM_DATA_DIR=D:\code\yunxiao\zhishu\data`（文件型存储仍在业务仓），重启 41012；切换后 MCP 页的 Key/审计即对 `zhishu-memory` 生效。
- S5：把 `data/` 从业务仓迁进 `zhishu-memory`（`MEM_DATA_DIR` 去掉），再跑回归 + 回滚演练。

## 5.1 S3 期间发现的两件事

- **`history.verify()` 报 `ok:false`（第一条坏在第 541 行，`cloud/owner/20261117-bc7…`）**：这是**既有数据**的哈希链断点（2026-11-17 那条被追加/改动过），不是这次移植引入的。S5 迁移数据前要先决定：补一条 rehash 记录，还是把断点原位修掉（需要账号主定，因为它是审计链）。
- **文件型存储默认在业务仓**：`core/history/observation/quality/supersede/issues` 的数据在 `D:\code\yunxiao\zhishu\data`（`parents[2]` 是业务仓根）。`zhishu-memory` 里已改成 `config.DATA_DIR`，默认 `<repo>/data`，`MEM_DATA_DIR` 覆盖；S4 切换时必须显式设这个环境变量，否则会读到空数据。

## 6. 回滚

- S1/S2 纯新增：删新增函数/页签即可，不影响现有启动路径。
- S4 只需把 `memoryMcpDir` 与 WorkBuddy `mcp.json` 改回 `memory-mcp`，重启 41012；`memory-mcp` 在归档前不要动其代码。
