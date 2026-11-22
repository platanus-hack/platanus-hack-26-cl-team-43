from dataclasses import dataclass

from psycopg.types.json import Jsonb


@dataclass
class AlertDecision:
    candidate_id: int
    tier: int
    severity: int  # tier 2 -> 0
    newly_broken: list[tuple[str, float]]  # (model display id, break_rate)


_NEWLY_BROKEN = """
select b.model_id, b.break_rate
from breaks b
join models m on m.id = b.model_id
join candidates c on c.id = b.candidate_id
where b.candidate_id = %s and m.tier = %s
  and not exists (
      select 1 from breaks b2
      join candidates c2 on c2.id = b2.candidate_id
      where c.family_id is not null and c2.family_id = c.family_id
        and c2.id <> c.id and b2.model_id = b.model_id
        and b2.confirmed_at < b.confirmed_at)
order by b.model_id
"""


def evaluate_alerts(conn, candidate_id: int, tier: int) -> AlertDecision | None:
    """Decide whether this candidate's breaks on `tier` warrant an alert.

    Records an alert_events row when a decision is made; caller commits.
    """
    if conn.execute(
        "select 1 from alert_events where candidate_id=%s and tier=%s",
        (candidate_id, tier),
    ).fetchone():
        return None
    if tier == 2 and conn.execute(
        "select 1 from alert_events where candidate_id=%s and tier=1", (candidate_id,)
    ).fetchone():
        return None
    rows = conn.execute(_NEWLY_BROKEN, (candidate_id, tier)).fetchall()
    if not rows:
        return None
    newly = [(r["model_id"], float(r["break_rate"])) for r in rows]
    severity = len(newly) if tier == 1 else 0
    conn.execute(
        "insert into alert_events(candidate_id, tier, severity, newly_broken) "
        "values (%s,%s,%s,%s)",
        (candidate_id, tier, severity, Jsonb([[m, r] for m, r in newly])),
    )
    return AlertDecision(candidate_id, tier, severity, newly)
