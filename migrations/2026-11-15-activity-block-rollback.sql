-- 回滚：时间台账三层模型（2026-11-15 迁移）
-- 幂等：对象不存在时静默跳过。
--
-- 状态：仅作留档，尚未执行；执行前要账号主批红。
-- 注意：
--   1. 回滚会删掉三个新表与合并视图，等于丢掉迁移窗口内写进 activity_log 的全部时间块。
--   2. growth.time_entry 的注释（DEPRECATED）一并恢复，旧表数据本迁移从未改动。
--   3. 只删本迁移新建的对象，不碰 growth 里的既有表。

drop view if exists growth.v_time_entry_anomalies;
drop view if exists growth.v_day_blocks;

comment on table growth.time_entry is null;

drop table if exists growth.activity_log_tag;
drop table if exists growth.tag;
drop table if exists growth.activity_log;

drop trigger if exists activity_cap on growth.activity;
drop function if exists growth.activity_cap();

drop table if exists growth.activity;
