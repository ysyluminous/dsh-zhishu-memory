-- mem.flywheel_metric：飞轮指标日打点（草稿 v3，未执行，schema 变更待账号主批红）
-- 迁移日期：2026-11-14
-- 归属：memory-mcp（mem 库）
-- 幂等：可重复执行；已存在则跳过
-- 回滚：见 2026-11-14-metric-rollback.sql
--
-- v3：按总经理 2026-11-14 23:3x 约束收缩——不新建一堆表，只加这一张。
--   原先草稿里的 metric_def / metric_run / metric_daily 全部去掉，指标名与批次都放进 dim。
--
-- 关键约定（采集脚本必须遵守，否则比率聚错）：
--   * value 存指标值本身（比率存 0.05，不存 5）；
--   * n 存分母样本量（写回 101 次挂 5 次 → value=0.0495, n=101）；
--   * 跨日/跨维度算比率必须 sum(value*n)/sum(n)，不能 avg(value)，也不能 sum(value)；
--   * 计数类指标 n 留 0，value 直接存次数。
--
-- 同日复跑（流量/定锚/定锚复跑）不覆盖历史：
--   dim 里带批次，如 {"scope":"fixed","run":"20261114T2325"}，唯一键含 dim_hash，
--   复跑是新增行；看板聚合只取 is_canonical=true 的那一行（换口径时把旧行置 false）。
--
-- 隔离：带 user_id，读写必须 where user_id = <当前身份>（AGENTS.md 二·六）。

create table if not exists mem.flywheel_metric (
  id           bigserial   primary key,
  user_id      text        not null,
  day          date        not null,                  -- 指标归属日，按 Asia/Shanghai 切日
  metric       text        not null,                   -- 点分小写：gate.hit_rate / writeback.fail_rate
  value        numeric     not null,                   -- 指标值（比率 0~1）
  n            integer     not null default 0,         -- 分母样本量
  dim          jsonb       not null default '{}'::jsonb, -- {"scope":"fixed","run":"20261114T2325","kind":"report"}
  dim_hash     text        not null default '',        -- md5(dim 规范序列化)，唯一键用（jsonb 无 btree 默认族）
  is_canonical boolean     not null default true,      -- 同日同 metric 的官方口径批次
  source       text        not null default '',        -- 采集脚本 + 关键参数：scripts/flywheel_eval.py --set
  updated_at   timestamptz not null default now()
);

-- 同一天同一指标同一维度只留一行：重跑脚本是 upsert 覆盖，不产生重复点
create unique index if not exists flywheel_metric_key_idx
  on mem.flywheel_metric (user_id, day, metric, dim_hash);

create index if not exists flywheel_metric_day_idx
  on mem.flywheel_metric (user_id, day desc);

create index if not exists flywheel_metric_metric_idx
  on mem.flywheel_metric (metric, day desc);

comment on table mem.flywheel_metric is '飞轮指标日打点：一行=日×指标×维度，复跑按 dim 区分，is_canonical 定官方口径';
comment on column mem.flywheel_metric.n is '分母样本量；比率跨期聚合必须 sum(value*n)/sum(n)';

-- 看板取「某天官方口径全指标」
-- select metric, value, n, dim->>'scope' as scope
-- from mem.flywheel_metric
-- where user_id = $1 and day = date '2026-11-14' and is_canonical
-- order by metric;

-- 周/月比率（禁止 avg）
-- select metric, sum(value*n)/nullif(sum(n),0) as rate
-- from mem.flywheel_metric
-- where user_id = $1 and metric = 'writeback.fail_rate' and is_canonical
--   and day >= date '2026-11-07' and day < date '2026-11-14'
-- group by 1;
