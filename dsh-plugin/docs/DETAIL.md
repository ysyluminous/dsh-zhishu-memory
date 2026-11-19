# 智枢记忆套件

一个 DSH bundle，四个 loader 行。2026-11-18 由四个独立插件目录合并而来：

| 原目录 | 现在 |
|---|---|
| `dsh-zhishu-memory` | 包根入口 `.`（记忆桥） |
| `dsh-ctx-guard` | `./ctx-guard` |
| `dsh-redline-guard` | `./redline` |
| `dsh-mem-float` | `./mem-float` + 包级 client（悬浮胶囊） |

装一次即可，四行在 Plugins 里各自启停；禁用某一行不影响其他三行。

## 1) 记忆桥（包根）

- `agent/pre-step` 第 1 步：把本轮用户输入 POST 到 `baseUrl`（默认 `http://127.0.0.1:41012`）的 `/gate`，服务返回相关决定/踩坑，追加到进入该步的消息里，打 `zhishu-memory` 来源标记。
- `agent/turn-stopping`：双写。原文（最多 8000 字）经 `/ingest` 进 `mem.conv`；摘要（结论/事实/证据指针，最多 300 字）经 `/remember` 进 `mem.doc`。每次写回带 `conversation_id`、`turn`、`raw_chars`、`digest_chars`；没写成的记 `write-back-skipped` 与原因。
- 上下文编排（`ctxPlan`）：按服务端 `/context/plan` 的“丢最老 N 轮”换算成一个 surface 区间。**默认只观测**；`MEM_CTX_SWAP=1` 才真的替换。编排默认走 `ctxBaseUrl`（默认 `http://127.0.0.1:41016`），不是 41012——生产 41012 上没有 `/context/plan`，指过去只会 404。
- 降级：`lib/degrade.js` 对空回答、超时、失败、限流分别给一句话，连续失败到阈值后节流。所有网络失败只写审计，绝不阻断回合。

配置：`baseUrl`、`limit`、`timeoutMs`（默认 10s）、`retries`、`writeBack`、`ctxPlan`、`ctxSwap`、`ctxThreshold`、`ctxKeepRecent`、`ctxBudget`、`ctxMaxMessages`、`ctxMaxChars`、`ctxTimeoutMs`、`ctxOverflowRecovery`、`ctxBaseUrl`。其中 `limit`、`timeoutMs`、`retries`、`writeBack`、`ctxThreshold`、`ctxKeepRecent` 可以在悬浮面板里拖滑杆改（见 §5）。

## 2) 上下文预算守卫（`./ctx-guard`）

`agent/pre-step` 里按 `noteEveryTurns` 追加一条极短提示（“能不能 3 个动作内解决 / 长内容写 mem.doc / 该压缩或开新会话”）。它**不裁剪历史、不改工具结果**，真正的压缩交给 `compaction-basic` 与 `tool-result-pruner`。

配置：`enabled`、`mode`（`smart` / `hard` / `off`）、`noteEveryTurns`、`noteMaxChars`、`logPath`。patch 里出厂值是 `noteEveryTurns: 8`；面板上改的是运行值，会覆盖出厂值。

**2026-11-18 修复（「一开插件就发不出消息」）**：`agent.inject()` 的签名是 `inject(message: UserMessage)`，要的是消息对象 `{ id, role, content, source }`，不是字符串（`dsh-agent/src/runtime-types.ts:271`、`dsh-llm/src/message.ts:246`）。旧代码传裸字符串，字符串被原样塞进 next-step 收件箱，下一个 `agent/pre-step` 里 `message.source.kind` 在 undefined 上取属性（`dsh-repeat-tool-reminder/src/index.ts:231`、`dsh-agent-loop/src/runtime-context.ts:24`），于是每一轮在第 2 步开头抛 `Cannot read properties of undefined (reading 'kind')`，turn 以 error 收尾。现在注入 `{ id: 'ctx-guard-*', role: 'user', content: [{type:'text',text}], source: { kind: 'ctx-guard' } }`，并有一条单测断言「注入的不是字符串、source.kind 存在」。

## 3) 红线守卫（`./redline`）

`tools/pre-execute`（waterfall）。命中规则就返回 `ask`，由审批服务弹卡；没人答（默认 120s）或环境不支持审批即拒绝。命中与决定写 `rules.json` 同目录的 `audit.jsonl`。

默认规则：`git push`、`npm/pnpm/yarn publish`、`docker push` / `compose up|down|rm` / `kubectl apply|delete` / `helm install|upgrade` / `flyctl deploy` / `vercel --prod`、递归删除、`DROP` / `TRUNCATE` / `DELETE FROM`、以及 `qqbot` 类对外发送工具。规则可用状态目录里的 `rules.json` 覆盖（`mode` 可设 `ask` 或 `deny`）。

**注意**：审批弹窗在无人值守的 bot 环境会被自动拒绝，这时红线命令会直接变成“用户拒绝”，这是预期行为，不是故障。

## 4) 记忆服务悬浮开关（`./mem-float` + client）

宿主侧在本机 `127.0.0.1:41033` 起一个小 HTTP 控制口：

- `GET /status` 探 `serviceUrl`（默认 `http://127.0.0.1:41012`）的 `/health`，顺带回 pid 与日志尾
- `POST /start` 用 `serviceDir/launch-mem-service.ps1` 把 Python 服务拉成独立后台进程，等 `/health` 真通才算成功
- `POST /stop` 结束监听该端口的进程
- `GET /settings` / `POST /settings` / `POST /settings/reset` 读写四行共用的参数（见 §5）

页面侧是 `shell.overlay` 上一个胶囊：显示已连接/未连接，点开看地址、延迟、pid、数据库、最近日志，可一键启停；下半部分是参数面板。

端口探测走 `execFile` 异步（不再用 `execSync` 在宿主主线程上跑 netstat），并且一次 `/status` 只探一次端口。

### 服务目录与 python 怎么解析（2026-11-19 修）

以前启动只认 `<serviceDir>/launch-mem-service.ps1`，而默认目录是 `~/.dsh/zhishu-memory/service` —— 没装过的人点「启动服务」必然报「找不到启动脚本」。现在：

1. **目录**：按 `MEM_SERVICE_DIR` → loader `serviceDir` → 设置 `memoryMcpDir` → `~/.dsh/zhishu-memory/service` → 包内 `service/` 依次找，取第一个**真含** `run.py`（或 `app/mcp_server.py`、`pyproject.toml`）的。
2. **python**：设置 `pythonBin` → `PYTHON` → `py -3` → `python` → `python3`，每条都问一次 `sys.executable`，拿**绝对路径**（PATH 里的 `python` 在宿主里常常不是用户装的那个）。
3. **启动**：有 `launch-mem-service.ps1` 就用它（尊重用户自己维护的日志/退出码）；没有就 `cd <dir> && PORT=<port> python run.py`，`cmd /c start /b` 脱离出去，然后等 `/health` 真通。
4. `/status` 会回 `serviceDir`、`serviceDirSearched`、`python`、`hasLauncher`，出问题时先看这四个字段。

实测（2026-11-19）：

```
/stop  → ok（pid 3776）
/start → ok started=True dir=D:\code\yunxiao\zhishu\memory-mcp  3.5 秒
/health → {"ok":true,"service":"zhishu-memory"}
```

另有一个隔离测试（一次性 USERPROFILE + 只有 `run.py` 的假服务）：10/10 通过 —— 目录解析、python 解析、无 launcher 启动、`PORT` 透传、二次 start 幂等、stop 正常。

配置：`port`、`serviceUrl`、`servicePort`、`serviceDir`、`launcher`、`python`、`pollMs`、`startTimeoutMs`。

## 5) 参数面板（滑杆）

面板下半部分是全部可调项 + 环境项，每项都有中文说明，写清「改它意味着什么」：预算自检间隔、自检提示字数上限、每轮注入记忆条数、单次请求超时、失败重试、上下文压力阈值、压缩保留最近轮数、收工写回记忆（开关）、预算自检开关、提示模式（`off` / `smart` / `hard`）。

**记忆服务的连接参数也在面板里**（`group: 'service'`，原来写在服务目录的 `.env`）：`memDsn`、`zhishuDsn`、`memDataDir`、`memReviewToken`、`memDefaultUser`、`memRequireKey`、`memDbLabel`、`memEnv`。改完重启服务即生效；`.env` 现在只是「脱离 DSH 单独跑服务」时的可选后备，服务自己会回落到面板设置。

- 存储：`~/.dsh/zhishu-memory/settings.json`（和 `audit.jsonl` 同目录）。删掉它 = 全部回到各行的出厂 `config`。
- 优先级：进程环境（mem-float 启动时传入）> 面板文件 > 代码里的 `default`；服务端 `config.py` 也按这个顺序读，所以 `.env` 没了也能连库。
- 生效方式：各行在**下一次用到参数时**按 mtime 重读，所以「保存参数」后下一轮就是新值，不用重启 DSH、不用改 profile YAML；`service` 组的项由 mem-float 在**启动服务时**翻译成环境变量，所以改完要重启一次服务。
- 越界值在写入时被夹回范围（例如 `timeoutMs` 上限 30 s）；未知键被忽略并在响应里列出来。
- 面板渲染包了一层错误边界：即使面板自己出错，也只显示一行错误提示，不会把整个页面根节点带崩。

## 6) 有效性体检（`tools/audit-report.mjs`）

只读脚本，吃插件自己留下的三份证据，输出一张记分卡并给出 FAIL/WARN 结论（FAIL 时退出码 1，可直接当 CI 断言）：

1. `~/.dsh/zhishu-memory/audit.jsonl` —— gate 命中/失败/耗时、写回成败与重试；
2. `~/.dsh/bot/workspace/logs/ctx-guard.jsonl` —— 预算自检注入次数与报错；
3. `~/.dsh/sessions/**/session.v4.jsonl.zstd` —— 真实会话日志里 `turn/end.reason.kind`。

为什么必须有第 3 层：前两层只证明「插件自己觉得没问题」，只有会话日志能证明「用户这一轮真的跑完了」。2026-11-18 那个 `Cannot read properties of undefined (reading 'kind')` 在前两层完全看不出来。

```powershell
# 判定看最近 24 小时，历史 7 天只作对照
node tools/audit-report.mjs
node tools/audit-report.mjs --hours 1 --days 3 --sessions 20
node tools/audit-report.mjs --json   # 给定时任务/看板
```

判定窗口刻意取「最近 N 小时」：老故障不该永远压着「现在好不好」。阈值：检索可用率 ≥90% PASS / ≥70% WARN；检索 P95 ≤3s PASS / ≤8s WARN；写回成功率 ≥95% PASS / ≥80% WARN；回合收尾错误率 ≤1% PASS / ≤5% WARN。

Windows 控制台若出现乱码，先 `chcp 65001` 再跑。

## 7) 工作台（胶囊 + 左侧面板 + 整页）

两种入口，同一份内容：

- **模态框**（右下角胶囊）：快速看一眼，六块内容都在，顶部有「打开工作台」跳到整页；
- **整页**（左侧栏「智枢记忆」面板，和「插件」「自动化任务」并列）：同一套页签铺满主区，宽度和留白按宿主入口页的规格来。

页面是宿主的两条正式扩展点：`sidebar.panellist` 注册侧栏图标（`id` 即面板 id），`main` 键控槽用同一个 id 注册整页；`ctx.layout.selectPanel(id)` 负责跳转。两者都没动宿主的会话界面，胶囊和模态框保持原样。

点胶囊 → 顶部页签，按需加载数据（没切过去的页签不发请求）：

| 页签 | 内容 |
|---|---|
| 概览 | 记忆服务 / pg16 / Qdrant 三张卡（各带延迟）、启停按钮、最近日志、最近检索与最近写回 |
| 配置 | 全部 22 个参数（连接 / 环境 / 调优 / 备份四组），外加「环境（一键安装）」：先是**预览**（不动任何东西），再是**执行**（需要点两次确认） |
| 会话 | **会话检索**：来源下拉（按 `mem.conv.source` 实时统计，如 豆包 / dsh / workbuddy）+ 时间（`2024` / `2024-10` / `2024-10-23`，也认 `24年10月`）+ 内容关键词；点一行展开消息。下方是最近的检索与写回记录（`audit.jsonl`） |
| MCP 服务 | **只在整页里有**（模态框不出现）：服务状态（`/health`）、MCP 地址、Key（生成/吊销，给别的工具用 `x-api-key` 连）、24h 内谁在调 MCP（客户端/版本/key 指纹/IP/耗时/成败）、审计文件位置，外加「打开控制台」。数据经 41033 转发到服务本体，浏览器不跨域 |
| 实体 | 直接读 `mem.doc`（path / kind / 标题 / 字数 / 更新时间），可搜索；点「修改」改标题、类型、正文并写回同一行 |
| 备份 | 上次备份时间、目录、最近几份的大小；参数「定时备份间隔 / 保留份数」；「立即备份」 |

### MCP 服务页的口径

- 数据来源：mem-float 的 `GET /mcp/status`（服务 `/health` + `/console/api/mcp`）、`GET /mcp/keys`、`POST /mcp/keys`、`POST /mcp/keys/revoke`，全部转发到 `serviceUrl`（41012）。
- 服务端本来就有的东西：`/console`（含 `#mcp` 审计页）、`/keys`、`/console/api/mcp`。本地模式（`MEM_REQUIRE_KEY=0`，插件自己托管）下没有企业登录态，Key 管理按 `MEM_DEFAULT_USER` 走；一旦开了 `REQUIRE_KEY` 就必须有真会话。
- Key 明文只在创建时返回一次；列表里只有指纹（`zm_` + 前 8 位）与最近使用时间。
- 换端口/换机器时，MCP 地址与「打开控制台」都从服务返回的 `endpoint` 推导，不写死。

### 会话检索的口径

- 接口：`GET /conversations/sources`、`GET /conversations?source=&from=&to=&q=&limit=&offset=`、`GET /conversations/messages?id=&limit=`（都在 mem-float 的 41033 上，只读）。
- 过滤：`source` 精确匹配 `mem.conv.source`；`from`/`to` 是 ISO 瞬时（客户端把「2024」换成**本地时区**的 2024-01-01 ~ 2025-01-01），比较 `coalesce(created_at, updated_at)`；`q` 同时匹配标题、snippet、`mem.conv_message.text`，以及未对账的 `mem.live_event.text`。
- 排序与分页：按会话开始时间倒序，一次 30 条（多取一条判断「还有」），下一页用 offset 追加。
- 命中片段：以关键词为中心取 220 字（关键词在长消息后半段也能看见），再压成单行；不输关键词时给首条消息的前 240 字做预览。
- 展开消息：`mem.conv_message` ∪ 未对账 `mem.live_event`，按 `(seq, created_at)` 排，正文用 base64 传输（`encode()` 会按 MIME 插换行，必须先去掉），单条截到 4000 字并标注。
- 性能实测（2026-11-19，13.6 万条消息）：豆包 + 2024 + 「报告」154 ms；不加来源/时间只搜关键词 4.9 s（全表 `ilike`，30 s 超时兜底）。

### 实体查看与修改的口径

- 读：`docker exec <pgContainer> psql -q -At -c "select … from mem.doc"`，只读，不经过记忆服务，所以快且不会触发检索逻辑。
- 写：`update mem.doc set title/kind/body, updated_at = now() where path = :'path'`；path 与 title 走 psql 变量替换（`:'path'`），正文走随机标签的美元引用，**都不是字符串拼接**。注入尝试实测返回 `changed=false` 且无副作用。
- **必须先加 `-q`**：不加的话 psql 会把 `UPDATE 0` 这种命令标签写到 stdout，被当成一行结果，于是「改一个不存在的 path」也报告成功（本轮实测踩到并修掉）。
- 边界：这是行级直改，不写 `mem.audit`、不重算 hash、不重建向量（本地 Qdrant 本来就是 0 集合）。要「带审计的更正」应走服务端的 `/remember` + `supersedes`，那是另一条路；本条在面板上如实说明了。

首次安装会自动弹出面板并停在「配置」：`setupDone` 默认 `false`，配好后在配置里把它打开，就不再来打扰。

三路探活的口径：记忆服务走 `GET /health`；pg16 是 **TCP 探活**（只证明端口在听，卡里写了这句）；Qdrant 走 `GET /collections` 并读每个集合的 `points_count`。

默认目标来自端口约定：本地 pg16 `127.0.0.1:41023`（库 `mem`），本地 Qdrant REST `127.0.0.1:41024`。2026-11-18 实测：记忆服务 4 ms、pg16 2 ms、Qdrant 1.20.2 通但 0 集合。

### 备份怎么做

- PostgreSQL：`docker exec <pgContainer> pg_dump -U <pgUser> --no-owner --no-acl <pgDatabase>`，**流式**边导边 gzip 落盘（大库不能读进内存：第一次实现用缓冲，直接撞 `ERR_CHILD_PROCESS_STDIO_MAXBUFFER`）。
- Qdrant：对每个集合 `POST /collections/{name}/snapshots`，把快照名记进 `qdrant.json`；没有集合就跳过。
- 落点：`~/.dsh/zhishu-memory/backups/run-<时间戳>/`，含 `pg.sql.gz`、`qdrant.json`、`report.json`；按「保留份数」只留最新 N 份。
- 定时：宿主每 10 分钟检查一次是否到点；间隔填 0 就只手动。
- 空跑不算成功：既没配 pg 容器、也没有 Qdrant 快照时 `ok=false`，避免第一次装上点一下就显示绿的。

实测（2026-11-18）：一份 `pg.sql.gz` 164 MB。

## 8) 公开发布（已改造完成，剩余两项需要人决定）

2026-11-18 完成的改造：

- `package.json`：去掉 `private`，补 `license: MIT`、`engines.dsh`、`publishConfig.access: public`、`keywords`、`./package.json` 导出；
- 新增 `LICENSE`（MIT）与 `README.md`（英文主文 + 中文速览，含「数据去哪了」「怎么关」）；
- **去掉全部机器硬编码**：`cordis.patch.yml` 里不再有盘符路径；`ctx-guard` 的日志默认落 `~/.dsh/zhishu-memory/ctx-guard.jsonl`；`mem-float` 的 `serviceDir` 默认 `~/.dsh/zhishu-memory/service`，`python`/`launcher` 留空自动推导；
- 一键安装补了 `memoryMcpRepo`：没有本地目录时 `git clone --depth 1` 到服务目录再 `pip install -e`，返回值里用 `complete` 区分「没报错」和「装完了」；
- 新增 `tools/pack-check.mjs`：不发网络、不改文件的发布自检（52 项）。

自检证据（2026-11-18）：

| 项 | 结果 |
|---|---|
| `pack-check` | **52/52 通过**（含 repository 已按真实 remote 声明） |
| 全新 profile 组合 | `dsh --profile probe --dump-config` 在一次 DSH_HOME 里组合出四行，输出无盘符路径 |
| `npm pack` | **24 个文件 / 69 KB**（解包 205 KB），没有多余文件 |
| 装进干净目录 | `npm install <tarball>` → 24 个文件，能按包名 `require.resolve` |
| 入口可加载 | 从装好的副本 `import`：`index.js`、`ctx-guard`、`redline`、`mem-float` 全部导出 `apply` |
| 干净机器空跑 | 21/21（一次性 USERPROFILE：默认值无绝对路径、`/setup` 不带 confirm 被拒、`/backup` 空跑降级不抛错） |

社区开放市场 [dshopen.org](https://dshopen.org/) 的收录要求：**公开源码 + 可安装 npm 版本 + 明确许可证 + 自己的 Logo + 完善描述**，提交走 [plugin-add 表单](https://github.com/polang233/dsh-plugin-index/issues/new?template=plugin-add.yml)。

还差三件事，都需要人或凭据：

1. **仓库要公开**：`repository` 已按真实 remote 指向 `github.com/ysyluminous/dsh-zhishu-memory`（`directory: dsh-plugin`），但匿名访问 GitHub API 仍返回 404——说明它现在是私有或未创建，而市场要求公开源码。
2. **把 `dsh-plugin/` 提交并推上去**：目前该目录在上游仓库里**没有被 git 跟踪**（`git ls-files dsh-plugin` 为 0）。
3. **npm 包名归属**：包名是 `@asywr/...`，GitHub 账号是 `ysyluminous`；要么在 npm 上有 `@asywr` 组织，要么改成 `@ysyluminous/dsh-zhishu-memory` 或无 scope 名。

发布动作（缺一项都会被挡）：

```bash
npm publish --access public            # 或先 npm pack 验证打包内容
dsh plugin --profile <干净profile> add @asywr/dsh-zhishu-memory
```

### 一键装环境（预览 → 执行）

「配置 → 环境（一键安装）」先给**预览**（只算命令，不动任何东西），再点两次确认才执行。步骤与实测（2026-11-18）：

| 步 | 做什么 | 实测 |
|---|---|---|
| `pg` | `docker run … pgvector/pgvector:pg16`；已有容器则 `docker start` | ok |
| `pg-db` | 查 `pg_database`，没有就 `createdb <库>` | ok（`已存在`） |
| `pg-vector` | `create extension if not exists vector`；**失败只记说明，不拦整步** | 你现有的 `postgres:16` 没有 pgvector → optional 失败 |
| `qdrant` | `docker run … qdrant/qdrant` / `docker start` | ok |
| `memory` | 有目录就 `pip install -e`；只有 Git 地址就 `git clone --depth 1` + 安装 | 未配置 → skipped |
| `memory-start` | 拉起记忆服务并等 `/health` 真通；已在跑则记「已在运行」 | ok（`已在运行`） |

三个刻意的设计：

- 镜像默认用 **`pgvector/pgvector:pg16`**，不用官方 `postgres:16`：官方镜像没有 `vector` 扩展，别人装完记忆服务会连不上。
- **建库单列一步**：`docker run` 出来的容器只有 `postgres` 库，而记忆服务要 `mem`。"容器起来了但服务连不上"是别人装机最容易踩的坑，所以显式查 + 建。
- **装完必须真的拉起来**：只装不起等于半个功能，所以末尾再等一次 `/health`；已经通着就不重复起进程。
- 返回值用 `complete` 区分「没报错」和「装完了」：记忆服务没装成时 `ok=true, complete=false`。

### Docker 不能只靠 PATH

DSH 宿主进程的 PATH 常和用户终端不一致（Docker Desktop 装在 DSH 之后，只改了用户 PATH）。实测宿主里 `docker --version` 直接 `ENOENT`，而同一用户在终端里一切正常，结果整个一键安装显示"docker 不可用"。

现在按顺序解析：`DOCKER_BIN` → `Program Files/Docker/Docker/resources/bin/docker.exe` → `ProgramData/DockerDesktop/version-bin/docker.exe` → PATH，解析一次后复用。备份的 `pg_dump`、实体的 `psql` 都走同一个解析结果。

### 备份（已实测清理逻辑）

- 落点 `~/.dsh/zhishu-memory/backups/run-<时间戳>/`，含 `pg.sql.gz`、`qdrant.json`、`report.json`。
- 实测：一份 `pg.sql.gz` **157–164 MB**（16.5 秒）；把保留份数设成 1 再跑一次，返回 `pruned=3`，目录里只剩最新一份。**所以默认保留 7 份在这台机器上会占 ≈1 GB，建议 3 份。**
- Qdrant 侧按集合建 snapshot；本地 0 集合时记为"跳过"，不算失败。

### 三条可复跑的自检

```bash
node tools/verify.mjs                  # 一把过三道闸门（下面三条）
node tools/pack-check.mjs              # 53 项：manifest / exports / locale / 图标 / 盘符 / 入口语法
node tools/fresh-profile.mjs           # 一次性 DSH_HOME 里链接后组合出四行
node tools/fresh-profile.mjs --install # 真 npm pack + `dsh plugin add` 装一遍，再组合
node tools/audit-report.mjs            # 运行期体检：检索、写回、回合收尾、注入
```

2026-11-18 实测 `node tools/verify.mjs`：**3.1 秒全过**

```
PASS  pack-check  0.6s            （53/53）
PASS  fresh-profile (link)  0.3s
PASS  fresh-profile (install)  2.0s
```

`fresh-profile.mjs` 需要 `DSH_NODE_EXECUTABLE` 与 `DSH_INSTALL_DIR`（在 DSH 会话里本来就有）。它**不启动任何服务**：只让 `dsh --profile probe --dump-config` 组合配置并退出，断言四行都在、输出里没有盘符路径。

2026-11-18 实测（`--install` 模式，3 秒）：

```
npm pack → dsh plugin --profile probe add <tarball>（pnpm，1 个包）
        → dsh --profile probe --dump-config
✓ 打包并安装后，全新 profile 组合出 4 行，无盘符路径
```

同一个工具里踩过并修掉的三件事，都写进注释了：Node 不能直接 spawn `npm.cmd`；把整条命令拼成字符串交给 `cmd /c` 会让引号多一层、npm 把路径当相对路径；Electron-as-node 下 `console.log` 之后的 `process.exit()` 偶尔会把最后一行吞掉（改用文件重定向或用参数数组绕开）。

> 注意：安装模式只证明「装得上、四行能组合」；**真正挂载（activation）**需要完整 profile + 凭据，那一步只能在有账号的机器上做。桌面 profile 里的四行是 active 的，`plugin-loaded` 与工作台接口都在实时应答。

## 9) 内置汇集（collect）

**目标**：插件自己就能把本机各 AI 工具的会话与记忆**增量**同步进记忆库，不依赖 omnihist、不调外部
CLI、不上传别处。别人装完插件、填好服务地址，打开开关就能定时跑。

### 模块

```
lib/collect/
  index.js            注册表 + detect/scan/run + 并发锁 + 上传 + 审计
  state.js            增量状态（~/.dsh/zhishu-memory/collect-state.json）
  util.js             文本抽取/截断/哈希/目录遍历/jsonl 增量读
  adapters/
    dsh.js            <home>/.dsh/sessions/**/session.v4.jsonl.zstd
    workbuddy.js      <home>/.workbuddy/projects/**/*.jsonl + MEMORY.md/SOUL.md/USER.md
    claude-code.js    <home>/.claude/projects/**/*.jsonl
    codex.js          <home>/.codex/**/*.jsonl（能解析就收，解析不出就跳过）
    markdown.js       记忆文档助手：找 MEMORY/SOUL/USER.md、按内容哈希增量
```

适配器是纯对象：`{ id, label, detect(env), scan(state, env) }`。`scan` 返回
`{ sessions, docs, next, pending, skipped }`，`sessions[i]` 形如
`{ externalId, title, cwd, createdAt, messages: [{ seq, role, kind, text, time }] }`。

### 配置项（`lib/settings.js`，group `collect`）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `collectEnabled` | bool | **false** | 总开关。默认关：没配服务地址前不该自己跑。关掉即停定时器。 |
| `collectEveryMinutes` | number 0–1440 | `60` | 汇集间隔；`0` = 只在手动点「立即汇集」时跑。 |
| `collectTools` | text | `dsh,workbuddy,claude-code,codex` | 参与汇集的工具，逗号分隔；未知 id 忽略。 |

面板参数区会把三项一起渲染（`group: 'collect'`）；工作台里还**没有**独立的「汇集」页签，
`GET /collect/status` 已经能直接给红绿灯，页签留到下一轮。

### 调度与控制口（mem-float 宿主）

- 启动 **60 秒后首查**，之后每 **10 分钟**看一次是否到点；开关关着或间隔为 0 时什么都不做。
- `lastRun` 取所选工具里最新的一次，到了间隔才真跑；**单次运行加锁**，定时器与手动点同时来只有一个真跑。
- 失败只写 `audit.jsonl`（`{ event:'collect', tool, sessions, messages, ok, ms }`）并打一行日志，不冒泡、不崩宿主。
- 手动接口不受 `collectEnabled` 限制（开关只决定定时器跑不跑）。

```
GET  /collect/status   → { ok, busy, serviceUrl, statePath, tools: [{ id, label, enabled, found,
                           root, localSessions, syncedSessions, syncedMessages, syncedDocs,
                           pending, lastRun, lastError }] }
POST /collect/run      → { tools?: string[] } 立刻跑一次，返回 { ok, results, messages, docs, sessions, pending, ms }
```

两条都在服务不可达时**优雅降级**：返回 `{ ok:false, error }`，HTTP 仍是 200，不抛。

### 增量口径

- **jsonl**（WorkBuddy / Claude Code / Codex）：按字节 `offset` 续读，只处理以 `\n` 结尾的完整行；
  半截行留到下一轮。文件变小 / offset 超过 size → 视为重写，从 0 重读。
- **DSH zstd**：没变且已读完才跳过；变了整文件解。**DSH 的 `.zstd` 是一串追加帧，不是一个帧**——
  Node 的 `zstdDecompressSync` / `createZstdDecompress` 只解第一帧（实测 7.8 MB 文件只解出 `session` 头 1 行），
  所以按 magic `28 b5 2f fd` 切帧逐帧解；坏帧跳过、后面的帧继续。
- **Markdown**：`mtime+size` 相同直接跳过；变了再算 `sha256`，内容没变也不重传。
- 状态只在**上传全部成功**后前进；失败保留旧 offset，下一轮重发，服务端按 `event_id` 去重。

### 落库口径

- 会话消息 → `POST {serviceUrl}/ingest`
  - `source` = 适配器 id（`dsh` / `workbuddy` / `claude-code` / `codex`）
  - `conversation_id` = `"<adapter>:<externalId>"`
  - `role` ∈ user / assistant / tool / system；`title` = 会话标题
  - `event_id = sha256("<adapter>|<session>|<seq>|<role>")`，重跑安全
  - `meta.via = 'dsh-zhishu-memory-collect'`，带 `tool/session/seq/time`
- Markdown 文档 → `POST {serviceUrl}/remember`
  - `kind: 'doc'`、`title: <绝对路径>`、`text: 正文`（最多 20 万字符）
- 服务地址：`readSettings().serviceUrl`（当前设置表里还没有这个键）→ mem-float 的 `cfg.serviceUrl`
  （默认 `http://127.0.0.1:41012`）。运行前先探一次 `/health`，不可达直接返回，不会逐条超时。

### 边界（刻意这么定）

- **单文件 > 64 MB 整文件跳过**（记 `skipped`）；本机一个 238 MB 的 WorkBuddy 日志实测被跳过。
- **单次每工具 ≤ 2000 条、总计 ≤ 20000 条消息**，超出留到下一轮；文档单独限 200 份/轮。
- 只取**文本段**：reasoning / thinking / tool_use / function_call 参数 / 图片一律不收；单条截断 8000 字。
- DSH 里插件注入的召回 / 上下文守卫 user 消息（`source.kind` 命中 memory-recall / ctx-guard / zhishu-memory）跳过，
  否则每轮都会把召回快照当用户发言重复写进 `mem.conv`。
- Codex 本地日志版本差异大，解析不出就跳过，不报错。
- 纯读本机文件、纯本机 HTTP，不联外网；没有新增依赖（只用 `node:fs/os/path/zlib/crypto/http`）。
- 状态文件：`~/.dsh/zhishu-memory/collect-state.json`；审计与现有四行共用 `audit.jsonl`。

### 自检

```bash
node tools/collect-selftest.mjs   # 一次性 USERPROFILE + 假服务，32 项
```

覆盖：2 个 WorkBuddy jsonl + 1 个多帧 DSH zstd + 1 个 MEMORY.md → 会话/消息/文档条数 →
第二次 0 新增 → 完整 run 的 `/ingest` `/remember` 口径（7 条消息 + 1 份文档、event_id 唯一）→
第二次不重传 → 追加一行只同步 1 条 → 单次上限（limit=2 后下一轮补 1 条）→ 状态红绿灯 →
并发第二次被锁住 → 服务不可达时优雅降级并写 `audit.jsonl`。

只读实测（本机真实数据，2026-11-19）：DSH 52 个会话、WorkBuddy 276 个、Claude Code 7 个、
Codex 1 个（无可解析消息）；WorkBuddy 有 5 个 >64 MB 文件被跳过；九份 MEMORY/SOUL/USER.md 被识别为文档。
`limit=400` 时每工具严格只出 400 条，`pending` 记 1292 / 364，下一轮继续。

## 已验证 / 未验证

已验证（2026-11-18 22:00，临时 USERPROFILE，没碰真实 `~/.dsh`）：五个文件的 `node --check`；25 项冒烟测试全过 —— 出厂 config 作默认、文件覆盖、越界夹回、未知键忽略、小数不残留浮点尘、`reset` 回到出厂；`ctx-guard` 注入的是对象且 `role: 'user'` / `content` 有 text 段 / `source.kind` 存在；`mem-float` 的 `GET/POST /settings`、`/settings/reset`、`/status` 走真实 HTTP 全通。

未验证：浏览器里的悬浮胶囊和面板外观需要打开页面看（本机没有可用的 React 渲染环境，只做了组件契约级检查）；红线审批的真实弹卡要在有审批通道的会话里试；记忆桥的 gate/写回依赖 41012 与 41016 的可用性。

## 回退

- 只停一项：Plugins 里禁用对应行（`zhishu-memory` / `ctx-guard` / `redline-guard` / `mem-float`）。
- 整包回退：卸载本 bundle，profile 里原来四个 `link:` 依赖仍在，但它们从未被挂载，卸载不会有残留影响。
