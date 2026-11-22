alter table subscribers add column min_severity int not null default 1;

create table alert_events (
    id bigserial primary key,
    candidate_id bigint not null references candidates(id),
    tier int not null,
    severity int not null,
    newly_broken jsonb not null,
    created_at timestamptz not null default now(),
    unique (candidate_id, tier)
);
