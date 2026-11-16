# memory-mcp/migrations 说明

本目录放 mem / zhishu 两个库的结构变更脚本，命名 `YYYY-MM-DD-<主题>.sql` + 同名 `-rollback.sql`。
**执行前要账号主批红**；脚本一律写成幂等（`if not exists` / `if exists`），回滚脚本同等对待。

## 已落地

| 日期 | 脚本 | 库 / schema | 说明 |
|---|---|---|---|
| 2026-11-14 | `2026-11-14-gate-log.sql` | mem | 每次 gate 的召回日志（数据飞轮埋点） |
| 2026-11-14 | `2026-11-14-search-cn-indexes.sql` | mem | pg_trgm 中文检索索引（配额/分层那次检索升级的配套） |
| 2026-11-15 | `2026-11-15-guoxue-sh.sql`（含 `-seed.py`） | zhishu · guoxue | 伤寒论条文/注解/章节三表 + 种子 |
| 2026-11-15 | `2026-11-15-guoxue-sh-annotation-song.sql` | zhishu · guoxue | 注解补 `song`（宋本对照）列 |
| 2026-11-16 | `2026-11-16-activity-log-end-nullable.sql` | zhishu · growth | 计划块允许没有 `end_at`；实做缺 end 由触发器拒 |
| 2026-11-16 | `2026-11-16-growth-bill.sql` | zhishu · growth | 账单表 `growth.bill`（老站 pim_bill 迁入） |
| 2026-11-16 | `2026-11-16-ai-call-media.sql` | zhishu · ai | `ai.call` 媒体扩列 9 列（图/视频记账）——**已于 2026-11-16 由账号主批红、智枢后端执行**；配套 [回填方案](../../docs/org/tech/ai-call-media-backfill.md) |

## ⚠ `growth.activity_log` 的 id 空洞：不是数据缺失，勿清理、勿重排

2026-11-16 核过一次：表里只有 id `2–12` 和 `29`，中间 `13–28` 是空的。

**原因是 PostgreSQL 的序列不参与事务回滚**：`nextval()` 在事务外生效，所以
「插入被约束/触发器拒绝」和「整段事务 ROLLBACK」都会照样吃掉一个序号。

**定性（幕后大佬 2026-11-16）：13–28 就是今天我们自己验收时消耗的**，不是别人的测试残留——
为验证「计划块可以没有 end_at」这条约束，在事务里反复插入再 `ROLLBACK`，序号就吃了。
我能点名的是：`26` 我插的计划块（已删）、`27` 「实做缺 end_at」被触发器拒（从未落行）、`28` MCP 端到端计划块（已删）；
其余 `13–25` 是 11-15 / 11-16 两轮事务内演练回滚掉的插入占掉的。

**`29` 不是脏数据，别清**：那是知行助理验通后写的真数据
（2026-11-15「成长.写投稿文章」计划块，`end_at` 空、`duration_min` null）。

结论与处置：

- **13–28 没有数据**，`select * from growth.activity_log where id between 13 and 28` 返回空集；
- 不要 `setval` 重排序列 —— `last_value=29` 与表内 `max(id)=29` 已一致，重排反而可能撞主键；
- id 只是代理键，`growth_day` / `v_day_blocks` 按 id 排序不受跳号影响；
- 以后再看到空洞，先查 `select max(id), (select last_value from growth.activity_log_id_seq)`，两者一致就是正常。

（同类空洞也可能出现在别的 `bigserial` 表上，成因一样，不要当脏数据清。）
