-- unified ingestion queue for all sources
create table if not exists mem.queue_item (
  id text primary key,
  user_id text not null,
  source_type text not null,
  source_id text not null,
  title text not null default '',
  summary text not null default '',
  status text not null default 'pending',   -- pending|processing|ready|failed|skipped|canceled
  priority integer not null default 2,
  score numeric,
  tags text[] not null default '{}',
  meta jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  processed_at timestamptz,
  unique (user_id, source_type, source_id)
);
create index if not exists queue_user_status_idx on mem.queue_item (user_id, status, priority, created_at);
create index if not exists queue_source_idx on mem.queue_item (source_type, source_id);
