from radar import cli
from radar.pipeline import seed_corpus


def test_seed_corpus_subcommand_dispatches(monkeypatch, capsys):
    monkeypatch.setattr(seed_corpus, "run", lambda: 7)
    cli.main(["seed-corpus"])
    assert "7" in capsys.readouterr().out
