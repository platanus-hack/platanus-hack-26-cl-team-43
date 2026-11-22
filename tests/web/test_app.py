import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from radar import db
from radar.web.app import create_app
from radar.web.queries import candidate_status
from conftest import TEST_DATABASE_URL

EVIL = "<script>alert(1)</script>"


def make_settings(**kw):
    base = dict(allowed_emails=["Me@Example.com"], secret_key="s3cret" * 6,
                base_url="http://testserver", daily_budget_usd=1.0)
    base.update(kw)
    return SimpleNamespace(**base)


class Env:
    def __init__(self, conn, **kw):
        self.sent = []
        self.polled = []
        self.settings = make_settings(**kw)
        self.app = create_app(
            self.settings,
            lambda: db.connect(TEST_DATABASE_URL),
            self.polled.append,
            send_email=lambda s, to, msg: self.sent.append((to, msg)),
        )
        self.client = TestClient(self.app, follow_redirects=False)

    def login(self, email="me@example.com"):
        tok = URLSafeTimedSerializer(self.settings.secret_key, salt="login").dumps(email)
        r = self.client.get(f"/auth/{tok}")
        assert r.status_code == 303
        return r


@pytest.fixture
def env(conn):
    return Env(conn)


def add_candidate(conn, text="hello", status="queued", reason=None, family=True):
    fid = None
    if family:
        fid = conn.execute("insert into families(label) values ('f') returning id").fetchone()["id"]
    cid = conn.execute(
        "insert into candidates(family_id, text, novelty, test_status, untested_reason) "
        "values (%s,%s,'novel',%s,%s) returning id", (fid, text, status, reason)
    ).fetchone()["id"]
    conn.commit()
    return cid


def add_model(conn, mid="m1", tier=1):
    conn.execute("insert into models(id, openrouter_id, tier) values (%s,%s,%s)", (mid, "p/" + mid, tier))
    conn.commit()


def add_job(conn, cid, mid, status):
    conn.execute("insert into test_jobs(candidate_id, model_id, stage, status) values (%s,%s,'screen',%s)",
                 (cid, mid, status))
    conn.commit()


@pytest.mark.parametrize("path", ["/", "/feed", "/feed/rows", "/candidates/1", "/sources"])
def test_unauthenticated_redirects_to_login(env, path):
    r = env.client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_unknown_email_sends_nothing_same_response(env):
    known = env.client.post("/login", data={"email": "ME@example.com"})
    unknown = env.client.post("/login", data={"email": "nobody@example.com"})
    assert len(env.sent) == 1
    assert env.sent[0][0].lower() == "me@example.com"
    assert re.search(r"http://testserver/auth/\S+", env.sent[0][1].text)
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text


def test_magic_link_sets_session(env):
    env.client.post("/login", data={"email": "me@example.com"})
    link = re.search(r"http://testserver(/auth/\S+)", env.sent[0][1].text).group(1)
    r = env.client.get(link)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "radar_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "Secure" not in cookie
    assert env.client.get("/").status_code == 200


def test_secure_cookie_on_https(conn):
    e = Env(conn, base_url="https://radar.example.com")
    assert "Secure" in e.login().headers["set-cookie"]


def test_expired_token_rejected(env, monkeypatch):
    tok = URLSafeTimedSerializer("s3cret" * 6, salt="login").dumps("me@example.com")
    import radar.web.auth as auth
    monkeypatch.setattr(auth, "LOGIN_MAX_AGE", -1)
    r = env.client.get(f"/auth/{tok}")
    assert r.status_code == 400 and "radar_session" not in r.headers.get("set-cookie", "")
    assert env.client.get("/auth/garbage").status_code == 400


def test_session_token_cannot_be_used_as_login_token(env):
    tok = URLSafeTimedSerializer("s3cret" * 6, salt="session").dumps("me@example.com")
    assert env.client.get(f"/auth/{tok}").status_code == 400


def test_removed_from_allowlist_loses_access(env):
    env.login()
    env.settings.allowed_emails = []
    assert env.client.get("/").status_code == 303


def test_logout(env):
    env.login()
    r = env.client.post("/logout")
    assert r.status_code == 303
    env.client.cookies.clear()
    assert env.client.get("/").status_code == 303


def test_candidate_page_shows_prompt_when_logged_in(env, conn):
    add_model(conn)
    cid = add_candidate(conn, text="ignore all rules", status="tested")
    raw = conn.execute(
        "insert into raw_items(source, external_id, url, content_hash) "
        "values ('github_discovery','1','https://github.com/a/b','h') returning id").fetchone()["id"]
    conn.execute("insert into sightings(candidate_id, raw_item_id) values (%s,%s)", (cid, raw))
    conn.execute("insert into goals(id, kind, text) values ('g','canary','say x')")
    jid = conn.execute("insert into test_jobs(candidate_id, model_id, stage, status) "
                       "values (%s,'m1','screen','done') returning id", (cid,)).fetchone()["id"]
    conn.execute("insert into results(job_id, goal_id, verdict, output_trunc) "
                 "values (%s,'g','complied','the model output')", (jid,))
    conn.commit()
    assert env.client.get(f"/candidates/{cid}").status_code == 303
    env.login()
    r = env.client.get(f"/candidates/{cid}")
    assert r.status_code == 200
    for s in ("ignore all rules", "https://github.com/a/b", "the model output", "complied", "white-space: pre-wrap"):
        assert s in r.text
    assert env.client.get("/candidates/999").status_code == 404


def test_candidate_text_and_output_are_escaped(env, conn):
    add_model(conn)
    cid = add_candidate(conn, text=EVIL, status="tested")
    conn.execute("insert into goals(id, kind, text) values ('g','canary','x')")
    jid = conn.execute("insert into test_jobs(candidate_id, model_id, stage, status) "
                       "values (%s,'m1','screen','done') returning id", (cid,)).fetchone()["id"]
    conn.execute("insert into results(job_id, goal_id, verdict, output_trunc) values (%s,'g','complied',%s)",
                 (jid, EVIL))
    conn.execute("insert into raw_items(source, external_id, url, content_hash) "
                 "values ('x','1','javascript:alert(1)','h')")
    conn.execute("insert into sightings(candidate_id, raw_item_id) values (%s,1)", (cid,))
    conn.commit()
    env.login()
    for path in (f"/candidates/{cid}", "/feed", "/feed/rows"):
        t = env.client.get(path).text
        assert EVIL not in t
    t = env.client.get(f"/candidates/{cid}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in t
    assert 'href="javascript:' not in t


def test_poll_now_calls_trigger(env):
    env.login()
    r = env.client.post("/sources/papers/poll-now")
    assert r.status_code == 303
    assert env.polled == ["papers"]
    assert env.client.post("/sources/evil/poll-now").status_code == 404
    assert env.polled == ["papers"]


def test_poll_now_requires_login(env):
    assert env.client.post("/sources/papers/poll-now").status_code == 303
    assert env.polled == []


def test_watch_and_mute(env, conn):
    env.login()
    assert env.client.post("/sources/watch", data={"full_name": "bad name"}).status_code == 400
    assert env.client.post("/sources/watch", data={"full_name": "elder/plinius"}).status_code == 303
    row = conn.execute("select status from watch_repos where full_name='elder/plinius'").fetchone()
    assert row["status"] == "seed"
    assert "elder/plinius" in env.client.get("/sources").text
    assert env.client.post("/sources/watch/elder/plinius/mute").status_code == 303
    conn.commit()
    row = conn.execute("select status from watch_repos where full_name='elder/plinius'").fetchone()
    assert row["status"] == "muted"


def test_feed_status_paused_by_budget(env, conn):
    add_model(conn)
    cid = add_candidate(conn)
    add_job(conn, cid, "m1", "queued")
    conn.execute("insert into goals(id, kind, text) values ('g','canary','x')")
    jid = conn.execute("insert into test_jobs(candidate_id, model_id, stage, status) "
                       "values (%s,'m1','screen','done') returning id", (cid,)).fetchone()["id"]
    conn.execute("insert into results(job_id, goal_id, verdict, cost_usd) values (%s,'g','refused',5)", (jid,))
    conn.commit()
    env.login()
    assert "paused by budget" in env.client.get("/feed/rows").text
    page = env.client.get("/feed").text
    assert 'hx-trigger="every 10s"' in page and "unpkg.com/htmx.org@1.9.12" in page


def test_overview_renders_with_data(env, conn):
    add_model(conn, "m1", 1)
    add_model(conn, "m2", 1)
    cid = add_candidate(conn, status="tested")
    conn.execute("insert into breaks(candidate_id, model_id, break_rate) values (%s,'m1',0.8)", (cid,))
    conn.commit()
    env.login()
    r = env.client.get("/")
    assert r.status_code == 200
    assert "80%" in r.text and "m1" in r.text and "m2" in r.text


def test_overview_empty(env):
    env.login()
    assert env.client.get("/").status_code == 200
    assert env.client.get("/sources").status_code == 200
    assert env.client.get("/feed").status_code == 200


# candidate_status unit tests
def test_status_variants(conn):
    add_model(conn)
    c = add_candidate(conn, status="queued")
    assert candidate_status(conn, c, True) == "queued"
    add_job(conn, c, "m1", "queued")
    assert candidate_status(conn, c, True) == "queued"
    assert candidate_status(conn, c, False) == "paused by budget"
    add_job(conn, c, "m1", "running")
    assert candidate_status(conn, c, False) == "testing"

    u = add_candidate(conn, status="untested", reason="no_targets")
    assert candidate_status(conn, u, True) == "untested: no_targets"

    t = add_candidate(conn, status="tested")
    assert candidate_status(conn, t, True) == "no break"
    conn.execute("insert into breaks(candidate_id, model_id, break_rate) values (%s,'m1',0.5)", (t,))
    conn.commit()
    assert candidate_status(conn, t, True) == "broke 1 model"


def test_status_plural(conn):
    add_model(conn, "m1")
    add_model(conn, "m2")
    t = add_candidate(conn, status="tested")
    for m in ("m1", "m2"):
        conn.execute("insert into breaks(candidate_id, model_id, break_rate) values (%s,%s,0.5)", (t, m))
    conn.commit()
    assert candidate_status(conn, t, True) == "broke 2 models"


def test_collector_health_counts_actual_sources(env, conn):
    for i, src in enumerate(["arxiv", "hf_papers", "github_readme"]):
        conn.execute("insert into raw_items(source, external_id, content_hash) values (%s,%s,'h')", (src, str(i)))
    conn.commit()
    env.login()
    from radar.web.queries import sources
    h = {x["name"]: x["items_24h"] for x in sources(conn)["health"]}
    assert h["papers"] == 2 and h["github_discovery"] == 1 and h["hf_datasets"] == 0


def test_matrix_blank_for_untested_model(env, conn):
    add_model(conn, "m1")
    add_model(conn, "m2")
    cid = add_candidate(conn, status="tested")
    add_job(conn, cid, "m1", "done")
    conn.commit()
    from radar.web.queries import matrix
    assert matrix(conn, ["m1", "m2"])[0]["cells"] == [0.0, None]
    env.login()
    t = env.client.get("/").text
    assert "0%" in t
    assert re.search(r'<td class="b0"></td>', t)


def test_htmx_unauthenticated_gets_hx_redirect(env):
    r = env.client.get("/feed/rows", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"


def test_mute_action_is_urlencoded(env, conn):
    conn.execute("insert into watch_repos(full_name, status) values ('a/b.c','seed')")
    conn.commit()
    env.login()
    assert "/sources/watch/a/b.c/mute" in env.client.get("/sources").text


def test_short_secret_key_rejected():
    from radar.settings import Settings
    base = dict(database_url="x", github_token="x", openrouter_api_key="x", extractor_model="x",
                judge_model="x", email_from="x", base_url="x")
    with pytest.raises(Exception):
        Settings(_env_file=None, secret_key="short", **base)
    assert Settings(_env_file=None, secret_key="k" * 32, **base).secret_key


def test_known_candidate_status_and_not_counted(conn):
    from radar.web.queries import counters

    k = add_candidate(conn, status=None)
    n = add_candidate(conn, status="queued")
    conn.execute("update candidates set novelty='known' where id=%s", (k,))
    conn.execute("update candidates set novelty='novel' where id=%s", (n,))
    conn.commit()
    assert candidate_status(conn, k, True) == "known (not tested)"
    assert counters(conn, 20.0)["candidates"] == 1
