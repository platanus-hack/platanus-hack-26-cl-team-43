from fakes import FakeEmbedder, unit

from radar.pipeline.seed_corpus import seed_known


def test_seed_known_creates_known_candidates_in_batches(conn):
    rows = [f"jailbreak number {i}" for i in range(130)] + ["", "   "]
    vectors = {f"jailbreak number {i}": unit([1.0] + [0.0] * i + [1.0] + [0.0] * (382 - i))
               for i in range(130)}
    emb = FakeEmbedder(vectors)
    n = seed_known(conn, emb, rows, "https://huggingface.co/datasets/x")
    assert n == 130
    assert [len(c) for c in emb.calls] == [64, 64, 2]
    c = conn.execute(
        "select count(*) n, count(distinct novelty) k, min(novelty) nov, "
        "bool_and(technique = '{other}') t, bool_and(template_severity = 'low') s "
        "from candidates").fetchone()
    assert (c["n"], c["k"], c["nov"], c["t"], c["s"]) == (130, 1, "known", True, True)
    raw = conn.execute(
        "select count(*) n, bool_and(processed_at is not null) p, min(source) s, min(url) u "
        "from raw_items").fetchone()
    assert (raw["n"], raw["p"], raw["s"], raw["u"]) == (
        130, True, "seed_corpus", "https://huggingface.co/datasets/x")
    assert conn.execute("select count(*) c from sightings").fetchone()["c"] == 130


def test_seed_known_is_idempotent(conn):
    emb = FakeEmbedder.same_vector()
    seed_known(conn, emb, ["a prompt"], "u")
    assert seed_known(conn, emb, ["a prompt"], "u") == 0
    assert conn.execute("select count(*) c from candidates").fetchone()["c"] == 1
