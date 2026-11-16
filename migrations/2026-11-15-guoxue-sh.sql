-- 伤寒论·条文注解：新建 guoxue schema（三张表 + 索引）
-- 迁移日期：2026-11-15
-- 归属：api（zhishu 库）
-- 幂等：schema / 表 / 索引全部 if not exists，重复执行不报错
-- 回滚：见 2026-11-15-guoxue-sh-rollback.sql
--
-- 边界：只新增 schema guoxue 里的对象；不碰 cms / growth / wealth / gov / public 任何对象。
--
-- 全文检索选型理由（实测，不是照抄）：
--   本库没有中文分词器（pg_jieba / zhparser 均未安装），
--   to_tsvector('simple', '太阳之为病，脉浮，头项强痛而恶寒') 会把连续汉字当成一个 token，
--   用 '头项强痛' 去 plainto_tsquery 查不到；因此不用 tsvector/GIN(tsvector)，
--   改用 pg_trgm（GIN + gin_trgm_ops）支撑 ilike 子串与 similarity，
--   中文任意子串可命中，且与 2026-11-14 个人记忆检索的选型保持一致。
--
-- 数据口径（源头 D:\code\yunxiao\temp\shanghanlun\records.json，13,112 条）：
--   sh_clause     398 条  = kind='原文' 的记录（no 形如 1.001 … 398.001）
--   sh_annotation 12,714  = 13,112 − 398，其中
--                            · 398 条 kind='词解'
--                            · 11,940 条各家注解（clause_no 指向对应条文）
--                            · 376 条来自《六经八纲-读懂金匮要略》讲座（no='399.0'，不属伤寒论条文，
--                              clause_no 置 NULL，kind='金匮'，只用于全文检索，不进条文详情）
--   sh_chapter    10 章   = 前端 apps/guoxue-web/src/lib/tcm/sh-chapters.ts 的既有 slug

create schema if not exists guoxue;

create extension if not exists pg_trgm;

-- 章节：id 用前端既有 slug（taiyang-shang … yinyang-yi），title 是中文篇名
create table if not exists guoxue.sh_chapter (
  id        text primary key,
  title     text        not null,
  order_idx integer     not null
);

-- 条文：一条只有一个主文本，song 是宋本对照（全量仅 766 条非空）
create table if not exists guoxue.sh_clause (
  no         text primary key,                                  -- 1.001 … 398.001
  chapter    text        not null references guoxue.sh_chapter(id),
  volume     integer,                                           -- 源数据无卷次，留空
  order_idx  integer     not null,                              -- 1 … 398，列表排序用
  kangping   text        not null default '',                   -- 康平本/主文本
  song       text        not null default '',                   -- 宋本对照
  created_at timestamptz not null default now()
);

-- 注解：clause_no 可空（金匮讲座 376 条无对应条文），其余按条文挂
create table if not exists guoxue.sh_annotation (
  id         bigserial primary key,
  clause_no  text references guoxue.sh_clause(no) on delete cascade,
  author     text        not null,                              -- 注解人 / 词解 / 金匮篇名
  book       text        not null default '',
  kind       text        not null default '',                   -- 注解 / 词解 / 金匮
  ver        text        not null default '',
  body       text        not null default '',
  created_at timestamptz not null default now()
);

create index if not exists sh_clause_chapter_order_idx
  on guoxue.sh_clause (chapter, order_idx);

create index if not exists sh_annotation_clause_idx
  on guoxue.sh_annotation (clause_no);

create index if not exists sh_annotation_author_idx
  on guoxue.sh_annotation (author);

-- 种子幂等的自然键：同一条文下同一 (作者, 书, 版本, 正文) 只留一条，
-- 重复跑种子走 on conflict do update，不会重复插。
create unique index if not exists sh_annotation_natural_uniq
  on guoxue.sh_annotation ((coalesce(clause_no, '')), author, book, ver, md5(body));

-- 全文检索索引：条文正文（康平本 + 宋本拼一起）与注解正文各一条 trigram GIN
create index if not exists sh_clause_text_trgm
  on guoxue.sh_clause using gin ((coalesce(kangping, '') || ' ' || coalesce(song, '')) gin_trgm_ops);

create index if not exists sh_annotation_body_trgm
  on guoxue.sh_annotation using gin (body gin_trgm_ops);
