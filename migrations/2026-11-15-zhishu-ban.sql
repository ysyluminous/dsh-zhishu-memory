-- 封号名单：正式版表结构（**脚本只交，未执行，等账号主/总经理批红**）
-- 拟执行日期：待批
-- 归属：api（zhishu 库 public schema）
-- 幂等：create table/index if not exists，可重复执行
-- 回滚：见 2026-11-15-zhishu-ban-rollback.sql
--
-- 现状：今晚先走环境变量 BANNED_EMAILS / BANNED_USER_IDS（见 api/internal/httpapi/bans.go），
--       命中后登录 403、已登录的 /api/auth/session 与内部 gate 也 403。
-- 批红后把 bans.go 的 IsBanned 换成「环境变量 OR 查本表」，接口形状不用动。
--
-- 注意（需要批红时一起确认）：卡上定的是 email 做主键，那么「只按 user_id 封、
-- 不知道邮箱」的场景就得塞一个占位 email。如果要支持纯 user_id 封禁，
-- 建议改成 id bigserial 主键 + email/user_id 各一个 partial unique index。
-- 这里先按卡上的字段落地。

create table if not exists public.zhishu_ban (
  email     text primary key,
  user_id   text,
  reason    text        not null default '',
  banned_at timestamptz not null default now(),
  banned_by text        not null default ''
);

-- 按 user_id 反查封禁（只对填了 user_id 的行建，省空间）
create index if not exists zhishu_ban_user_idx
  on public.zhishu_ban (user_id)
  where user_id is not null;
