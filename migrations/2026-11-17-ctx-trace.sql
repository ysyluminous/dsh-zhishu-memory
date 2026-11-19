-- mem.ctx_trace / mem.hindsight_trace / mem.v_ctx_daily / mem.gate_log.bot
-- 迁移日期：2026-11-17
-- 归属：memory-mcp（mem 库）· 需求方：账号主 2026-11-17 15:5x「上下文/压缩/门控/hindsight 留痕可审计」
-- 幂等：可重复执行；表 / 列 / 索引 / 视图已存在时原样跳过
-- 回滚：见 2026-11-17-ctx-trace-rollback.sql
--
-- 说明：
--   1. 只新增 mem.ctx_trace、mem.hindsight_trace 两张表、一个视图，并给既有
--      mem.gate_log 补一列 bot（默认空串，不动历史行）；
--   2. 写入方只 insert 不 update：一条记录一次调用 / 一次事件，事后不改原文；
--   3. 上下文快照按「每次模型调用一行」存，注入块放 jsonb（name/src/chars/tokens_est），
--      不存 prompt 原文，避免把正文再抄一遍；需要原文时按 (session_id, turn, step) 回会话文件取；
--   4. day 由写入方显式给（Asia/Shanghai 日期），缺省用服务器时区兜底；
--   5. 压缩两种口径同时存：compaction.action 记 start/summary/end，
--      payload.landed 记「是否真落地」，与 usage.calls 的对账口径一致；
--   6. 回滚会丢已写入的上下文快照与 hindsight 记录；生产执行前先确认页面已下线并拿批红。

-- ---------------------------------------------------------------- ctx_trace

create table if not exists mem.ctx_trace (
  id                 bigserial primary key,
  user_id            text        not null,
  day                date        not null default ((now() at time zone 'Asia/Shanghai')::date),
  session_id         text        not null default '',
  bot                text        not null default '',
  part               integer,
  turn               integer,
  step               integer,
  model              text        not null default '',
  kind               text        not null default 'pre-call',  -- pre-call | post-call | compaction | gate | hindsight | writeback
  injected           jsonb       not null default '[]'::jsonb,  -- [{name,src,chars,tokens_est}]
  injected_chars     integer     not null default 0,
  injected_tokens    integer     not null default 0,
  history_chars      integer     not null default 0,
  history_tokens     integer     not null default 0,
  system_tokens      integer     not null default 0,
  ctx_tokens         integer     not null default 0,
  ctx_limit          integer,
  ctx_pct            numeric(6,3),
  prompt_tokens      integer,
  completion_tokens  integer,
  cache_read_tokens  bigint,
  cache_pct          numeric(6,3),
  compaction         jsonb,
  payload            jsonb       not null default '{}'::jsonb,
  latency_ms         integer,
  ok                 boolean     not null default true,
  error              text        not null default '',
  created_at         timestamptz not null default now()
);

create index if not exists ctx_trace_user_day_idx
  on mem.ctx_trace (user_id, day desc);

create index if not exists ctx_trace_session_idx
  on mem.ctx_trace (session_id, created_at desc);

create index if not exists ctx_trace_bot_day_idx
  on mem.ctx_trace (bot, day desc);

create index if not exists ctx_trace_kind_day_idx
  on mem.ctx_trace (kind, day desc);

-- ----------------------------------------------------------- hindsight_trace

create table if not exists mem.hindsight_trace (
  id           bigserial primary key,
  user_id      text        not null,
  day          date        not null default ((now() at time zone 'Asia/Shanghai')::date),
  session_id   text        not null default '',
  bot          text        not null default '',
  op           text        not null default 'query',  -- ingest | query | health
  endpoint     text        not null default '',
  req_digest   text        not null default '',       -- 请求摘要（前 200 字），不存全文
  req_chars    integer     not null default 0,
  latency_ms   integer     not null default 0,
  returned     integer     not null default 0,
  ok           boolean     not null default true,
  error        text        not null default '',
  payload      jsonb       not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);

create index if not exists hindsight_trace_user_day_idx
  on mem.hindsight_trace (user_id, day desc);

create index if not exists hindsight_trace_op_day_idx
  on mem.hindsight_trace (op, day desc);

-- --------------------------------------------------------- gate_log.bot 补列

alter table mem.gate_log
  add column if not exists bot text not null default '';

create index if not exists gate_log_bot_day_idx
  on mem.gate_log (bot, created_at desc);

-- ------------------------------------------------------------- 每日聚合视图

create or replace view mem.v_ctx_daily as
select
  t.user_id,
  t.day,
  t.bot,
  t.session_id,
  count(*) filter (where t.kind = 'pre-call')                              as calls,
  coalesce(sum(t.ctx_tokens) filter (where t.kind = 'pre-call'), 0)         as ctx_tokens_sum,
  coalesce(max(t.ctx_tokens) filter (where t.kind = 'pre-call'), 0)         as ctx_tokens_max,
  round(avg(t.ctx_pct) filter (where t.kind = 'pre-call'), 2)               as ctx_pct_avg,
  count(*) filter (where t.kind = 'compaction')                             as compaction_events,
  coalesce(sum(t.prompt_tokens), 0)                                          as prompt_tokens,
  coalesce(sum(t.completion_tokens), 0)                                       as completion_tokens,
  coalesce(sum(t.cache_read_tokens), 0)                                       as cache_read_tokens,
  coalesce(sum(t.injected_chars), 0)                                          as injected_chars
from mem.ctx_trace t
group by t.user_id, t.day, t.bot, t.session_id;
