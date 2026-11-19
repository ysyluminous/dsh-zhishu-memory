-- 回滚：mem.ctx_trace / mem.hindsight_trace / mem.v_ctx_daily / mem.gate_log.bot
-- 对应迁移：2026-11-17-ctx-trace.sql
-- 幂等：对象不存在时静默跳过。
--
-- ============================================================
-- 状态：脚本已交，**尚未在生产 mem 库执行**（生产库结构变更要账号主批红）。
--       执行前先确认：设置页的上下文审计面板已下线、ctx_trace 里的数据可丢。
-- ============================================================
--
-- 注意：回滚会丢掉已写入的上下文快照与 hindsight 留痕；gate_log 只删本次
-- 新增的 bot 列，不动历史行的其他字段。

drop view if exists mem.v_ctx_daily;

drop index if exists mem.hindsight_trace_op_day_idx;
drop index if exists mem.hindsight_trace_user_day_idx;
drop table if exists mem.hindsight_trace;

drop index if exists mem.ctx_trace_kind_day_idx;
drop index if exists mem.ctx_trace_bot_day_idx;
drop index if exists mem.ctx_trace_session_idx;
drop index if exists mem.ctx_trace_user_day_idx;
drop table if exists mem.ctx_trace;

drop index if exists mem.gate_log_bot_day_idx;
alter table mem.gate_log drop column if exists bot;
