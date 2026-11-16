-- ai.call 媒体类扩列（图片/视频也能记账）
-- 状态：**已执行** —— 账号主 2026-11-16 批红，智枢后端执行；
--       执行记录（含回填与修正）见 docs/org/tech/ai-call-media-backfill.md 第八节。
--       本文件是那次执行的原样 DDL 原文，改它等于改历史，别改。
-- 迁移日期：2026-11-16
-- 归属：智枢 zhishu 库 ai schema（表 ai.call）
-- 幂等：全部 `add column if not exists`，可重复执行；`comment on column` 本身幂等
-- 回滚：见 2026-11-16-ai-call-media-rollback.sql
--
-- 改了什么（9 列，全部可空，不动既有数据）：
--   workflow / prompt_text / seed / width / height / frames / peak_vram_mib / output_path / meta
--
-- 为什么：
--   账号主要求「生成的图片和视频也记进同一个账本，不另开表」，要能回答
--   这次生成用了什么工作流、什么 prompt、什么参数、多久、多大、产物在哪。
--   现有 ai.call 只够记文本调用（tokens / latency / gpu_seconds / ok / error_code），
--   媒体必需的工作流名、prompt 原文、seed、宽高帧数、显存峰值、产物路径都没有落点。
--
-- 复用既有列（不加新列，避免同义列分叉）：
--   任务类型  → task            （image / video / text；已存在，别再造 kind）
--   耗时      → latency_ms + gpu_seconds（已存在）
--   成败/错误 → ok + error_code （已存在）
--   模型      → endpoint_id 关联 ai.endpoint，需要时 join 取（别加 model_code）
--
-- 隐私口径（2026-11-16 定，本次不变）：
--   文字类（task=text）：仍然只存 prompt_hash（64 位 sha256）+ prompt_head 前 50 字，**不存原文**；
--   媒体类（task=image/video）才写 prompt_text 原文 —— 出图/出片的重跑、复现、争议排查离不了原文，
--   而文本对话的隐私成本完全是另一回事。本列注释里写死这条，别让后来人"顺手"打开。
--
-- 其它参数（steps / cfg / sampler / scheduler / 模型文件名 / 放大倍率 / 环境快照 hash）
-- 一律进 meta jsonb，不加碎列；读的时候 `coalesce(meta,'{}'::jsonb)`。
--
-- 现场事实（2026-11-16 20:5x 只读核对）：
--   ai.call 共 15 列、3 行（task = smoke / smoke2 / smoke2，全部 16:39–16:43 的冒烟数据），
--   没有任何真实调用，也没有 jsonb 列 —— 现在扩列代价为零，不会碰到真实数据。
--   既有索引 4 个：call_pkey(id)、call_time_idx(created_at desc)、
--   call_user_time_idx(user_id, created_at desc)、call_endpoint_time_idx(endpoint_id, created_at desc)。
--
-- 本次**不加索引**：按「只加可空列」的口径，索引留到有按 task 列表查询的真实需求再加
-- （候选：`create index concurrently ... on ai.call (task, created_at desc) where task in ('image','video')`）。

begin;

alter table ai.call add column if not exists workflow      text;
alter table ai.call add column if not exists prompt_text   text;
alter table ai.call add column if not exists seed          bigint;
alter table ai.call add column if not exists width         integer;
alter table ai.call add column if not exists height        integer;
alter table ai.call add column if not exists frames        integer;
alter table ai.call add column if not exists peak_vram_mib integer;
alter table ai.call add column if not exists output_path   text;
alter table ai.call add column if not exists meta          jsonb;

comment on column ai.call.task is
  '任务类型：text（文本对话，历史默认）/ image（出图）/ video（出片）。历史行里有 smoke、smoke2 冒烟值，故不加 CHECK 约束。';
comment on column ai.call.workflow is
  '工作流名（出图/出片用的 API JSON 文件名或分支名，如 xxx.api.json）。文本类为空。';
comment on column ai.call.prompt_text is
  '⚠ 仅媒体类（task in image,video）记录 prompt 原文；文字类只存 prompt_hash + prompt_head 前 50 字，禁止写本列（2026-11-16 隐私口径）。';
comment on column ai.call.seed is
  '生成种子。ComfyUI 的 64 位无符号种子超过 bigint 上限时置空，原值放 meta.seed_raw。';
comment on column ai.call.width is '产物宽度（像素）。文本类为空。';
comment on column ai.call.height is '产物高度（像素）。文本类为空。';
comment on column ai.call.frames is
  '视频帧数。图片为 1 或空；拿不到真实帧数时留空，不猜。';
comment on column ai.call.peak_vram_mib is
  '生成过程显存峰值（MiB）。只记实测值，采不到就留空，不填估算。';
comment on column ai.call.output_path is
  '产物路径（服务端本地绝对路径或对象存储 key）。图片=单文件；视频=成片。多产物时首个在此，全部列表放 meta.outputs。';
comment on column ai.call.meta is
  '其它参数（steps/cfg/sampler/scheduler/模型文件名/放大倍率/环境快照 hash/产物列表等）。不加碎列，读时 coalesce(meta,''{}''::jsonb)。';

commit;

-- 执行后自检（只读）：
--   select column_name, data_type, is_nullable from information_schema.columns
--    where table_schema='ai' and table_name='call' order by ordinal_position;   -- 期望 24 列
--   select count(*) from ai.call;                                              -- 期望仍是 3（加列不改行）
--   select count(*) from ai.call where task in ('image','video');               -- 期望 0（尚无媒体行）
