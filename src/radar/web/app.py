"""Dashboard: FastAPI + Jinja2 (autoescaped) + HTMX.

Prompts and model outputs are attacker-controlled: they are only ever rendered
through autoescaped templates (never `|safe`) inside <pre>.
CSRF: no framework; the session cookie is SameSite=Lax, which is the mitigation.
"""
import logging
import re
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from radar.alerts.channels import send_email as default_send_email
from radar.alerts.render import AlertMessage
from radar.web import auth, queries

log = logging.getLogger(__name__)
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


def _safe_url(url) -> str | None:
    """Only http(s) links are rendered as hrefs."""
    if url and urlparse(url).scheme in ("http", "https"):
        return url
    return None


def _bucket(rate: float | None) -> str:
    if rate is None:
        return "b0"
    if rate >= 0.75:
        return "b4"
    if rate >= 0.5:
        return "b3"
    if rate >= 0.25:
        return "b2"
    if rate > 0:
        return "b1"
    return "b0"


def create_app(settings, conn_factory, trigger_poll, send_email=default_send_email) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["pct"] = lambda r: "" if r is None else f"{round(r * 100)}%"
    env.filters["urlpath"] = lambda v: quote(str(v), safe="")
    env.globals.update(safe_url=_safe_url, bucket=_bucket)
    secure = settings.base_url.startswith("https")

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx["user"] = getattr(request.state, "user", None)
        return HTMLResponse(env.get_template(name).render(**ctx), status_code=status_code)

    def get_conn():
        conn = conn_factory()
        try:
            yield conn
        finally:
            conn.close()

    def require_user(request: Request) -> str:
        email = auth.read_session(settings, request.cookies.get(auth.COOKIE))
        if not email or not auth.is_allowed(settings, email):
            if request.headers.get("HX-Request"):
                raise HTTPException(401, headers={"HX-Redirect": "/login"})
            raise HTTPException(303, headers={"Location": "/login"})
        request.state.user = email
        return email

    # ---- auth (public) ----
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html")

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, email: str = Form("")):
        if auth.is_allowed(settings, email):
            token = auth.make_login_token(settings, email)
            link = f"{settings.base_url.rstrip('/')}/auth/{token}"
            try:
                send_email(settings, email.strip(), AlertMessage(
                    "Jailbreak Radar login",
                    f"Sign in (valid 15 minutes):\n{link}"))
            except Exception:
                log.exception("failed to send login email")
        return render(request, "login_sent.html")

    @app.get("/auth/{token}")
    def auth_link(request: Request, token: str):
        email = auth.read_login_token(settings, token)
        if not email or not auth.is_allowed(settings, email):
            return render(request, "login.html", status_code=400,
                          error="That link is invalid or expired.")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(auth.COOKIE, auth.make_session(settings, email),
                        max_age=auth.SESSION_MAX_AGE, httponly=True,
                        samesite="lax", secure=secure)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    # ---- pages (session required) ----
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_user)])
    def overview(request: Request, conn=Depends(get_conn)):
        models = queries.tier1_models(conn)
        return render(
            request, "overview.html",
            counters=queries.counters(conn, settings.daily_budget_usd),
            models=models, matrix=queries.matrix(conn, models),
            robustness=queries.robustness(conn, models))

    @app.get("/feed", response_class=HTMLResponse, dependencies=[Depends(require_user)])
    def feed_page(request: Request, conn=Depends(get_conn)):
        return render(request, "feed.html",
                      rows=queries.feed(conn, settings.daily_budget_usd))

    @app.get("/feed/rows", response_class=HTMLResponse, dependencies=[Depends(require_user)])
    def feed_rows(request: Request, conn=Depends(get_conn)):
        return render(request, "feed_rows.html",
                      rows=queries.feed(conn, settings.daily_budget_usd))

    @app.get("/candidates/{cid}", response_class=HTMLResponse,
             dependencies=[Depends(require_user)])
    def candidate(request: Request, cid: int, conn=Depends(get_conn)):
        d = queries.candidate_detail(conn, cid, settings.daily_budget_usd)
        if d is None:
            raise HTTPException(404, "candidate not found")
        return render(request, "candidate.html", **d)

    @app.get("/sources", response_class=HTMLResponse, dependencies=[Depends(require_user)])
    def sources_page(request: Request, conn=Depends(get_conn)):
        return render(request, "sources.html", **queries.sources(conn))

    @app.post("/sources/watch", dependencies=[Depends(require_user)])
    def watch(full_name: str = Form(""), conn=Depends(get_conn)):
        full_name = full_name.strip()
        if not REPO_RE.match(full_name):
            raise HTTPException(400, "full_name must look like owner/repo")
        conn.execute(
            "insert into watch_repos(full_name, status) values (%s, 'seed')"
            " on conflict (full_name) do update set status='seed'", (full_name,))
        conn.commit()
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/watch/{owner}/{repo}/mute", dependencies=[Depends(require_user)])
    def mute(owner: str, repo: str, conn=Depends(get_conn)):
        conn.execute("update watch_repos set status='muted' where full_name=%s",
                     (f"{owner}/{repo}",))
        conn.commit()
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{name}/poll-now", dependencies=[Depends(require_user)])
    def poll_now(name: str):
        if name not in queries.COLLECTORS:
            raise HTTPException(404, "unknown collector")
        trigger_poll(name)
        return RedirectResponse("/sources", status_code=303)

    return app
