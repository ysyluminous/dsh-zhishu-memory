-- 回滚：2026-11-16-ai-call-media.sql（ai.call 媒体类扩列）
-- 状态：配套迁移**已于 2026-11-16 执行**（账号主批红），所以本回滚脚本随时可用；
--       执行记录见 docs/org/tech/ai-call-media-backfill.md 第八节。
-- 迁移日期：2026-11-16
-- 归属：智枢 zhishu 库 ai schema
-- 幂等：全部 `drop column if exists`
--
-- ⚠ 回滚会**丢掉已回填的媒体账**（workflow / prompt_text / seed / 宽高帧数 / 显存峰值 / 产物路径 / meta）。
--   若有真实媒体行，先导出再回滚：
--     \copy (select * from ai.call where task in ('image','video')) to 'ai_call_media_backup.csv' csv header
--   只加可空列、没动既有数据，所以「列不存在」不等于「数据丢了」，但列一删值就没了。
--
-- 不影响：现有 3 行冒烟数据（这些列本来就是空）、Go 网关的 insert（它没用到这些列）。

begin;

alter table ai.call drop column if exists meta;
alter table ai.call drop column if exists output_path;
alter table ai.call drop column if exists peak_vram_mib;
alter table ai.call drop column if exists frames;
alter table ai.call drop column if exists height;
alter table ai.call drop column if exists width;
alter table ai.call drop column if exists seed;
alter table ai.call drop column if exists prompt_text;
alter table ai.call drop column if exists workflow;

commit;

-- 回滚后自检：列数应回到 15
--   select count(*) from information_schema.columns where table_schema='ai' and table_name='call';
