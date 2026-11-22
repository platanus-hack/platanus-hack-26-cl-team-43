from datetime import datetime, timedelta, timezone

from radar.testing.goals import ModelSpec, sync_config


def setup(conn):
    sync_config(conn, [], [ModelSpec("t1", "p/1", 1, True), ModelSpec("t1b", "p/1b", 1, True),
                           ModelSpec("t2", "p/2", 2, True)])
    conn.commit()


def seed_candidate(conn, *, text="JB mode on. {goal} now.", embedded_goal=None,
                   novelty="novel", source="github", url="https://github.com/a/b/blob/x/f.md",
                   technique=("roleplay",), **_) -> int:
    cid = conn.execute(
        "insert into candidates(text, embedded_goal, novelty, technique) "
        "values (%s,%s,%s,%s) returning id", (text, embedded_goal, novelty, list(technique)),
    ).fetchone()["id"]
    rid = conn.execute(
        "insert into raw_items(source, external_id, url, content_hash) values (%s,%s,%s,'h') "
        "returning id", (source, f"x{cid}", url),
    ).fetchone()["id"]
    conn.execute("insert into sightings(candidate_id, raw_item_id) values (%s,%s)", (cid, rid))
    conn.commit()
    return cid


def family(conn) -> int:
    fid = conn.execute("insert into families(label) values ('f') returning id").fetchone()["id"]
    conn.commit()
    return fid


def candidate(conn, fid=None, **kw) -> int:
    cid = seed_candidate(conn, **kw)
    if fid:
        conn.execute("update candidates set family_id=%s where id=%s", (fid, cid))
        conn.commit()
    return cid


def brk(conn, cid, model_id, rate=0.8, minutes_ago=0):
    conn.execute(
        "insert into breaks(candidate_id, model_id, break_rate, confirmed_at) values (%s,%s,%s,%s)",
        (cid, model_id, rate, datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)),
    )
    conn.commit()
