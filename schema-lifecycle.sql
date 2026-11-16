-- data lifecycle: source / extract / audit / trash / tombstone
create table if not exists mem.source_item (
  id text primary key,
  user_id text not null,
  source text not null,
  external_id text not null,
  raw_text text,
  raw_meta jsonb,
  content_hash text not null,
  score numeric,
  state text not null default 'ingest',
  purge_after timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, source, external_id)
);
create index if not exists source_item_user_idx on mem.source_item (user_id, state, score desc);
create index if not exists source_item_hash_idx on mem.source_item (content_hash);

create table if not exists mem.extract (
  id text primary key,
  user_id text not null,
  source_item_id text not null,
  kind text not null,
  title text not null default '',
  body text not null default '',
  confidence numeric,
  coverage jsonb,
  state text not null default 'extracted',
  created_at timestamptz not null default now()
);
create index if not exists extract_source_idx on mem.extract (source_item_id);
create index if not exists extract_user_idx on mem.extract (user_id, state);

create table if not exists mem.audit (
  id text primary key,
  user_id text not null,
  extract_id text,
  source_item_id text,
  auditor text not null,
  verdict text not null,
  findings jsonb,
  created_at timestamptz not null default now()
);
create index if not exists audit_extract_idx on mem.audit (extract_id);

create table if not exists mem.lifecycle (
  source_item_id text primary key,
  state text not null default 'active',
  reason text not null default '',
  actor text not null default '',
  purge_after timestamptz,
  updated_at timestamptz not null default now()
);

create table if not exists mem.tombstone (
  content_hash text primary key,
  source text not null default '',
  external_id text not null default '',
  purged_at timestamptz not null default now(),
  note text not null default ''
);
