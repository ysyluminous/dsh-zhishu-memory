-- 回滚：2026-11-16-growth-bill.sql
-- 只删本迁移建的表与索引；其它 schema 对象不动。
-- ⚠ 会连带删掉导入进来的账单行；老库 pim_bill 一行未动，可反复重导。
-- 幂等：if exists。

drop index if exists growth.bill_user_kind_idx;
drop index if exists growth.bill_user_time_idx;
drop table if exists growth.bill;
