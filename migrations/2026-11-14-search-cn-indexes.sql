-- 中文检索（pg_trgm）索引：配合 service.search_personal 的多词 AND 过滤 + similarity 排序
-- 迁移日期：2026-11-14
-- 归属：memory-mcp（mem 库）
-- 幂等：全部 create index if not exists，可重复执行
-- 回滚：见 2026-11-14-search-cn-indexes-rollback.sql
--
-- 背景（2026-11-14 只读探测结果）：
--   1. pg_available_extensions 里与中文检索相关的只有 pg_trgm 1.6（已安装）；
--      pg_jieba / zhparser / vector 均无，做不了 tsvector 中文分词，
--      所以路线定为 pg_trgm + similarity/word_similarity 排序，ilike 仍作过滤层；
--   2. mem.doc(title, body)、mem.conv(snippet) 的 trgm 索引在本迁移之前已存在，
--      本迁移只补新查询真正需要、库里还缺的三项：
--      - mem.conv.title 的 trgm 索引（新查询会 title ilike 过滤会话标题）；
--      - mem.conv 上 coalesce(snippet,'') 的表达式 trgm 索引
--        （新查询为防 NULL 传引用 coalesce，普通 snippet 索引用不上该表达式）；
--      - 两张表的统计信息重算，让规划器按新分布选索引。

create extension if not exists pg_trgm;

-- mem.doc：标题 / 正文多词过滤（已存在则跳过，不重建）
create index if not exists doc_title_trgm on mem.doc using gin (title gin_trgm_ops);
create index if not exists doc_body_trgm  on mem.doc using gin (body gin_trgm_ops);

-- mem.conv：会话标题 / 正文片段多词过滤
create index if not exists conv_title_trgm on mem.conv using gin (title gin_trgm_ops);
create index if not exists conv_snippet_trgm on mem.conv using gin (snippet gin_trgm_ops);
create index if not exists conv_snippet_coalesce_trgm
  on mem.conv using gin ((coalesce(snippet, '')) gin_trgm_ops);

analyze mem.doc;
analyze mem.conv;
