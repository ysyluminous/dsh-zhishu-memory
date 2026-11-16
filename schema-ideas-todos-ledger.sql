-- ideas / todos / ledger for zhishu-memory
create table if not exists mem.idea (
  id text primary key,
  user_id text not null,
  title text not null default '',
  body text not null default '',
  status text not null default 'open',      -- open|planning|planned|executing|review|done
  priority integer not null default 2,
  parent_id text,
  decision text not null default '',
  decision_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idea_user_idx on mem.idea (user_id, status, updated_at desc);

create table if not exists mem.idea_step (
  id text primary key,
  idea_id text not null,
  user_id text not null,
  seq integer not null default 0,
  title text not null default '',
  status text not null default 'pending',    -- pending|doing|done|blocked
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idea_step_idx on mem.idea_step (idea_id, seq);

create table if not exists mem.todo (
  id text primary key,
  user_id text not null,
  title text not null default '',
  detail text not null default '',
  status text not null default 'pending',    -- pending|doing|done|canceled
  priority integer not null default 2,
  due_at timestamptz,
  project text not null default '',
  source text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  done_at timestamptz
);
create index if not exists todo_user_idx on mem.todo (user_id, status, due_at);

create table if not exists mem.ledger (
  id text primary key,
  user_id text not null,
  occurred_at timestamptz not null default now(),
  amount numeric(14,2) not null,
  currency text not null default 'CNY',
  category text not null default '',
  merchant text not null default '',
  note text not null default '',
  source text not null default '',
  created_at timestamptz not null default now()
);
create index if not exists ledger_user_idx on mem.ledger (user_id, occurred_at desc);
