-- 回滚：2026-11-15-guoxue-sh.sql
-- 只删本迁移新建的 schema guoxue 及其全部对象；不碰其他 schema。
-- pg_trgm 扩展是库级共享对象（mem 库与既有功能也在用），保留，不 drop extension。
-- 幂等：if exists。

drop schema if exists guoxue cascade;
