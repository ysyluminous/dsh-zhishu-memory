-- mem.gate_log：每次 gate 的召回日志（数据飞轮看板埋点）
-- 迁移日期：2026-11-14
-- 归属：memory-mcp（mem 库）
-- 幂等：可重复执行；已存在表 / 索引 / 函数时原样跳过
-- 回滚：见 2026-11-14-gate-log-rollback.sql
--
-- 说明：
--   1. 只新增一张表与两个索引，不改任何既有表结构；
--   2. id 用 bigserial，写入方（service.gate）只 insert 不 update；
--   3. query 原文照存（审计需要逐轮归因），query_hash 用 sha256 便于去重统计；
--   4. hit_ids / hit_scores 用 jsonb 数组，空召回时是 '[]'；
--   5. ok=false 记失败原因在 error 里，看板据此算空召回率与失败率。

create table if not exists mem.gate_log (
  id              bigserial primary key,
  user_id         text        not null,
  source          text        not null default 'mcp',
  conversation_id text        not null default '',
  turn            integer,
  query           text        not null default '',
  query_hash      text        not null default '',
  hit_ids         jsonb       not null default '[]'::jsonb,
  hit_scores      jsonb       not null default '[]'::jsonb,
  count           integer     not null default 0,
  injected_chars  integer     not null default 0,
  latency_ms      integer     not null default 0,
  ok              boolean     not null default true,
  error           text        not null default '',
  created_at      timestamptz not null default now()
);

create index if not exists gate_log_user_time_idx
  on mem.gate_log (user_id, created_at desc);

create index if not exists gate_log_time_idx
  on mem.gate_log (created_at desc);
