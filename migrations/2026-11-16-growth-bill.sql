-- 账单表 growth.bill（账号主 2026-11-16 批红）
-- 迁移日期：2026-11-16
-- 归属：智枢 zhishu 库 growth schema
-- 幂等：表/索引 if not exists，可重复执行
-- 回滚：见 2026-11-16-growth-bill-rollback.sql
--
-- 依据 docs/org/tech/bills-migration-plan.md：只新增 1 张表，分类/账户都不建表
-- （分类走文本快照、账户只存显示名，payment/account 敏感字段一律不迁）。
-- 导入工具：api/cmd/billsimport（source='legacy_pim_bill'，幂等键 user_id+legacy_id）。

create table if not exists growth.bill (
  id              bigserial primary key,
  user_id         text not null,
  legacy_id       text,
  title           text not null,
  amount          numeric(12,2) not null check (amount >= 0),
  currency        char(3) not null default 'CNY',
  kind            text not null check (kind in ('income','expense','transfer')),
  category_key    text,
  category_title  text,
  category_mapped boolean not null default true,
  account_label   text,
  occurred_at     timestamptz not null,
  note            text,
  source          text not null default 'manual',
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint bill_user_legacy_uk unique (user_id, legacy_id)
);

create index if not exists bill_user_time_idx on growth.bill (user_id, occurred_at desc);
create index if not exists bill_user_kind_idx on growth.bill (user_id, kind);

comment on table growth.bill is
  '账单事件（金额）。老站 pim_bill 迁入，source=legacy_pim_bill；分类/账户不建表，只存文本快照。';
comment on column growth.bill.legacy_id is '老库 pim_bill.id，导入幂等键（配 user_id 唯一）。';
comment on column growth.bill.category_mapped is
  'false = 老分类对不上分类表（英文枚举等），页面标「未归类」，标题照抄原文不翻译。';
