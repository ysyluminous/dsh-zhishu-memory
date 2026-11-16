-- Live events captured at the turn boundary by hooks/MCP/skills.
-- Kept separate from omnihist-mirrored messages so a later sync can reconcile
-- without double counting.
create table if not exists mem.live_event (
  id bigserial primary key,
  event_id text not null unique,
  source text not null,
  conversation_id text not null,
  seq integer not null default 0,
  role text not null default 'user',
  text text not null default '',
  content_hash text not null default '',
  created_at timestamptz not null default now(),
  received_at timestamptz not null default now(),
  reconciled_at timestamptz,
  meta jsonb
);

create index if not exists live_event_conv_idx
  on mem.live_event (conversation_id, seq, id);
create index if not exists live_event_recent_idx
  on mem.live_event (received_at desc);
