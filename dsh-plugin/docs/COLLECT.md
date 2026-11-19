# 内置汇集（collect）设计

目标：**插件自己就能把本机各 AI 工具的会话与记忆增量同步进记忆库**，不依赖 omnihist、不调用外部 CLI，
别人装完插件、填好服务地址，定时任务就跑起来。

## 1. 为什么不能调 omnihist

omnihist 是作者本机的独立项目（`~/.omnihist/omnihist.db` + CLI）。任何没装它的人，插件都不该报错或变哑。
所以只借它的**口径**（每个工具的本地存储长什么样、怎么认会话/消息），代码全部内置。

## 2. 模块

```
lib/collect/
  index.js     注册表 + detect/scan/run + 并发与限流 + 跳过已存在
  pg.js        本地直写（docker exec psql + COPY）与 mem 里的已有条数
  state.js     增量状态读写（~/.dsh/zhishu-memory/collect-state.json）
  adapters/
    dsh.js           DSH：<home>/.dsh/sessions/<workspace>/<session>/session.v4.jsonl.zstd
    workbuddy.js     WorkBuddy：<home>/.workbuddy/projects/**/*.jsonl + MEMORY.md/SOUL.md/USER.md
    claude-code.js   Claude Code：<home>/.claude/projects/**/*.jsonl
    codex.js         Codex：<home>/.codex/**/*.jsonl（有就收，没有就跳过）
    markdown.js      任意 memory 目录下的 *.md → 文档入 mem
```

适配器契约（纯对象，无 class）：

```js
export default {
  id: 'workbuddy',
  label: 'WorkBuddy',
  /** 探测本机是否存在该工具的数据；返回 { found, root, sessions } —— sessions 是本地会话数（用于红绿灯） */
  detect(env) {},
  /** 增量扫描；返回 { sessions: [...], next: { ..., files: {...} } } */
  scan(state, env) {},
}
```

`sessions[i]`：`{ externalId, title, cwd, createdAt, messages: [{ seq, role, kind, text, time }] }`
`role` ∈ `user|assistant|tool|system`；`text` 只取文本段（不带 reasoning），单条截断 8000 字。

会话 id：`<adapter id>:<externalId>`。workbuddy 的 `externalId` 是
`<sessionId>-<sha256(文件绝对路径)[0:8]>`——mem 里 omnihist 导入的同一批会话就是这个口径，
两边必须一致，否则「跳过已存在」永远命中不了，同一批会话会在 mem 里再建一套。

## 3. 增量状态

`~/.dsh/zhishu-memory/collect-state.json`：

```json
{ "version": 1, "tools": { "workbuddy": { "files": { "<abs path>": { "mtimeMs": 1, "size": 2, "offset": 3, "lastId": "..." } }, "lastRun": "...", "synced": 123 } } }
```

- jsonl：按 `offset` 续读，只解析新行；文件变小或 mtime 回退 → 整文件重读。
- `.zstd`（DSH）：按 mtime+size 跳过未变文件；变了就整文件解（Node 24 自带 `zlib.createZstdDecompress`）。
- 单次运行上限：每工具最多 500 万条消息、总 1000 万条；超出留到下一轮。

## 4. 落库

两条路，按「有没有本地库」自动选：

### 4.1 本地直写（首选，有 `pgContainer` 且 docker 可用）

`docker exec psql` 的 COPY，一次批量写 `mem.conv_message`：

- 文本走 COPY 的 base64 列（`convert_from(decode(text_b64,'base64'),'UTF8')`），NUL 先剥离（PG 的 text 存不了 `\u0000`）；
- `omni_id = ('x' || substr(md5(conversation_id || ':' || seq), 1, 14))::bit(56)::bigint`，与 `tools/omnihist-migrate.mjs` 同一条公式；
- 幂等键 `(conversation_id, seq, content_hash)`：同 seq 只有一行。同 seq 不同 hash、且那一行是汇集自己写的 → 就地更新；那一行是别的导入方（`omni_id` 是 sqlite rowid 量级）→ 不动、不新增（计 `mismatch`，因为 seq 口径不同，按 seq 追加会把历史写乱）；
- 一批一个事务（≤8 MB），失败整批回滚；状态不前进，下一轮重放，幂等键保证不重复。

### 4.2 跳过已存在（只有在本地直写可用时才判）

写之前一次查出本次所有会话在 mem 里的条数（`conv_message` + 未对账的 `live_event`），
`mem 条数 ≥ 本地条数` 就只推进状态、不发任何请求。

### 4.3 回落 `/ingest`

拿不到本地库时才逐条 `POST {serviceUrl}/ingest`（`source: <adapter id>`、`conversation_id: "<id>:<externalId>"`、`role`、`text`、`event_id = sha256("<adapter>|<session>|<seq>|<role>")`）。服务端按 `event_id` 去重，重跑安全。

Markdown 记忆文件始终走 `POST {serviceUrl}/remember`（`kind: 'doc'`、`title: 路径`、`text: 正文`）。
本地库可用而服务不可达时，消息照写、文档留到下一轮（`documents` 状态不推进）。
审计：`audit.jsonl` 写 `{ event: 'collect', tool, sessions, messages, docs, ok, ms, mode, existing, mismatch }`。

## 5. 调度与配置

- 设置项（`lib/settings.js`，group `collect`）：
  - `collectEnabled`（bool，默认 **false**：别人没配服务前不该自己跑）
  - `collectEveryMinutes`（number 0–1440，默认 60；0 = 只在手动点）
  - `collectTools`（text，默认 `dsh,workbuddy,claude-code,codex`）
- 本地直写复用 `setup` 组的 `pgContainer` / `pgUser` / `pgDatabase`（工作台 → 配置 → pg16 容器名）；
  留空即只走 `/ingest`。
- 定时器放在 mem-float 宿主：启动后 60 秒首跑，之后每 10 分钟检查一次是否到点（沿用 backups 的做法）。关掉开关即停。
- 单次运行加锁，禁止并发。

## 6. 控制口（mem-float）

- `GET  /collect/status` → 每个工具 `{ id, label, found, localSessions, syncedSessions, lastRun, pending }`（红绿灯用）
- `POST /collect/run` `{ tools?: string[] }` → 立刻跑一次，返回 `{ results: [...] }`
- 这两条必须在服务不可达时**优雅降级**（返回 `ok:false, error`），不能抛。

## 7. 验收

- `tools/collect-selftest.mjs`：一次性 USERPROFILE 造假数据（2 个 jsonl + 1 个 DSH zstd）→ scan 得到预期条数 → 再跑一次得 0 新增 → 状态文件正确；workbuddy 会话 id 带文件哈希后缀。
- 本地直写（手工 / 集成）：临时会话 + 真库 → 服务不可达也能写、`omni_id`/`content_hash` 口径正确、重跑 0 新增 0 重复、追加只补差额、别人的行判 `mismatch` 不追加。
- 实测（2026-11-19）：workbuddy 276 个会话、状态丢失需全量重扫时 6.3 s 全绿、0 条写入、`mem.conv_message` 行数与 `(conversation_id, seq)` 重复数均不变；正常状态点「同步」259 ms；`node tools/omnihist-migrate.mjs` 合计待补 0。
- `npm run verify` 必须仍全绿（pack-check + 两种安装模式）。
- 全部 `node --check` 通过；无新增依赖。
