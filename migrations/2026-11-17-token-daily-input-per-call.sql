-- mem.token_daily.input_per_call：按「每次模型调用」口径的输入用量
-- 迁移日期：2026-11-17 · 依据：老大 2026-11-17 批准（口径纠错：用户轮次=turn/start，调用=带 usage 的 assistant/message）
-- 幂等：ADD COLUMN IF NOT EXISTS；回滚见 2026-11-17-token-daily-input-per-call-rollback.sql
-- 口径：input_per_call = input_tokens / call_count，call_count 取带 usage 的 assistant/message 条数，**不是 turn 数**；
--       turn_calls 列保留原义（用户轮次），不改含义，否则历史行对不上。

alter table mem.token_daily
  add column if not exists input_per_call numeric;

comment on column mem.token_daily.input_per_call is 'input_tokens / 带 usage 的 assistant/message 条数（每次模型调用的平均 input）';