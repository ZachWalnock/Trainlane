create table if not exists runs (
    id uuid primary key,
    status text not null,
    script_path text not null,
    backend text not null,
    image_tag text,
    started_at timestamptz not null,
    ended_at timestamptz,
    error_summary text,
    metadata jsonb not null default '{}'::jsonb
);

create table if not exists events (
    id bigserial primary key,
    run_id uuid not null references runs(id) on delete cascade,
    ts timestamptz not null,
    event_type text not null,
    key text,
    value_json jsonb not null,
    step bigint,
    source text not null default 'sdk'
);

create index if not exists events_run_id_id_idx on events(run_id, id);
