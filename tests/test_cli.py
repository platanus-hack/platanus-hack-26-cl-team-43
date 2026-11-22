import pytest

from conftest import TEST_DATABASE_URL
from radar import cli, main

SECRETS = ["GITHUB_TOKEN", "HF_TOKEN", "OPENROUTER_API_KEY", "EXTRACTOR_MODEL", "JUDGE_MODEL",
           "EMAIL_FROM", "SECRET_KEY", "BASE_URL"]


@pytest.fixture
def only_db(monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)


def test_migrate_needs_only_database_url(only_db, capsys):
    cli.main(["migrate"])
    assert "migrations applied" in capsys.readouterr().out


def test_poll_prints_inserted_count(only_db, monkeypatch, capsys):
    seen = {}

    def fake_poll(conn, settings, cfg, name):
        seen["args"] = (settings.github_token, settings.hf_token, name)
        return 3

    monkeypatch.setattr(main, "poll_once", fake_poll)
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    cli.main(["poll", "github_discovery"])
    assert capsys.readouterr().out.strip() == "github_discovery: 3 new raw items"
    assert seen["args"] == ("gh", None, "github_discovery")


def test_poll_rejects_unknown_collector(only_db):
    with pytest.raises(SystemExit):
        cli.main(["poll", "nope"])


def test_poll_github_requires_token(only_db):
    with pytest.raises(SystemExit, match="GITHUB_TOKEN"):
        cli.main(["poll", "github_watchlist"])
