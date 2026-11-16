-- 回滚：2026-11-16-activity-log-end-nullable.sql
-- 把 growth.activity_log 的 end_at / 跨度约束恢复成 2026-11-15 的样子：
--   end_at  not null；check (end_at > start_at)；去掉 activity_log_require_end 触发器。
--
-- ⚠ 恢复 not null 前必须先处理「没有 end_at 的计划块」，否则 set not null 会失败。
--   这里显式检查并给出可读报错，不静默改数据、不偷偷删行。
-- 幂等：触发器/函数/约束都 if exists；检查通过后重复执行不报错。

do $$
declare missing bigint;
begin
  select count(*) into missing from growth.activity_log where end_at is null;
  if missing > 0 then
    raise exception '回滚前先处理 % 条没有 end_at 的时间块（补 end_at 或确认删除），否则无法恢复 end_at not null', missing;
  end if;
end $$;

drop trigger if exists activity_log_require_end on growth.activity_log;
drop function if exists growth.activity_log_require_end();

alter table growth.activity_log drop constraint if exists activity_log_span;
alter table growth.activity_log
  add constraint activity_log_span check (end_at > start_at);

alter table growth.activity_log alter column end_at set not null;

comment on column growth.activity_log.end_at is '结束时间，必须晚于 start_at。';
