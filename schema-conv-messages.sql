-- Full conversation messages, mirrored from omnihist into the online memory DB.
-- omni_id is the source SQLite row id and is the incremental sync cursor.
create table if not exists mem.conv_message (
  omni_id bigint primary key,
  conversation_id text not null,
  seq integer not null default 0,
  role text not null default '',
  kind text not null default '',
  created_at timestamptz,
  text text not null default '',
  content_hash text not null default '',
  meta jsonb,
  synced_at timestamptz not null default now()
);

create index if not exists conv_message_conv_idx
  on mem.conv_message (conversation_id, seq, omni_id);
create index if not exists conv_message_created_idx
  on mem.conv_message (created_at desc);
