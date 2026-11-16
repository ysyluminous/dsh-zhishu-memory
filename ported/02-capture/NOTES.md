# 02-capture

> 来源：`EternalNight996/memory-eternal` 的 `lib/` 与 `hooks/`（zip 解压，未取到 commit sha，版本按上游 `package.json` 记 0.49.11）。
> 纪律：**原样复制，不改造**；本文件只记录事实，读不懂的写「未核实」。

## 这个模块干什么

会话结束后的采集落地：写回链路（插件 → /ingest → app/service.py remember）。
将来接我们的：`dsh-zhishu-memory/app/service.py` 的 `remember()`（含 v1 的 pending 闸门）。

## 原样复制的文件

| 文件 | 来源 | 字节 |
|---|---|---|
| `capture.js` | `lib/capture.js` | 见文件 |
| `capture-run.js` | `lib/capture-run.js` | 见文件 |
| `capture-log.js` | `lib/capture-log.js` | 见文件 |
| `capture-target.js` | `lib/capture-target.js` | 见文件 |
| `claude-session.js` | `hooks/claude-session.js` | 见文件 |
| `capture.mjs` | `hooks/capture.mjs` | 见文件 |

## 已核实：`capture-session-id` **没有独立文件**

2026-11-16 全量扫 `sessionId|session_id`（排除 tests/bench）的结果：

```
51  index.js
19  lib/working-memory.js
12  lib/capture.js          ← 会话 id 逻辑的主体在这里
 8  lib/eval-longmemeval.js
 7  lib/mcp-http.js
 2  hooks/claude-session.js ← 采集钩子
 2  hooks/capture.mjs       ← 采集钩子
```

**所以原来「源仓库里没找到 capture-session-id.js（未核实）」这句作废**：
不存在独立文件，**逻辑在 `capture.js`（12 处）+ 两个 hooks 里**。

## 会话 id 的来源链（按上述扫描结果写；具体函数名未核实）

```
hooks/claude-session.js  ─┐
                          ├─→ 取出 session id ─→ lib/capture.js ─→ lib/capture-log.js（落日志）
hooks/capture.mjs        ─┘
```

- 两个钩子是「谁在什么时候触发采集」的入口；
- `capture.js` 是主干（12 处 session id 相关），`capture-log.js` 负责落日志；
- 这条链里**具体函数名、参数形状未核实**（只做了静态扫描，没跑起来）。

## 内部依赖（由 require/import 静态解析，未核实运行时行为）

- 见各组 `NOTES.md` 的逐条列表；本组以 `capture*.js` 互相引用为主，`hooks/*` 反向依赖 `lib/`。

## 我们仓库里**暂时没有**的依赖

- Node hooks 运行时（`hooks/claude-session.js`、`hooks/capture.mjs` 由 Claude/DSH 钩子机制触发，我们目前是 DSH 插件 + MCP，**钩子机制未核实是否等价**）。

## 额外需要移植的东西（未核实）

- 上游 CommonJS + Node 运行时，我们是 Python；`hooks/*` 依赖宿主的事件模型，**不能只搬文件**。

## 将来接到我们哪个文件

- `app/service.py` 的 `remember()`；会话 id 可作为 `meta.session_id`（**当前 mem.doc 没有该列，需要落 JSONL 或后续批红的列**）。
