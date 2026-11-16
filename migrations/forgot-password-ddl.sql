-- 「忘记密码」建表 DDL（待账号主批红，**未执行**）
--
-- 为什么要这张表（三行）：
-- 1) 改密要能落一个「智枢侧的新密码」，而老库 mz_user 是明文且属于「不写老站任何表」的红线；
-- 2) 新建 public.web_credential 存 bcrypt 哈希，登录时先查它、命中就不再走老库密码；
-- 3) 删行即回滚到老密码，不动老站一行、不影响老站登录，可逆。
--
-- 同样待批的还有：x_observe_email_code 加 purpose 列（login|reset 分开）。
-- 若账号主不批新增列，可退化为「email 前缀 reset:<email>」实现，不碰结构但语义变脏。

create table if not exists public.web_credential (
  user_ref      text primary key,
  email         text not null,
  password_hash text not null,
  updated_at    timestamptz not null default now()
);

create index if not exists web_credential_email_idx on public.web_credential (lower(email));

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

-- 回滚（整份 DDL 的反向操作）
-- drop table if exists public.web_credential;
-- alter table x_observe_email_code drop constraint if exists x_observe_email_code_purpose_chk;
-- alter table x_observe_email_code drop column if exists purpose;

-- ⚠ 列说明（给后来人）：若采用「零 DDL」方案，x_observe_email_code.email 这一列**不纯**——
--   登录码存 `<email>`，改密码存 `reset:<email>`。任何读这张表的地方都必须走
--   api/internal/httpapi/web_reset.go 的 splitAddr()/joinAddr() 这一对入口，
--   不许自己 strings.HasPrefix(..., "reset:")；「最近给谁发过码」这类列表查询要
--   显式 `WHERE email NOT LIKE 'reset:%'` 才能只列登录码。
