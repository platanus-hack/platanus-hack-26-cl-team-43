"""Unit tests for the Hackathon Demo Seeder CLI."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from app import cli


class InMemorySeedDB:
    """Stateful in-memory handler simulating PostgreSQL tables for demo seeding."""

    def __init__(self) -> None:
        self.model_releases: list[dict[str, Any]] = []
        self.eval_datasets: list[dict[str, Any]] = []
        self.eval_results: list[dict[str, Any]] = []
        self.blog_posts: list[dict[str, Any]] = []

        self._model_id_seq = 0
        self._dataset_id_seq = 0
        self._eval_result_id_seq = 0
        self._blog_post_id_seq = 0

    def __call__(self, query: str, params: Any = None) -> Any:
        normalized = " ".join(query.strip().lower().split())

        if normalized.startswith("insert into model_releases"):
            model_name, provider, release_date, audited = params
            for row in self.model_releases:
                if row["model_name"] == model_name and row["provider"] == provider:
                    row["audited"] = audited
                    return {"id": row["id"]}
            self._model_id_seq += 1
            new_row = {
                "id": self._model_id_seq,
                "model_name": model_name,
                "provider": provider,
                "release_date": release_date,
                "audited": audited,
            }
            self.model_releases.append(new_row)
            return {"id": new_row["id"]}

        if normalized.startswith("insert into eval_datasets"):
            category, prompt_text, source = params
            for row in self.eval_datasets:
                if row["category"] == category and row["prompt_text"] == prompt_text:
                    row["source"] = source
                    return {"id": row["id"]}
            self._dataset_id_seq += 1
            new_row = {
                "id": self._dataset_id_seq,
                "category": category,
                "prompt_text": prompt_text,
                "source": source,
            }
            self.eval_datasets.append(new_row)
            return {"id": new_row["id"]}

        if normalized.startswith("select id from eval_results"):
            model_id, dataset_id = params
            for row in self.eval_results:
                if row["model_id"] == model_id and row["dataset_id"] == dataset_id:
                    return {"id": row["id"]}
            return None

        if normalized.startswith("insert into eval_results"):
            model_id, dataset_id, passed, score, logs, cost_usd = params
            assert 0.0 <= float(score) <= 1.0, f"Score out of bounds: {score}"
            self._eval_result_id_seq += 1
            new_row = {
                "id": self._eval_result_id_seq,
                "model_id": model_id,
                "dataset_id": dataset_id,
                "passed": passed,
                "score": float(score),
                "logs": logs,
                "cost_usd": float(cost_usd),
            }
            self.eval_results.append(new_row)
            return {"id": new_row["id"]}

        if normalized.startswith("insert into blog_posts"):
            title, slug, summary, markdown_body, eval_id, hours_ago = params
            for row in self.blog_posts:
                if row["slug"] == slug:
                    return None
            self._blog_post_id_seq += 1
            new_row = {
                "id": self._blog_post_id_seq,
                "title": title,
                "slug": slug,
                "summary": summary,
                "markdown_body": markdown_body,
                "eval_id": eval_id,
                "status": "published",
                "hours_ago": hours_ago,
            }
            self.blog_posts.append(new_row)
            return {"id": new_row["id"]}

        raise AssertionError(f"Unexpected SQL query in InMemorySeedDB: {query}")


def test_seed_demo_data_first_run_populates_all_tables(
    make_fake_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = InMemorySeedDB()
    fake_conn, _fake_cursor, fake_get_db_connection = make_fake_db(handler=db)
    monkeypatch.setattr(cli, "get_db_connection", fake_get_db_connection)

    summary = cli.seed_demo_data()

    # Summary checks
    assert summary.models_total == 5
    assert summary.models_audited == 3
    assert summary.models_unaudited == 2
    assert summary.datasets_total == 5
    assert summary.eval_results_inserted == 15
    assert summary.eval_results_existing == 0
    assert summary.eval_results_total == 15
    assert summary.blog_posts_inserted == 3
    assert summary.blog_posts_existing == 0
    assert summary.blog_posts_total == 3
    assert fake_conn.commit_count == 1

    # Model releases checks
    assert len(db.model_releases) == 5
    audited_models = [row for row in db.model_releases if row["audited"]]
    unaudited_models = [row for row in db.model_releases if not row["audited"]]
    assert len(audited_models) == 3
    assert len(unaudited_models) == 2

    # Eval datasets checks (>= 4 categories)
    categories = {row["category"] for row in db.eval_datasets}
    assert len(db.eval_datasets) == 5
    assert len(categories) >= 4

    # Eval results checks: 15 rows across all 3 audited models with varied pass rates & valid scores
    assert len(db.eval_results) == 15
    audited_ids = {row["id"] for row in audited_models}
    pass_counts_by_model: dict[int, int] = {model_id: 0 for model_id in audited_ids}
    total_cost_usd = 0.0

    for row in db.eval_results:
        assert row["model_id"] in audited_ids
        assert 0.0 <= row["score"] <= 1.0
        assert row["cost_usd"] > 0.0
        assert row["logs"].strip() != ""
        total_cost_usd += row["cost_usd"]
        if row["passed"]:
            pass_counts_by_model[row["model_id"]] += 1

    # Pass rates should vary across the 3 audited models and total cost stays well below $5.00
    assert len(set(pass_counts_by_model.values())) == 3
    assert total_cost_usd < 5.0

    # Blog posts checks: 3 published Markdown posts linked to seeded eval_results
    assert len(db.blog_posts) == 3
    eval_result_ids = {row["id"] for row in db.eval_results}
    raw_html_pattern = re.compile(r"<[a-zA-Z][^>]*>")
    for post in db.blog_posts:
        assert post["status"] == "published"
        assert post["eval_id"] in eval_result_ids
        assert post["markdown_body"].startswith("# ")
        assert "## Category Scorecard" in post["markdown_body"]
        assert "| Category | Status | Score | Cost (USD) |" in post["markdown_body"]
        assert not raw_html_pattern.search(post["markdown_body"])


def test_seed_demo_data_is_idempotent_across_reruns(
    make_fake_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = InMemorySeedDB()
    fake_conn, _fake_cursor, fake_get_db_connection = make_fake_db(handler=db)
    monkeypatch.setattr(cli, "get_db_connection", fake_get_db_connection)

    first = cli.seed_demo_data()
    second = cli.seed_demo_data()

    assert first.eval_results_inserted == 15
    assert first.blog_posts_inserted == 3

    assert second.models_total == 5
    assert second.datasets_total == 5
    assert second.eval_results_inserted == 0
    assert second.eval_results_existing == 15
    assert second.eval_results_total == 15
    assert second.blog_posts_inserted == 0
    assert second.blog_posts_existing == 3
    assert second.blog_posts_total == 3

    assert len(db.model_releases) == 5
    assert len(db.eval_datasets) == 5
    assert len(db.eval_results) == 15
    assert len(db.blog_posts) == 3
    assert fake_conn.commit_count == 2


def test_seed_demo_data_upgrades_preseeded_unaudited_models(
    make_fake_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = InMemorySeedDB()
    # Simulate rows pre-seeded by db/init.sql with audited=False
    db.model_releases.extend(
        [
            {
                "id": 101,
                "model_name": "gpt-4o-mini",
                "provider": "openai",
                "release_date": "2025-01-15",
                "audited": False,
            },
            {
                "id": 102,
                "model_name": "claude-3-5-haiku",
                "provider": "anthropic",
                "release_date": "2025-02-01",
                "audited": False,
            },
            {
                "id": 103,
                "model_name": "llama-3.1-8b-instruct",
                "provider": "meta",
                "release_date": "2025-02-10",
                "audited": False,
            },
        ]
    )
    db._model_id_seq = 103

    fake_conn, _fake_cursor, fake_get_db_connection = make_fake_db(handler=db)
    monkeypatch.setattr(cli, "get_db_connection", fake_get_db_connection)

    summary = cli.seed_demo_data()

    assert summary.models_total == 5
    assert summary.models_audited == 3
    assert summary.models_unaudited == 2
    assert len(db.model_releases) == 5
    assert fake_conn.commit_count == 1

    audited_by_name = {row["model_name"]: row["audited"] for row in db.model_releases}
    assert audited_by_name["gpt-4o-mini"] is True
    assert audited_by_name["claude-3-5-haiku"] is True
    assert audited_by_name["llama-3.1-8b-instruct"] is True
    assert audited_by_name["qwen-2.5-72b-instruct"] is False
    assert audited_by_name["mistral-large-2411"] is False


def test_cli_main_demo_seed_text_and_json_output(
    make_fake_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = InMemorySeedDB()
    _fake_conn, _fake_cursor, fake_get_db_connection = make_fake_db(handler=db)
    close_calls = 0

    def fake_close_db_pool() -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(cli, "get_db_connection", fake_get_db_connection)
    monkeypatch.setattr(cli, "close_db_pool", fake_close_db_pool)

    # First run: human-readable output
    exit_code_text = cli.main(["demo-seed"])
    assert exit_code_text == 0
    assert close_calls == 1
    stdout_text = capsys.readouterr().out
    assert "Demo seed complete:" in stdout_text
    assert "models=5" in stdout_text
    assert "eval_results=15" in stdout_text
    assert "blog_posts=3" in stdout_text

    # Second run: JSON output (idempotent rerun)
    exit_code_json = cli.main(["demo-seed", "--json"])
    assert exit_code_json == 0
    assert close_calls == 2
    stdout_json = capsys.readouterr().out
    payload = json.loads(stdout_json)
    assert payload == {
        "models_total": 5,
        "models_audited": 3,
        "models_unaudited": 2,
        "datasets_total": 5,
        "eval_results_inserted": 0,
        "eval_results_existing": 15,
        "eval_results_total": 15,
        "blog_posts_inserted": 0,
        "blog_posts_existing": 3,
        "blog_posts_total": 3,
    }
