-- judge errors are stored as 'unknown' (excluded from break rates)
alter table results drop constraint results_verdict_check;
alter table results add constraint results_verdict_check
    check (verdict in ('refused','partial','complied','unknown'));
alter table baselines drop constraint baselines_verdict_check;
alter table baselines add constraint baselines_verdict_check
    check (verdict in ('refused','partial','complied','unknown'));

-- baseline calls count against the daily budget
alter table baselines add column cost_usd double precision not null default 0;

alter table models alter column tier type int using tier::int;

create index test_jobs_claim_idx on test_jobs (status, priority desc, id);
