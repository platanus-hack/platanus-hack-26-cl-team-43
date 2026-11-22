"""Alert/digest rendering. Never include prompt text, embedded goals, repo paths,
file names or model output: only summaries plus a link to the gated dashboard."""
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from radar.alerts.rules import AlertDecision

SANTIAGO = ZoneInfo("America/Santiago")


@dataclass
class AlertMessage:
    subject: str
    text: str


def _pct(rate: float) -> str:
    return f"{round(rate * 100)}%"


def _source_domain(conn, candidate_id: int) -> str:
    row = conn.execute(
        "select r.url from sightings s join raw_items r on r.id = s.raw_item_id "
        "where s.candidate_id=%s order by s.seen_at, s.raw_item_id limit 1",
        (candidate_id,),
    ).fetchone()
    host = urlparse(row["url"]).hostname if row and row["url"] else None
    return host or "unknown"


def render_alert(conn, d: AlertDecision, base_url: str) -> AlertMessage:
    c = conn.execute(
        "select family_id, novelty, technique, first_seen_at from candidates where id=%s",
        (d.candidate_id,),
    ).fetchone()
    n = len(d.newly_broken)
    kind = "frontier" if d.tier == 1 else "open"
    prefix = "" if d.tier == 1 else "[open model] "
    subject = (f"{prefix}[Jailbreak Radar] New jailbreak broke {n} {kind} "
               f"model{'' if n == 1 else 's'}")
    family = ("new family" if c["novelty"] == "novel"
              else f"variant of family #{c['family_id']}")
    first_seen = c["first_seen_at"].astimezone(SANTIAGO).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"Techniques: {', '.join(c['technique'] or []) or 'unknown'}",
        f"Family: {family}",
        "Newly broken models:",
        *[f"  - {m}: {_pct(r)}" for m, r in d.newly_broken],
        f"Source: {_source_domain(conn, d.candidate_id)}",
        f"First seen: {first_seen} (America/Santiago)",
        f"Details (login required): {base_url}/candidates/{d.candidate_id}",
    ]
    return AlertMessage(subject, "\n".join(lines))


def render_digest(conn, since: datetime, base_url: str) -> AlertMessage:
    new = conn.execute(
        "select count(*) n from candidates where first_seen_at >= %s", (since,)
    ).fetchone()["n"]
    fams = conn.execute(
        "select count(*) n from families where first_seen_at >= %s", (since,)
    ).fetchone()["n"]
    per_model = conn.execute(
        "select model_id, count(*) n from breaks where confirmed_at >= %s "
        "group by model_id order by model_id", (since,),
    ).fetchall()
    events = conn.execute(
        "select candidate_id, severity, newly_broken from alert_events "
        "where created_at >= %s order by id", (since,),
    ).fetchall()
    lines = [
        f"New candidates: {new}",
        f"Novel families: {fams}",
        "Confirmed breaks per model:",
        *([f"  - {r['model_id']}: {r['n']}" for r in per_model] or ["  (none)"]),
        "Alerts:",
    ]
    for e in events:
        models = ", ".join(m for m, _ in e["newly_broken"])
        lines.append(f"  - severity {e['severity']}: {models} - "
                     f"{base_url}/candidates/{e['candidate_id']}")
    if not events:
        lines.append("  (none)")
    return AlertMessage("[Jailbreak Radar] Daily digest", "\n".join(lines))
