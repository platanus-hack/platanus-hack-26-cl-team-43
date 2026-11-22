from alert_helpers import brk, candidate, family, setup
from radar.alerts.rules import evaluate_alerts


def test_variant_rebreaking_same_model_does_not_alert(conn):
    setup(conn)
    f = family(conn)
    a = candidate(conn, f)
    b = candidate(conn, f, novelty="variant")
    brk(conn, a, "t1", minutes_ago=60)
    brk(conn, b, "t1")
    assert evaluate_alerts(conn, b, 1) is None


def test_variant_breaking_new_model_alerts(conn):
    setup(conn)
    f = family(conn)
    a = candidate(conn, f)
    b = candidate(conn, f, novelty="variant")
    brk(conn, a, "t1", minutes_ago=60)
    brk(conn, b, "t1", 0.6)
    brk(conn, b, "t1b", 0.9)
    d = evaluate_alerts(conn, b, 1)
    assert d.severity == 1 and d.tier == 1
    assert d.newly_broken == [("t1b", 0.9)]
    conn.commit()
    assert evaluate_alerts(conn, b, 1) is None  # idempotent


def test_first_break_in_family_alerts_with_all_models(conn):
    setup(conn)
    a = candidate(conn, family(conn))
    brk(conn, a, "t1")
    brk(conn, a, "t1b")
    assert evaluate_alerts(conn, a, 1).severity == 2


def test_tier2_only_break_is_severity_zero_open_model(conn):
    setup(conn)
    a = candidate(conn, family(conn))
    brk(conn, a, "t2", 0.7)
    assert evaluate_alerts(conn, a, 1) is None
    d = evaluate_alerts(conn, a, 2)
    assert d.severity == 0 and d.tier == 2 and d.newly_broken == [("t2", 0.7)]


def test_tier2_skipped_when_tier1_already_alerted(conn):
    setup(conn)
    a = candidate(conn, family(conn))
    brk(conn, a, "t1")
    brk(conn, a, "t2")
    assert evaluate_alerts(conn, a, 1) is not None
    assert evaluate_alerts(conn, a, 2) is None
