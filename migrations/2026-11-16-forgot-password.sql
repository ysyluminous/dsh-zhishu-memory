-- 忘记密码（2026-11-16）：邮箱验证码改密
-- 1) 验证码表加 purpose（login|reset 必须分开，登录码不能换改密）
-- 2) 新建 public.web_credential：智枢侧的密码覆盖值（bcrypt 哈希），不写老库 mz_user
-- 幂等；回滚见同名 -rollback 说明（删列 / 删表）。

alter table if exists x_observe_email_code
  add column if not exists purpose text not null default 'login';

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'x_observe_email_code_purpose_chk'
  ) then
    alter table x_observe_email_code
      add constraint x_observe_email_code_purpose_chk check (purpose in ('login', 'reset'));
  end if;
end $$;

create table if not exists public.web_credential (
  user_ref      text primary key,
  email         text not null,
  password_hash text not null,
  updated_at    timestamptz not null default now()
);

create index if not exists web_credential_email_idx on public.web_credential (lower(email));

comment on table public.web_credential is
  '智枢侧密码覆盖值（bcrypt）。存在则密码登录只认它，老站 mz_user 不受影响；删行即恢复老密码。';
