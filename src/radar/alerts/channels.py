import logging

import httpx

from radar.alerts.render import AlertMessage
from radar.alerts.rules import AlertDecision

log = logging.getLogger(__name__)

WHATSAPP_MAX = 1500


class ChannelNotConfigured(RuntimeError):
    pass


def send_email(settings, to: str, msg: AlertMessage, http: httpx.Client | None = None) -> None:
    if not settings.resend_api_key:
        raise ChannelNotConfigured("resend_api_key missing")
    r = (http or httpx).post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json={"from": settings.email_from, "to": to, "subject": msg.subject, "text": msg.text},
        timeout=15,
    )
    r.raise_for_status()


def send_whatsapp(settings, to: str, msg: AlertMessage, http: httpx.Client | None = None) -> None:
    if not (settings.twilio_sid and settings.twilio_token and settings.twilio_whatsapp_from):
        raise ChannelNotConfigured("twilio credentials missing")
    r = (http or httpx).post(
        f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_sid}/Messages.json",
        auth=(settings.twilio_sid, settings.twilio_token),
        data={"From": f"whatsapp:{settings.twilio_whatsapp_from}",
              "To": f"whatsapp:{to}",
              "Body": (msg.subject + "\n\n" + msg.text)[:WHATSAPP_MAX]},
        timeout=15,
    )
    r.raise_for_status()


def _short_error(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:200]


def dispatch(conn, settings, d: AlertDecision, msg: AlertMessage) -> int:
    """Send to eligible subscribers, recording an alerts row each. Never raises; caller commits."""
    subs = conn.execute(
        "select email, phone, channels from subscribers where active and min_severity <= %s "
        "order by id", (d.severity,),
    ).fetchall()
    sent = 0
    for s in subs:
        for channel in s["channels"] or []:
            if channel == "email" and s["email"]:
                send, dest = send_email, s["email"]
            elif channel == "whatsapp" and s["phone"]:
                send, dest = send_whatsapp, s["phone"]
            else:
                continue
            try:
                send(settings, dest, msg)
                ok, status = True, "sent"
            except Exception as e:  # noqa: BLE001 - failures are recorded, not raised
                ok, status = False, f"failed: {_short_error(e)}"
            conn.execute(
                "insert into alerts(candidate_id, channel, sent_at, status) "
                "values (%s,%s,case when %s then now() end,%s)",
                (d.candidate_id, channel, ok, status),
            )
            sent += ok
    return sent


def dispatch_digest(conn, settings, msg: AlertMessage) -> int:
    """Email the digest to active subscribers; alerts.candidate_id is NOT NULL so nothing is recorded."""
    rows = conn.execute(
        "select id, email from subscribers where active and email is not null "
        "and 'email' = any(channels) order by id"
    ).fetchall()
    sent = 0
    for r in rows:
        try:
            send_email(settings, r["email"], msg)
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("digest to subscriber %s failed: %s", r["id"], _short_error(e))
    return sent
