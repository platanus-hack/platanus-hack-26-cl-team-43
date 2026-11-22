"""Read queries for the dashboard pages."""
from radar.testing.budget import _santiago_midnight, can_spend, spent_today

COLLECTORS = ("github_discovery", "github_watchlist", "github_code_search", "hf_datasets", "papers")


def candidate_status(conn, candidate_id: int, budget_ok: bool) -> str:
    row = conn.execute(
        "select c.test_status, c.untested_reason,"
        " (select count(*) from breaks b where b.candidate_id = c.id) as breaks,"
        " (select count(*) from test_jobs j where j.candidate_id = c.id and j.status='running') as running,"
        " (select count(*) from test_jobs j where j.candidate_id = c.id and j.status='queued') as queued"
        " from candidates c where c.id = %s",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return "queued"
    if row["running"]:
        return "testing"
    if row["breaks"]:
        return f"broke {row['breaks']} models"
    if row["test_status"] == "untested":
        return f"untested: {row['untested_reason'] or 'unknown'}"
    if row["queued"]:
        return "queued" if budget_ok else "paused by budget"
    if row["test_status"] == "tested":
        return "no break"
    return "queued"


def counters(conn, cap_usd: float) -> dict:
    since = _santiago_midnight()
    one = lambda sql: conn.execute(sql, (since,)).fetchone()["n"]  # noqa: E731
    return {
        "raw_items": one("select count(*) n from raw_items where fetched_at >= %s"),
        "candidates": one("select count(*) n from candidates where first_seen_at >= %s"),
        "novel": one("select count(*) n from candidates where novelty='novel' and first_seen_at >= %s"),
        "breaks": one("select count(*) n from breaks where confirmed_at >= %s"),
        "spent": spent_today(conn),
        "cap": cap_usd,
    }


def tier1_models(conn) -> list[str]:
    return [r["id"] for r in conn.execute(
        "select id from models where tier = 1 and enabled order by id")]


def matrix(conn, models: list[str]) -> list[dict]:
    fams = conn.execute(
        "select f.id, f.label from families f where exists ("
        " select 1 from candidates c where c.family_id = f.id and c.test_status='tested')"
        " order by f.id desc limit 30").fetchall()
    ids = [f["id"] for f in fams]
    best = {}
    if ids:
        for r in conn.execute(
            "select c.family_id, b.model_id, max(b.break_rate) r from breaks b"
            " join candidates c on c.id = b.candidate_id where c.family_id = any(%s)"
            " group by 1, 2", (ids,)):
            best[(r["family_id"], r["model_id"])] = r["r"]
    return [{"id": f["id"], "label": f["label"],
             "cells": [best.get((f["id"], m), 0.0) for m in models]} for f in fams]


def robustness(conn, models: list[str]) -> list[dict]:
    total = conn.execute(
        "select count(distinct family_id) n from candidates where test_status='tested' and family_id is not null"
    ).fetchone()["n"]
    broke = {r["model_id"]: r["n"] for r in conn.execute(
        "select b.model_id, count(distinct c.family_id) n from breaks b"
        " join candidates c on c.id = b.candidate_id where c.family_id is not null group by 1")}
    out = [{"model": m, "broke": broke.get(m, 0), "total": total,
            "share": (broke.get(m, 0) / total) if total else 0.0} for m in models]
    return sorted(out, key=lambda x: (x["share"], x["model"]))


def feed(conn, cap_usd: float, limit: int = 100) -> list[dict]:
    budget_ok = can_spend(conn, cap_usd)
    rows = conn.execute(
        "select c.id, c.text, c.novelty, c.first_seen_at,"
        " (select r.source from sightings s join raw_items r on r.id = s.raw_item_id"
        "   where s.candidate_id = c.id order by s.seen_at, s.raw_item_id limit 1) as source"
        " from candidates c order by c.id desc limit %s", (limit,)).fetchall()
    for r in rows:
        r["status"] = candidate_status(conn, r["id"], budget_ok)
    return rows


def candidate_detail(conn, cid: int, cap_usd: float) -> dict | None:
    c = conn.execute("select * from candidates where id=%s", (cid,)).fetchone()
    if c is None:
        return None
    c.pop("embedding", None)
    return {
        "c": c,
        "status": candidate_status(conn, cid, can_spend(conn, cap_usd)),
        "sightings": conn.execute(
            "select r.url, r.source, r.author, s.seen_at from sightings s"
            " join raw_items r on r.id = s.raw_item_id where s.candidate_id=%s"
            " order by s.seen_at", (cid,)).fetchall(),
        "variants": conn.execute(
            "select id, novelty, left(text, 120) as snippet from candidates"
            " where family_id = %s and id <> %s order by id", (c["family_id"], cid)).fetchall()
        if c["family_id"] else [],
        "results": conn.execute(
            "select j.model_id, j.stage, j.status as job_status, r.goal_id, r.attempt,"
            " r.verdict, r.output_trunc from test_jobs j left join results r on r.job_id = j.id"
            " where j.candidate_id=%s order by j.model_id, j.stage, r.goal_id, r.attempt",
            (cid,)).fetchall(),
        "breaks": conn.execute(
            "select model_id, break_rate from breaks where candidate_id=%s order by model_id",
            (cid,)).fetchall(),
    }


def sources(conn) -> dict:
    state = {r["name"]: r for r in conn.execute("select * from collector_state")}
    items = {r["source"]: r["n"] for r in conn.execute(
        "select source, count(*) n from raw_items where fetched_at >= now() - interval '24 hours'"
        " group by 1")}
    names = list(COLLECTORS) + sorted(n for n in state if n not in COLLECTORS)
    health = [{"name": n, "last_run_at": (state.get(n) or {}).get("last_run_at"),
               "last_error": (state.get(n) or {}).get("last_error"),
               "items_24h": items.get(n, 0)} for n in names]
    watch = conn.execute("select full_name, status, added_at from watch_repos order by added_at desc").fetchall()
    return {"health": health, "watch": watch}
