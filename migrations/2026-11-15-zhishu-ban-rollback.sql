-- 回滚：2026-11-15-zhishu-ban.sql
-- 只删本迁移建的表与索引；其它 schema 对象不动。
-- 幂等：if exists。

drop index if exists public.zhishu_ban_user_idx;
drop table if exists public.zhishu_ban;
