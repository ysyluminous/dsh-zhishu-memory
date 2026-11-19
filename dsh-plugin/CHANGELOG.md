# 更新记录

## 未发布

### 修

- **`tools/omnihist-migrate.mjs` 的去重探针不再空值崩溃**：`--source` 指向没有会话的来源、或空库里跑全量时，探针仍会进分支、拿 `undefined` 去查 `message` 表，`Math.min(3, 0)` 还会把去重模式误判成 `hash`（下一步 INSERT 就可能撞主键）。现在 sample 为空直接按 `seq` 去重；mem 有这个会话但 omnihist 侧没有消息时也按 `seq`；整套探测包了 try/catch，任何一步读不到都回落 `seq` 而不是整场失败。顺手把 `--source` 传空串归一成「全量」，避免 `where` 和参数不一致。

### 加

- **切换记忆服务到 `zhishu-memory`（S4）**：`memoryMcpDir` 改指 `D:\code\dsh-zhishu-memory`，服务自带 `/console`、`/keys`、`#mcp` 审计；WorkBuddy 与插件都还是 41012，一行客户端配置没改。实测 `/health`、`/console`、`/keys`、`/console/api/mcp`、`/conversations/search` 全 200，工具数 46。
- **装上就能用（别人的体验）**：① `memoryMcpRepo` 默认指向公开仓库，新机器「一键装环境」的 memory 步开箱可用，不用先找 Git 地址；② mem-float 装载 8 秒后自动探一次——目录在、服务没跑、用户本次没手动停过，就自动拉起（只启动，不 clone、不 pip install；手动点过「停止」后不再自动起）。
- **`zhishu-memory` 记忆引擎对齐（S3）**：把 `memory-mcp` 的引擎整块接入 `D:\code\dsh-zhishu-memory\app`（`service.py` + core/history/issues/observation/projects/quality/supersede/visibility + todo_store/growth），46 个 MCP 工具与全部 REST 路由齐活，同时保留 DSH 自己的控制台、`#mcp` 审计、上下文编排与本地模式 Key 放行；文件型存储收敛到 `config.DATA_DIR`（`MEM_DATA_DIR` 可覆盖）。同一个库 + 同一个数据目录下，`/search`、`/conversations/search`、`/stats`、`/issues` 与线上 `memory-mcp` **字节级一致**。
- **整页工作台新增「MCP 服务」页签**（只在整页里，模态框不出现）：服务状态、MCP 地址、Key 生成/吊销（给别人 `x-api-key` 连）、24h 内谁在调 MCP（客户端 / 版本 / key 指纹 / IP / 耗时 / 成败）、审计文件位置，以及「打开控制台」。数据由 mem-float 新增的 `GET /mcp/status`、`GET/POST /mcp/keys`、`POST /mcp/keys/revoke` 转发到 41012，浏览器不跨域。
- **`conv_search` 回迁到 `zhishu-memory`**（S1）：从 `memory-mcp` 带回 `conv_search` + `_conv_terms` / `_snippet_around` / `_anchor` / `_attach_contexts`，MCP 工具与 `GET /conversations/search` 都注册好；带契约测试。同时把 Key 管理在**本地模式**（`MEM_REQUIRE_KEY=0`）下放行给默认账号、`PUBLIC_MCP_URL` 默认跟随 `PORT`。
- **左侧面板 + 整页工作台**：插件现在在宿主左侧导航注册一个「智枢记忆」面板（`sidebar.panellist`），点开是铺满主区的整页工作台（`main` 键控槽，同一个 id），页签仍是概览 / 配置 / 会话 / 实体 / 汇集 / 备份，但按宿主入口页的宽度和留白排版。模态框保持原样，只多了一个「打开工作台」直接跳过去——模态框负责快速看一眼，整页负责操作。两处共用同一个 `WorkbenchBody` 组件，功能不会写两遍；`ctx.layout.selectPanel(id)` 负责跳转，面板没注册时降级成什么都不做而不是把界面打白。
- **工作台的「会话」页签能搜会话了**：来源下拉（按 `mem.conv.source` 实时统计，如 豆包 / dsh / workbuddy）、时间（`2024` / `2024-10` / `2024-10-23`，也认 `24年10月`；按会话开始时间、本地时区）、内容关键词（标题 + 摘要 + 正文 + 未对账 live_event）；结果按时间倒序分页，点一行就地展开消息（`conv_message` ∪ 未对账 `live_event`，正文截到 4000 字）。新接口 `GET /conversations/sources|/conversations|/conversations/messages`，只读、30 s 超时。原「最近的检索 / 写回」列表保留在下方。实测 13.6 万条消息：豆包 + 2024 + 「报告」154 ms；纯关键词全表扫 4.9 s。
- **内置汇集支持「本地直写」**：配了 `pgContainer` 且 docker 可用时，会话消息改走 `docker exec psql` 的 COPY 批量写 `mem.conv_message`（文本 base64、`omni_id = md5(conversation_id:seq)` 前 56 位、按 `(conversation_id, seq, content_hash)` 幂等），不再逐条 `POST /ingest`——服务端单条约 127ms，实测 workbuddy 首轮 23 分钟那一路就是它拖的。会话行一并补 `snippet` / `hash`（与 `/ingest` 同口径）并把 `message_count` 对齐到物理行数。拿不到本地库（没配容器 / docker 不可用 / 容器没起 / mem 表缺失）才回落 `/ingest`；本地库可用而记忆服务不可达时，消息照写、文档留到下一轮（状态也不推进 `documents`）。
- **汇集支持「跳过已存在」**：本地直写前先一次查出本次所有会话在 mem 里的消息数（`conv_message` + 未对账的 `live_event`），`mem ≥ 本地条数` 就只推进状态、不发任何写请求。实测 workbuddy 276 个会话在「状态丢失、要全量重扫」的情况下 6.3s 全绿、0 条写入、0 重复行；正常状态点「同步」259ms。
- **workbuddy 会话 id 与 mem/migrate 口径对齐**：mem 里 omnihist 导入的 workbuddy 会话，`external_id` 是 `<sessionId>-<sha256(文件绝对路径)[0:8]>`（omnihist 用文件哈希给同名 sessionId 去重）。汇集的 `externalId` 现在也带这个后缀，否则「跳过已存在」永远命中不了，同一批会话会在 mem 里再建一套。
- **别人的行不按 seq 追加**：同一个会话如果 mem 里的行不是汇集写的（`omni_id` 是 sqlite rowid 量级，说明是 omnihist 按自己的去重/过滤口径导入的），seq 编号对不上，按 seq 追加会把历史写成乱序 + 内容重复。这种会话计为 `mismatch`、不写、写进 `audit.jsonl` 与 `collect-state.json`；只有「mem 里没有」或「mem 里的行是汇集自己写的」才增量追加。

## 2.1.0 — 2026-11-18

公开发布改造（本地 `link:` 安装一直在用 2.0.0；这次是第一个「别人也能装」的版本）。

### 修

- **修掉「找不到启动脚本」**：以前启动只认 `<serviceDir>/launch-mem-service.ps1`，而默认目录是 `~/.dsh/zhishu-memory/service`——没装过的人点「启动服务」必然报这条。现在改为：先在候选目录里找真正含 `run.py`（或 `app/mcp_server.py`、`pyproject.toml`）的目录（`MEM_SERVICE_DIR` → loader `serviceDir` → 设置里的 `memoryMcpDir` → `~/.dsh/zhishu-memory/service` → 包内 `service/`），再解析真实 python（设置 → `PYTHON` → `py -3` → `python` → `python3`，用 `sys.executable` 拿绝对路径）；有 launcher 就用，没有就 `cd <dir> && PORT=<port> python run.py`，照样等到 `/health` 真通。`/status` 会显示解析到的目录、python 和有无 launcher。
- **没有 Docker 不再整场放弃**：一键安装只跳过容器相关步，记忆服务与 python 照做。
- **修掉「一开插件就发不出消息」**：`ctx-guard` 把裸字符串传给 `agent.inject()`，而 DSH 要的是 `UserMessage`。字符串进收件箱后，下一轮 `pre-step` 里任何一处 `message.source.kind` 都会在 `undefined` 上取属性，turn 以 `Cannot read properties of undefined (reading 'kind')` 收尾。现在注入 `{ id, role: 'user', content, source: { kind: 'ctx-guard' } }`。
- **修掉「宿主找不到 docker」**：`docker` 只在用户终端的 PATH 里（Docker Desktop 装在 DSH 之后），宿主进程里 `docker --version` 直接 ENOENT，于是整个一键安装显示成"docker 不可用"。现在先试 `DOCKER_BIN`，再试 Windows 常见安装路径（`Program Files/Docker/Docker/resources/bin`、`ProgramData/DockerDesktop/version-bin`），最后才回落到 PATH，并只解析一次。
- `mem-float` 的端口探测从 `execSync` 改成 `execFile` 异步：不再在宿主主线程上同步阻塞。
- `/status` 一次请求只探一次端口（原来三目两边写的是同一个表达式，跑了两遍）。

### 加

- **内置汇集（collect）**：插件自己按增量把本机 **DSH / WorkBuddy / Claude Code / Codex** 的会话与 `MEMORY.md` / `SOUL.md` / `USER.md` 同步进记忆库（会话走 `POST /ingest`、文档走 `POST /remember`，`event_id` 去重），不依赖 omnihist、不调外部 CLI、不联外网。默认关闭；开启后启动 60 秒首跑、按「汇集间隔」到点才跑，单次运行加锁、失败只记 `audit.jsonl`。控制口 `GET /collect/status`、`POST /collect/run`；新增设置项 `collectEnabled` / `collectEveryMinutes` / `collectTools`。边界：单文件 >64 MB 跳过，单次每工具 ≤2000 条、总计 ≤20000 条，只收文本段。
- **工作台**（右下角胶囊）：概览 / 配置 / 会话 / 实体 / 备份 五个页签。
- **实体查看与修改**：直接读 `mem.doc`，可改标题、类型、正文。SQL 参数走 `psql -v :'name'`，正文用随机标签的美元引用——注入尝试实测无副作用。
- **一键装环境**：`docker run pgvector/pgvector:pg16` + 查/建库 + `create extension vector`（可选）+ Qdrant + 记忆服务（本地目录或 Git 地址）+ **装完真的拉起来并等 `/health`**（已在跑则识别为「已在运行」）。先预览、再确认；`ok` / `complete` 分开。
- **定时备份**：流式 `pg_dump`（大库不能读进内存）→ gzip；Qdrant 按集合建 snapshot；保留份数清理已实测（`keep=1` → `pruned=3`）。
- **参数滑杆 22 项**，分连接 / 环境 / 调优 / 备份四组，每项一句人话说明，写 `~/.dsh/zhishu-memory/settings.json`，下一轮生效。
- 首次安装自动弹出配置页（`setupDone=false` 时）。

### 发布相关

- `package.json`：去掉 `private`，补 `license: MIT`、`repository`、`homepage`、`bugs`、`engines.node`、`engines.dsh`、`publishConfig.access: public`、`keywords`、`scripts`、`./package.json` 导出。
- 新增 `LICENSE`（MIT）、`README.md`（英中双语，含「数据去哪了」）、`docs/MARKET-SUBMISSION.md`。
- **去掉全部机器硬编码**：`cordis.patch.yml` 无盘符；`ctx-guard` 日志默认 `~/.dsh/zhishu-memory/ctx-guard.jsonl`；`mem-float` 服务目录默认 `~/.dsh/zhishu-memory/service`。
- 新增自检工具：`tools/pack-check.mjs`（53 项）、`tools/fresh-profile.mjs`（link / `--install` 两种模式）、`tools/verify.mjs`（一把过）、`tools/audit-report.mjs`（运行期体检）。
- `prepublishOnly` 绑到 `verify`：闸门不过就发不出去。

### 已知限制

- 实体修改是**行级直改**：不写 `mem.audit`、不重算 hash、不重建向量。要「带审计的更正」需走服务端 `/remember` + `supersedes`。
- 一键安装只覆盖 Docker 路线；没有 Docker 时只给命令预览，不代跑。
- 全新 profile 的**挂载（activation）**没在第二台机器上验证；组合（`--dump-config`）与真实安装（`dsh plugin add`）已验证。

## 2.0.0 — 2026-11-18

四个独立插件目录（`dsh-zhishu-memory` / `dsh-ctx-guard` / `dsh-redline-guard` / `dsh-mem-float`）合并成一个 bundle，四行独立启停。
