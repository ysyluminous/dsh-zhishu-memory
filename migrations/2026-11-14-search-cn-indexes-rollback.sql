-- 回滚：2026-11-14-search-cn-indexes.sql
-- 只删本迁移新增的两个索引：
--   mem.conv_title_trgm、mem.conv_snippet_coalesce_trgm
-- doc_title_trgm / doc_body_trgm / conv_snippet_trgm 在迁移前就存在，属于既有结构，不动。
-- pg_trgm 扩展被旧 ilike 兜底与评测脚本共用，保留，不 drop extension。
-- 幂等：drop index if exists。

drop index if exists mem.conv_title_trgm;
drop index if exists mem.conv_snippet_coalesce_trgm;
