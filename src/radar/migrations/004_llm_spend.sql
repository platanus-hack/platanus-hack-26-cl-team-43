-- non-tester LLM spend (extraction) that counts against the daily budget
create table llm_spend (
    id bigserial primary key,
    kind text not null,
    cost_usd double precision not null,
    created_at timestamptz not null default now()
);

-- poison-pill guard: raw items that keep crashing processing are given up on
alter table raw_items add column process_attempts int not null default 0;
