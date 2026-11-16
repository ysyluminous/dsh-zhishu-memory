-- mem.todo：加 owner_type / owner_id / link 三个字段（草稿，未执行，待账号主批红）
-- 迁移日期：2026-11-15
-- 归属：memory-mcp（mem 库）
-- 幂等：可重复执行；列/索引/约束已存在时跳过
-- 回滚：2026-11-15-todo-owner-link-rollback.sql
--
-- 设计要点（和「生产历史数据一行不删不改」对齐）：
--   1. 三列全部 **可空、不给默认值**，ADD COLUMN 是元数据操作，不重写表、不改任何历史行；
--   2. 历史行这三列保持 NULL，语义是「未知归属」。读侧一律 coalesce(owner_type,'human')，
--      要回填必须单独出数据迁移、单独批红，不在本脚本里做；
--   3. owner_id 不设外键：人侧是账号主 id，agent 侧是 bot 名，两者不在同一张表；
--   4. link 用前缀约定而不是外键，指向别处的记录：
--        card:<卡片 id>        /  link:card:xxx
--        memorial:<奏折 id>    /  idea:<mem.idea.id>
--      前缀写错顶多跳转失败，不会破坏引用完整性，也不会级联删数据。
--
-- 为什么三列而不是一张关联表：
--   /growth/time 与 /court 是同一批待办的两个视角，一天要读几十次；
--   关联表会让每个列表页多一次 join，而归属只有一对一。等需要多对多再拆表。

alter table mem.todo add column if not exists owner_type text;
alter table mem.todo add column if not exists owner_id   text;
alter table mem.todo add column if not exists link       text;

-- 取值域约束（幂等：约束已存在就跳过）
do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'todo_owner_type_chk'
       and conrelid = 'mem.todo'::regclass
  ) then
    alter table mem.todo
      add constraint todo_owner_type_chk
      check (owner_type is null or owner_type in ('human','agent'));
  end if;
end $$;

-- /growth/time：按人/代理 + 状态取待办（部分索引，null 的历史行不进索引）
create index if not exists todo_owner_idx
  on mem.todo (user_id, owner_type, status, due_at)
  where owner_type is not null;

-- /court：按 link 反查「这条待办挂在哪张卡/哪份奏折」
create index if not exists todo_link_idx
  on mem.todo (user_id, link)
  where link is not null and link <> '';

comment on column mem.todo.owner_type is 'human=账号主本人；agent=某个 bot；NULL=历史行，读侧按 human 处理';
comment on column mem.todo.owner_id   is 'human 时是账号主 id；agent 时是 bot 名（与 list_bots 逐字一致）';
comment on column mem.todo.link       is '前缀约定：card:<id> / memorial:<id> / idea:<id>，不设外键';

-- 读侧约定（不改表，写在代码里）：
--   /growth/time   select ... from mem.todo
--                  where user_id = $1 and coalesce(owner_type,'human') = 'human'
--   /growth/time   关联 agent 待办（只读展示）
--                  ... where user_id = $1 and owner_type = 'agent'
--   /court         ... where user_id = $1 and owner_type = 'agent'
