-- 计划块允许没有结束时间（growth.activity_log 约束放松）
-- 迁移日期：2026-11-16
-- 归属：智枢 zhishu 库 growth schema
-- 幂等：约束先 drop if exists 再 add、函数 create or replace、触发器 drop/create，重复执行不报错
-- 回滚：见 2026-11-16-activity-log-end-nullable-rollback.sql
--
-- 背景（内容副总 2026-11-16）：
--   原定义在 2026-11-15-activity-block.sql:64 `end_at timestamptz not null`、
--   :72 `check (end_at > start_at)`，导致「先排计划、收工后补时长」写不进去。
--   现在：计划块只记 start_at（end_at 为 null），收工后再补 end_at。
--
-- 口径：
--   1. end_at 可空；跨度约束改成 `end_at is null or end_at > start_at`（相等仍拒）。
--   2. status='实做' 必须有 end_at —— 用触发器给中文报错文案（CHECK 的报错不可定制）。
--   3. status 枚举保持库内中文值 ('计划','实做','未记')，不引入 planned/actual 之类英文值。
--   4. duration_min 是生成列：end_at 为空时它自然为 null（不是 0），不用改列定义。

-- ⚠ 序列空洞说明（2026-11-16 核过）：本表 id 存在 13–28 的空洞，那是「插入被拒 / 事务回滚」
--   消耗掉的序列值（nextval 不随事务回滚），**不是数据缺失，勿清理、勿重排序列**。
--   详见同目录 README.md 的「id 空洞」一节。
alter table growth.activity_log alter column end_at drop not null;

-- 跨度：有结束时间才比大小；没有结束时间的计划块不参与比较。
alter table growth.activity_log drop constraint if exists activity_log_span;
alter table growth.activity_log
  add constraint activity_log_span check (end_at is null or end_at > start_at);

-- 实做块必须有 end_at；报错文案统一成这句（不要写「必填」）。
create or replace function growth.activity_log_require_end() returns trigger
language plpgsql as $$
begin
  if new.status = '实做' and new.end_at is null then
    raise exception '计划块可以没有结束时间；收工后补 end_at'
      using errcode = 'check_violation',
            hint = 'status=实做 的时间块必须填 end_at；只有 status=计划 的块可以先没有结束时间';
  end if;
  return new;
end $$;

comment on function growth.activity_log_require_end() is
  '实做块必须带 end_at；计划块允许 end_at 为空（2026-11-16）。';

drop trigger if exists activity_log_require_end on growth.activity_log;
create trigger activity_log_require_end
  before insert or update on growth.activity_log
  for each row execute function growth.activity_log_require_end();

comment on column growth.activity_log.end_at is
  '结束时间：计划块可为空（收工后补）；status=实做 时必须非空，由 activity_log_require_end 触发器强制。';

