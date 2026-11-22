from datetime import UTC, datetime

from fakes import FakeEmbedder, unit
from helpers import insert_raw

from radar.pipeline.dedupe import classify, ingest_prompt, normalize
from radar.pipeline.extract import ExtractedPrompt

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def ep(text="Do X now [GOAL]", technique=("roleplay",), goal_slot="[GOAL]") -> ExtractedPrompt:
    return ExtractedPrompt(text=text, confidence=0.9, technique=list(technique),
                           targets=["any"], format="single_turn", goal_slot=goal_slot,
                           embedded_goal=None, embedded_goal_severity=None,
                           template_severity="low")


def vec(*head):
    return unit(list(head) + [0.0] * (384 - len(head)))


def test_classify_thresholds():
    assert classify(0.96) == "duplicate"; assert classify(0.95) == "duplicate"
    assert classify(0.90) == "variant"; assert classify(0.85) == "variant"
    assert classify(0.84) == "novel"; assert classify(None) == "novel"


def test_normalize_replaces_slot():
    assert normalize("Hi  [INSERT PROMPT HERE] now", "[INSERT PROMPT HERE]") == "hi {GOAL} now"


def test_normalize_without_slot():
    assert normalize("  A\n\tB  ", None) == "a b"


def test_novel_creates_family_and_candidate(conn):
    rid = insert_raw(conn, text="t")
    emb = FakeEmbedder(default=vec(1))
    out = ingest_prompt(conn, emb, ep(), rid, T0)
    assert out.novelty == "novel"
    cand = conn.execute("select * from candidates where id=%s", (out.candidate_id,)).fetchone()
    assert cand["template"] == "do x now {GOAL}"
    assert cand["novelty"] == "novel"
    assert cand["first_seen_at"] == T0
    assert emb.calls == [["do x now {GOAL}"]]
    fam = conn.execute("select * from families where id=%s", (cand["family_id"],)).fetchone()
    assert fam["label"] == "roleplay"
    s = conn.execute("select * from sightings").fetchall()
    assert [(r["candidate_id"], r["raw_item_id"], r["seen_at"]) for r in s] == [
        (out.candidate_id, rid, T0)]


def test_novel_without_technique_label_other(conn):
    rid = insert_raw(conn, text="t")
    out = ingest_prompt(conn, FakeEmbedder(default=vec(1)), ep(technique=()), rid, T0)
    label = conn.execute(
        "select f.label from families f join candidates c on c.family_id=f.id where c.id=%s",
        (out.candidate_id,)).fetchone()["label"]
    assert label == "other"


def test_duplicate_adds_sighting_and_keeps_earliest(conn):
    r1, r2 = insert_raw(conn, text="a"), insert_raw(conn, text="b")
    emb = FakeEmbedder.same_vector()
    first = ingest_prompt(conn, emb, ep(), r1, T0)
    earlier = datetime(2026, 10, 1, tzinfo=UTC)
    dup = ingest_prompt(conn, emb, ep(text="do x now [GOAL]"), r2, earlier)
    assert dup.novelty == "duplicate" and dup.candidate_id == first.candidate_id
    assert conn.execute("select count(*) c from candidates").fetchone()["c"] == 1
    assert conn.execute("select count(*) c from sightings").fetchone()["c"] == 2
    fs = conn.execute("select first_seen_at from candidates").fetchone()["first_seen_at"]
    assert fs == earlier


def test_variant_joins_neighbour_family(conn):
    r1, r2 = insert_raw(conn, text="a"), insert_raw(conn, text="b")
    emb = FakeEmbedder({"aaa": vec(1), "bbb": vec(0.9, (1 - 0.81) ** 0.5)})
    a = ingest_prompt(conn, emb, ep(text="aaa", goal_slot=None), r1, T0)
    b = ingest_prompt(conn, emb, ep(text="bbb", goal_slot=None), r2, T0)
    assert (a.novelty, b.novelty) == ("novel", "variant")
    fams = conn.execute("select family_id, novelty from candidates order by id").fetchall()
    assert fams[0]["family_id"] == fams[1]["family_id"]
    assert fams[1]["novelty"] == "variant"
    assert conn.execute("select count(*) c from families").fetchone()["c"] == 1


def test_known_forces_known_novelty(conn):
    rid = insert_raw(conn, text="a")
    out = ingest_prompt(conn, FakeEmbedder(default=vec(1)), ep(), rid, T0, known=True)
    assert out.novelty == "known"
    assert conn.execute("select novelty from candidates").fetchone()["novelty"] == "known"


def test_precomputed_embedding_skips_embedder(conn):
    rid = insert_raw(conn, text="a")
    emb = FakeEmbedder()
    out = ingest_prompt(conn, emb, ep(), rid, T0, embedding=vec(1))
    assert out.novelty == "novel" and emb.calls == []
