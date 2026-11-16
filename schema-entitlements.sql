-- P0 identity/entitlement skeleton
create table if not exists mem.content_policy (
  content_type text not null,
  content_id text not null,
  visibility text not null default 'public',   -- public|login|vip|denied
  region text not null default 'cn',           -- cn|overseas
  paywall text not null default 'free',        -- free|points|vip
  teaser text not null default 'title',        -- title|summary|none
  owner_user_id text,
  updated_at timestamptz not null default now(),
  primary key (content_type, content_id)
);

create table if not exists mem.content_grant (
  user_id text not null,
  content_type text not null,
  content_id text not null,
  grant_type text not null default 'allow',    -- allow|deny
  expires_at timestamptz,
  note text not null default '',
  primary key (user_id, content_type, content_id)
);

create table if not exists mem.plan_entitlement (
  plan text not null,
  capability text not null,
  limit_value integer,
  period text not null default 'day',
  primary key (plan, capability)
);

create table if not exists mem.subscription (
  id text primary key,
  user_id text not null,
  plan text not null,
  status text not null default 'active',       -- active|past_due|canceled|expired
  period_start timestamptz,
  period_end timestamptz,
  source text not null default 'manual',
  external_id text,
  created_at timestamptz not null default now()
);
create index if not exists subscription_user_idx on mem.subscription (user_id, status, period_end desc);

create table if not exists mem.usage_counter (
  user_id text not null,
  metric text not null,
  period text not null,
  used integer not null default 0,
  primary key (user_id, metric, period)
);

insert into mem.plan_entitlement (plan, capability, limit_value, period) values
  ('free','read_overseas',0,'total'),
  ('free','search',50,'day'),
  ('free','ai_chat',20,'day'),
  ('free','download',3,'day'),
  ('vip','read_overseas',1,'total'),
  ('vip','search',null,'day'),
  ('vip','ai_chat',500,'day'),
  ('vip','download',50,'day'),
  ('team','read_overseas',1,'total'),
  ('team','search',null,'day'),
  ('team','ai_chat',2000,'day'),
  ('team','download',200,'day')
on conflict (plan, capability) do update
  set limit_value = excluded.limit_value, period = excluded.period;
