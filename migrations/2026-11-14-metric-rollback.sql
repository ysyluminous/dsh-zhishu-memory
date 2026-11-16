-- 回滚 2026-11-14-metric.sql（v3，单表版）
-- 只删本次新增的对象
-- 注意：执行前先确认 mem.flywheel_metric 里没有已归档的历史点（本表是唯一副本）

drop table if exists mem.flywheel_metric;
