from radar import cli
from radar.pipeline import seed_corpus


def test_seed_corpus_subcommand_dispatches(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("HF_TOKEN", "hf")
    for k in ("GITHUB_TOKEN", "OPENROUTER_API_KEY", "SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    seen = {}
    monkeypatch.setattr(seed_corpus, "run", lambda dsn, tok: seen.update(a=(dsn, tok)) or 7)
    cli.main(["seed-corpus"])
    assert "7" in capsys.readouterr().out
    assert seen["a"] == ("postgresql://x/y", "hf")
