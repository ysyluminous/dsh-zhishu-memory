-- prompt library + extraction templates
create table if not exists mem.prompt_library (
  id text primary key,
  ref_id text not null default '',
  title text not null default '',
  description text not null default '',
  content text not null default '',
  category text not null default '',
  tags jsonb,
  params jsonb,
  is_public text not null default '',
  user_id text,
  updated_at timestamptz not null default now()
);
create index if not exists prompt_library_category_idx on mem.prompt_library (category);
create index if not exists prompt_library_ref_idx on mem.prompt_library (ref_id);

create table if not exists mem.prompt_template (
  key text not null,
  version integer not null default 1,
  source_type text not null,
  title text not null default '',
  description text not null default '',
  template text not null,
  output_schema jsonb,
  model text not null default '',
  temperature numeric,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  primary key (key, version)
);
create index if not exists prompt_template_source_idx on mem.prompt_template (source_type, enabled);
