-- 时间台账三层模型：activity（活动实体）/ activity_log（时间块）/ tag + activity_log_tag
-- 迁移日期：2026-11-15
-- 归属：zhishu 库的 growth schema（不是 mem 库；growth.time_entry 等既有表都在这里）
-- 幂等：全部 if not exists / create or replace，重复执行不报错
-- 回滚：见 2026-11-15-activity-block-rollback.sql
--
-- 唯一真源（内容副总 2026-11-15 定）：
--   activity_log 是唯一写入真源；growth.time_entry 立刻停写、保留只读、标 deprecated、一条不删。
--   迁移窗口内 growth_day 走 growth.v_day_blocks 合并读（历史 time_entry + 新块），
--   迁完切新表，v_day_blocks 保留为过渡视图。
--
-- user_id 隔离（AGENTS.md 二·六）：每张表都带 user_id，复合主键/外键都含 user_id。
--
-- ⚠ 灌种子的顺序（下一个接手的人注意）：activity.parent_id 是自引用外键，
--   必须先插父节点再插子节点（例：先 growth，再 growth.iterate_bot）；
--   顺序反了会撞 activity_parent_fk。实测撞过一次，别重蹈。
-- ⚠ 实体总数上限 30 条/用户，由 activity_cap 触发器强制；第 31 条直接报错。

-- ---------- 1. 活动实体 ----------
-- id 用稳定 slug（sport.racket.badminton），可读、可跨环境对齐；层级用 parent_id 自引用。
create table if not exists growth.activity (
  id                text        not null,
  user_id           text        not null,
  name              text        not null,
  parent_id         text,
  default_dimension text        not null default '场景',
  default_tags      jsonb       not null default '{}'::jsonb,
  sort_order        integer     not null default 100,
  archived_at       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint activity_pkey primary key (user_id, id),
  constraint activity_parent_fk foreign key (user_id, parent_id)
    references growth.activity (user_id, id) on delete restrict,
  constraint activity_name_len check (char_length(btrim(name)) between 1 and 40),
  constraint activity_self_parent check (parent_id is null or parent_id <> id)
);

comment on table growth.activity is
  '活动实体（层级树）。总数上限 30 条/用户，由 activity_cap 触发器强制；超过即报错，不静默截断。';

-- 实体总数 ≤30：触发器兜底，插入第 31 条直接失败。
create or replace function growth.activity_cap() returns trigger
language plpgsql as $$
begin
  if (select count(*) from growth.activity a
        where a.user_id = new.user_id and a.archived_at is null) >= 30 then
    raise exception 'activity 实体已达上限 30 条（user_id=%），请先归档或复用', new.user_id;
  end if;
  return new;
end $$;

drop trigger if exists activity_cap on growth.activity;
create trigger activity_cap before insert on growth.activity
  for each row execute function growth.activity_cap();

-- ---------- 2. 时间块 ----------
-- 出门与回家是一块的两头，不单记；end_at 必须晚于 start_at，跨天由调用方切成两块。
create table if not exists growth.activity_log (
  id           bigserial   primary key,
  user_id      text        not null,
  activity_id  text        not null,
  start_at     timestamptz not null,
  end_at       timestamptz not null,
  duration_min integer     generated always as
                 ((floor(extract(epoch from (end_at - start_at)) / 60))::integer) stored,
  status       text        not null default '计划',
  note         text        not null default '',
  source       text        not null default 'mcp',
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  constraint activity_log_span check (end_at > start_at),
  constraint activity_log_status check (status in ('计划', '实做', '未记')),
  constraint activity_log_activity_fk foreign key (user_id, activity_id)
    references growth.activity (user_id, id) on delete restrict
);

comment on table growth.activity_log is
  '时间块（唯一写入真源）。duration_min 由 start/end 生成，不手填；出门与回家是同一块的两头。';

create index if not exists activity_log_user_start_idx
  on growth.activity_log (user_id, start_at desc);
create index if not exists activity_log_start_idx
  on growth.activity_log (start_at desc);
create index if not exists activity_log_activity_idx
  on growth.activity_log (user_id, activity_id, start_at desc);

-- ---------- 3. 标签 ----------
-- 五个固定维度，不多不少；同维度同名标签在用户内唯一。
create table if not exists growth.tag (
  id         bigserial   primary key,
  user_id    text        not null,
  dimension  text        not null,
  name       text        not null,
  created_at timestamptz not null default now(),
  constraint tag_dimension check (dimension in ('场景', '同行', '能量', '资本', '质量')),
  constraint tag_name_len check (char_length(btrim(name)) between 1 and 24),
  constraint tag_user_name_uniq unique (user_id, dimension, name),
  constraint tag_user_id_uniq unique (user_id, id)
);

comment on table growth.tag is
  '标签。五个固定维度：场景／同行／能量／资本／质量；维度取值由 tag_dimension 约束挡住。';

create table if not exists growth.activity_log_tag (
  user_id text   not null,
  log_id  bigint not null,
  tag_id  bigint not null,
  constraint activity_log_tag_pkey primary key (user_id, log_id, tag_id),
  constraint activity_log_tag_log_fk foreign key (log_id)
    references growth.activity_log (id) on delete cascade,
  constraint activity_log_tag_tag_fk foreign key (user_id, tag_id)
    references growth.tag (user_id, id) on delete cascade
);

-- ---------- 4. 过渡视图：迁移窗口合并读 ----------
-- growth_day 在迁移窗口内读这个视图：历史 time_entry（只读） + 新 activity_log。
--
-- data_anomaly 说明（内容副总 2026-11-15 口径）：
--   历史坏行不改、不修，只在读侧标出来，让看数的人知道这行为什么没有时长。
--   ''                正常
--   'end_before_start' 历史行 end <= start（例：growth.time_entry id=12，actual_end 01:31 < actual_start 09:00）
--   'missing_time'     历史行缺一头时间（planned/actual 都没配齐）
--   要不要修那几行，等账号主自己定。
create or replace view growth.v_day_blocks as
  select a.user_id,
         'activity_log'::text                      as src,
         a.id::text                                as ref_id,
         a.activity_id                             as activity_id,
         a.start_at                                as start_at,
         a.end_at                                  as end_at,
         a.duration_min                            as duration_min,
         a.status                                  as status,
         a.note                                    as note,
         ''::text                                  as data_anomaly
    from growth.activity_log a
  union all
  select t.user_id,
         'time_entry'::text                        as src,
         t.id::text                                as ref_id,
         'legacy.' || coalesce(nullif(t.kind, ''), 'time_entry') as activity_id,
         coalesce(t.actual_start, t.planned_start) as start_at,
         coalesce(t.actual_end, t.planned_end)     as end_at,
         case
           when coalesce(t.actual_start, t.planned_start) is not null
            and coalesce(t.actual_end, t.planned_end) is not null
            -- 历史数据里有 actual_end < actual_start 的坏行（id=12），必须留在 null，
            -- 否则合并读会出现负时长，把当天总时长算小。
            and coalesce(t.actual_end, t.planned_end) > coalesce(t.actual_start, t.planned_start)
           then (floor(extract(epoch from (
                  coalesce(t.actual_end, t.planned_end) - coalesce(t.actual_start, t.planned_start)
                )) / 60))::integer
           else null
         end                                       as duration_min,
         t.status                                  as status,
         coalesce(t.note, '')                      as note,
         case
           when coalesce(t.actual_start, t.planned_start) is null
             or coalesce(t.actual_end, t.planned_end) is null then 'missing_time'
           when coalesce(t.actual_end, t.planned_end) <= coalesce(t.actual_start, t.planned_start)
             then 'end_before_start'
           else ''
         end                                       as data_anomaly
    from growth.time_entry t;

comment on view growth.v_day_blocks is
  '迁移窗口的合并读视图：新 activity_log + 历史 time_entry。切换完成后只读 activity_log，视图留作对账。data_anomaly 标出历史坏行，坏行本身不修。';

-- 异常清单：谁身上有坏数据，一眼看全，不改原表。
create or replace view growth.v_time_entry_anomalies as
  select user_id, id, kind, title, status,
         planned_start, planned_end, actual_start, actual_end,
         case
           when coalesce(actual_start, planned_start) is null
             or coalesce(actual_end, planned_end) is null then 'missing_time'
           else 'end_before_start'
         end as data_anomaly
    from growth.time_entry
   where coalesce(actual_start, planned_start) is null
      or coalesce(actual_end, planned_end) is null
      or coalesce(actual_end, planned_end) <= coalesce(actual_start, planned_start);

comment on view growth.v_time_entry_anomalies is
  '历史 time_entry 异常清单（缺时间 / end<=start）。只读清单，不修原表；修不修由账号主定。';


-- ---------- 5. 旧表标 deprecated（只加注释，不删不改结构） ----------
comment on table growth.time_entry is
  'DEPRECATED 2026-11-15：只读历史，勿写。唯一写入真源已切到 growth.activity_log；新代码不许再 insert 这里。';
