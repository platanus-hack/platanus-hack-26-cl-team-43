from radar.settings import Settings


def test_migrate_creates_tables(conn):
    rows = conn.execute("select tablename from pg_tables where schemaname='public'").fetchall()
    names = {r["tablename"] for r in rows}
    assert {"raw_items", "candidates", "families", "sightings", "test_jobs",
            "results", "baselines", "breaks", "subscribers", "alerts",
            "watch_repos", "collector_state", "models", "goals"} <= names


def test_raw_item_unique(conn):
    q = "insert into raw_items(source, external_id, url, content_text, content_hash) values ('s','e','u','t','h') on conflict do nothing"
    conn.execute(q); conn.execute(q)
    assert conn.execute("select count(*) c from raw_items").fetchone()["c"] == 1


def test_vector_roundtrip(conn):
    conn.execute(
        "insert into candidates(text, embedding) values ('x', %s)", ([0.1] * 384,)
    )
    row = conn.execute("select embedding from candidates").fetchone()
    assert len(row["embedding"].to_list()) == 384


def test_allowed_emails_comma_separated(monkeypatch):
    env = dict(
        DATABASE_URL="d", GITHUB_TOKEN="g", OPENROUTER_API_KEY="o",
        EXTRACTOR_MODEL="e", JUDGE_MODEL="j", EMAIL_FROM="f",
        SECRET_KEY="ssssssssssssssssssssssssssssssss", BASE_URL="b", ALLOWED_EMAILS="a@x.com, b@y.com",
    )
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.allowed_emails == ["a@x.com", "b@y.com"]
    assert s.daily_budget_usd == 20.0


def test_testing_migration_allows_unknown_and_int_tier(conn):
    conn.execute("insert into models(id, openrouter_id, tier) values ('m','a/b',2)")
    conn.execute("insert into goals(id, kind, text) values ('g','canary','t')")
    conn.execute(
        "insert into baselines(model_id, goal_id, date, verdict, cost_usd) "
        "values ('m','g',current_date,'unknown',0.01)"
    )
    conn.execute("insert into candidates(text) values ('x')")
    conn.execute("insert into test_jobs(candidate_id, model_id, stage) values (1,'m','screen')")
    conn.execute("insert into results(job_id, goal_id, verdict) values (1,'g','unknown')")
    assert conn.execute("select tier from models").fetchone()["tier"] == 2
    assert conn.execute("select cost_usd from baselines").fetchone()["cost_usd"] == 0.01
