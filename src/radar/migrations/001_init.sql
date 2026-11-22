create extension if not exists vector;

create table collector_state (
    name text primary key,
    cursor jsonb,
    last_run_at timestamptz,
    last_error text
);

create table watch_repos (
    full_name text primary key,
    status text not null check (status in ('seed','auto','from_paper','muted')),
    added_at timestamptz not null default now()
);

create table raw_items (
    id bigserial primary key,
    source text not null,
    external_id text not null,
    url text,
    author text,
    license text,
    published_at timestamptz,
    fetched_at timestamptz not null default now(),
    content_text text,
    content_hash text not null,
    kind text not null default 'text',
    processed_at timestamptz,
    unique (source, external_id, content_hash)
);

create table families (
    id bigserial primary key,
    label text,
    first_seen_at timestamptz not null default now()
);

create table candidates (
    id bigserial primary key,
    family_id bigint references families(id),
    text text not null,
    template text,
    technique text[],
    targets text[],
    format text,
    goal_slot text,
    embedded_goal text,
    embedded_goal_severity text,
    template_severity text,
    embedding vector(384),
    novelty text check (novelty in ('novel','variant','known')),
    first_seen_at timestamptz not null default now(),
    test_status text,
    untested_reason text
);
create index candidates_embedding_idx on candidates
    using hnsw (embedding vector_cosine_ops);

create table sightings (
    candidate_id bigint not null references candidates(id),
    raw_item_id bigint not null references raw_items(id),
    seen_at timestamptz not null default now(),
    primary key (candidate_id, raw_item_id)
);

create table models (
    id text primary key,
    openrouter_id text not null,
    tier text,
    enabled boolean not null default true
);

create table goals (
    id text primary key,
    kind text not null check (kind in ('canary','policy')),
    text text not null,
    rubric text
);

create table test_jobs (
    id bigserial primary key,
    candidate_id bigint not null references candidates(id),
    model_id text not null references models(id),
    stage text not null check (stage in ('screen','confirm')),
    priority int not null default 0,
    status text not null default 'queued'
        check (status in ('queued','running','done','failed')),
    attempts int not null default 0,
    not_before timestamptz,
    locked_at timestamptz
);

create table results (
    id bigserial primary key,
    job_id bigint not null references test_jobs(id),
    goal_id text references goals(id),
    attempt int not null default 1,
    verdict text not null check (verdict in ('refused','partial','complied')),
    output_trunc text,
    cost_usd double precision,
    latency_ms int,
    created_at timestamptz not null default now()
);

create table baselines (
    model_id text not null references models(id),
    goal_id text not null references goals(id),
    date date not null,
    verdict text not null check (verdict in ('refused','partial','complied')),
    primary key (model_id, goal_id, date)
);

create table breaks (
    candidate_id bigint not null references candidates(id),
    model_id text not null references models(id),
    break_rate double precision not null,
    confirmed_at timestamptz not null default now(),
    primary key (candidate_id, model_id)
);

create table subscribers (
    id bigserial primary key,
    email text,
    phone text,
    channels text[],
    active boolean not null default true
);

create table alerts (
    id bigserial primary key,
    candidate_id bigint not null references candidates(id),
    channel text not null,
    sent_at timestamptz,
    status text
);
