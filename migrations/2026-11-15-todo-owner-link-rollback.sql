-- 回滚 2026-11-15-todo-owner-link.sql
-- 只删本次新增的列/索引/约束，不动 mem.todo 的任何数据行
-- 注意：回滚前先确认没有代码在读 owner_type/owner_id/link，否则页面会报列不存在

drop index if exists mem.todo_link_idx;
drop index if exists mem.todo_owner_idx;

alter table mem.todo drop constraint if exists todo_owner_type_chk;

alter table mem.todo drop column if exists link;
alter table mem.todo drop column if exists owner_id;
alter table mem.todo drop column if exists owner_type;
