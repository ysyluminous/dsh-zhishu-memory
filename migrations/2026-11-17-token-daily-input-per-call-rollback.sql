-- 回滚：mem.token_daily.input_per_call（2026-11-17）
-- 幂等：列不存在时静默跳过。回滚只删列，不动历史行其他字段。
alter table mem.token_daily drop column if exists input_per_call;