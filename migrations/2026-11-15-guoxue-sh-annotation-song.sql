-- 伤寒论·注解补宋本对照列：guoxue.sh_annotation.song
-- 迁移日期：2026-11-15
-- 归属：api（zhishu 库）
-- 幂等：add column if not exists；索引用 if not exists；可重复执行
-- 回滚：见 2026-11-15-guoxue-sh-annotation-song-rollback.sql
--
-- 背景：源头 records.json 里 766 条记录带 song（宋本对照），
--   其中 398 条是 kind='原文'（已入 sh_clause.song），
--   另 368 条散在各家注解里（陈逊斋 118、苏世屏 102、张长恩 75、胡希恕 72、何庆勇 1），
--   首版表结构没有这一列，按 A 口径「保留宋本数据」必须补上，否则等于改源数据。

alter table guoxue.sh_annotation
  add column if not exists song text not null default '';

-- 按 song 非空筛选用；368/12714 的行数占比很低，用条件索引足够
create index if not exists sh_annotation_song_nonempty_idx
  on guoxue.sh_annotation (clause_no)
  where song <> '';
