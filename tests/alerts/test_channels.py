import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from alert_helpers import brk, candidate, family, setup
from radar.alerts.channels import ChannelNotConfigured, dispatch, send_email, send_whatsapp
from radar.alerts.render import AlertMessage
from radar.alerts.rules import AlertDecision

MSG = AlertMessage("Subj", "Body")


def S(**kw):
    base = dict(resend_api_key="rk", email_from="from@x.io", twilio_sid="sid",
                twilio_token="tok", twilio_whatsapp_from="+14155238886")
    base.update(kw)
    return SimpleNamespace(**base)


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_email_request():
    seen = {}

    def h(req):
        seen["req"] = req
        return httpx.Response(200, json={})
    send_email(S(), "to@x.io", MSG, client(h))
    r = seen["req"]
    assert str(r.url) == "https://api.resend.com/emails"
    assert r.headers["authorization"] == "Bearer rk"
    assert json.loads(r.content) == {"from": "from@x.io", "to": "to@x.io",
                                     "subject": "Subj", "text": "Body"}


def test_send_whatsapp_request_and_cap():
    seen = {}

    def h(req):
        seen["req"] = req
        return httpx.Response(201, json={})
    send_whatsapp(S(), "+56911111111", AlertMessage("Subj", "x" * 3000), client(h))
    r = seen["req"]
    assert str(r.url) == "https://api.twilio.com/2010-04-01/Accounts/sid/Messages.json"
    form = parse_qs(r.content.decode())
    assert form["From"] == ["whatsapp:+14155238886"] and form["To"] == ["whatsapp:+56911111111"]
    assert form["Body"][0].startswith("Subj\n\n") and len(form["Body"][0]) == 1500
    assert r.headers["authorization"].startswith("Basic ")


def test_missing_credentials_and_http_error():
    with pytest.raises(ChannelNotConfigured):
        send_email(S(resend_api_key=None), "a@b.c", MSG, client(lambda r: httpx.Response(200)))
    with pytest.raises(ChannelNotConfigured):
        send_whatsapp(S(twilio_token=None), "+1", MSG, client(lambda r: httpx.Response(200)))
    with pytest.raises(httpx.HTTPStatusError):
        send_email(S(), "a@b.c", MSG, client(lambda r: httpx.Response(500)))


def _subs(conn):
    for email, phone, ch, sev, active in [
        ("a@x.io", "+1", ["email", "whatsapp"], 1, True),
        ("b@x.io", None, ["email", "whatsapp"], 0, True),
        ("c@x.io", "+3", ["email"], 3, True),
        ("d@x.io", None, ["email"], 0, False),
    ]:
        conn.execute("insert into subscribers(email, phone, channels, min_severity, active) "
                     "values (%s,%s,%s,%s,%s)", (email, phone, ch, sev, active))
    conn.commit()


def test_dispatch_respects_min_severity(conn, monkeypatch):
    setup(conn)
    _subs(conn)
    cid = candidate(conn)
    sent = []
    monkeypatch.setattr("radar.alerts.channels.send_email", lambda s, to, m, http=None: sent.append(("e", to)))
    monkeypatch.setattr("radar.alerts.channels.send_whatsapp", lambda s, to, m, http=None: sent.append(("w", to)))
    n = dispatch(conn, S(), AlertDecision(cid, 1, 1, [("t1", 0.8)]), MSG)
    conn.commit()
    # a: email+whatsapp, b: email only (no phone); c needs severity 3; d inactive
    assert sorted(sent) == [("e", "a@x.io"), ("e", "b@x.io"), ("w", "+1")] and n == 3
    rows = conn.execute("select channel, status from alerts").fetchall()
    assert len(rows) == 3 and all(r["status"] == "sent" for r in rows)
    assert conn.execute("select count(*) n from alerts where sent_at is null").fetchone()["n"] == 0


def test_send_failure_recorded_not_raised(conn):
    setup(conn)
    _subs(conn)
    cid = candidate(conn)
    n = dispatch(conn, S(resend_api_key=None, twilio_sid=None),
                 AlertDecision(cid, 1, 1, [("t1", 0.8)]), MSG)
    conn.commit()
    assert n == 0
    rows = conn.execute("select status from alerts").fetchall()
    assert len(rows) == 3 and all(r["status"].startswith("failed: ") for r in rows)


def test_dispatch_digest_emails_only_email_subscribers(conn, monkeypatch):
    from radar.alerts.channels import dispatch_digest
    _subs(conn)
    sent = []
    monkeypatch.setattr("radar.alerts.channels.send_email", lambda s, to, m, http=None: sent.append(to))
    assert dispatch_digest(conn, S(), MSG) == 3
    assert sorted(sent) == ["a@x.io", "b@x.io", "c@x.io"]
