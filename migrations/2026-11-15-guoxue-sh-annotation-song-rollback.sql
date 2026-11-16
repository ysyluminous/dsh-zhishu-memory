-- 回滚：2026-11-15-guoxue-sh-annotation-song.sql
-- 只删本补丁加的条件索引与 song 列；guoxue 其他对象不动。
-- 幂等：if exists。

drop index if exists guoxue.sh_annotation_song_nonempty_idx;
alter table guoxue.sh_annotation drop column if exists song;
