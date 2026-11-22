"""La demo en vivo: tres proveedores del mismo modelo, uno tramposo, y Cachai auditándolos.

Corre sobre un Ollama propio con Qwen2.5 1.5B en Q8 (la referencia) y los modelos a los que
desvía el tramposo. Expone:

- /oficial/v1, /a/v1 y /b/v1: APIs compatibles con OpenAI. Las tres dicen servir el mismo
  modelo; uno de A o B, elegido al azar en cada ronda, hace trampa.
- /: la página de la demo. /api/ronda arma una ronda nueva; /api/auditar corre la auditoría
  rápida y manda el avance por SSE; al final revela quién era el tramposo.

Cualquiera puede auditar las mismas URLs con el CLI desde su máquina. El tramposo es nuestro
y solo se audita a sí mismo.

Con DATABASE_URL definida suma las cuentas (cachai.cuentas): sesión con Clerk, `cachai login`,
las fichas que sube cada quien y el panel en /panel. Sin esa variable, la demo corre igual.
"""
import asyncio
import json
import logging
import os
import random
import secrets
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from cachai import checks, ficha, scan, suite
from cachai.client import Target
from cachai.villain import HIDDEN_PROMPT

OLLAMA = os.environ.get("CACHAI_OLLAMA_URL", "http://127.0.0.1:11434/v1")
REF_MODEL = os.environ.get("CACHAI_REF_MODEL", "qwen2.5:1.5b-instruct-q8_0")
PUBLIC_MODEL = "qwen2.5-1.5b-instruct"  # el nombre que ven los clientes en los tres
TRAPS = {
    "cuantizado": {"swap": os.environ.get("CACHAI_Q4_MODEL", "qwen2.5:1.5b-instruct-q4_K_M"),
                   "label": "sirve la versión cuantizada a 4 bits con el mismo nombre"},
    "modelo-chico": {"swap": os.environ.get("CACHAI_SMALL_MODEL", "qwen2.5:0.5b-instruct-q8_0"),
                     "label": "sirve un modelo tres veces más chico con el mismo nombre"},
    "prompt-oculto": {"inject": HIDDEN_PROMPT,
                      "label": "agrega un system prompt oculto delante de cada pedido"},
}
PROVIDERS = ("oficial", "a", "b")
log = logging.getLogger("cachai.serve")


def _accounts(app: FastAPI, database_url: str, verify, public_url: str) -> bool:
    """Monta las cuentas. Si la base falla, la demo sigue viva sin ellas."""
    try:
        from cachai import cuentas, db

        frontend_api = os.environ.get("CLERK_FRONTEND_API", "https://clerk.jjato.com")
        db.upgrade(database_url)
        _, sessions = db.connect(database_url)
        verify = verify or cuentas.clerk_verifier(frontend_api, {public_url})
        app.include_router(cuentas.router(sessions, verify, public_url, frontend_api))
        return True
    except Exception:
        log.exception("las cuentas no arrancaron; la demo sigue sin ellas")
        return False


def quick_probes(seed: int) -> list:
    """Unas 24 pruebas, sin tool calls: las mismas de `cachai scan --rapida`."""
    return suite.quick(seed)


def _rename_stream(sse: str, asked: str) -> str:
    """Un stream de chat con el nombre de modelo que pidió el cliente en cada trozo."""
    out = []
    for line in sse.splitlines():
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
            try:
                chunk = json.loads(line[5:])
            except ValueError:
                chunk = None
            if isinstance(chunk, dict) and "model" in chunk:
                chunk["model"] = asked
                line = "data: " + json.dumps(chunk, ensure_ascii=False)
        out.append(line)
    return "\n".join(out) + "\n"


def create_app(self_url: str | None = None, database_url: str | None = None,
               verify=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    database_url = database_url or os.environ.get("DATABASE_URL")
    public_url = os.environ.get("CACHAI_PUBLIC_URL", "https://cachai.jjato.com").rstrip("/")
    if not (database_url and _accounts(app, database_url, verify, public_url)):
        @app.get("/api/config")
        def config():
            return {"cuentas": False}
    upstream = httpx.AsyncClient(timeout=300)
    state = {"cheater": random.choice("ab"), "trap": "cuantizado", "round": 1}
    audit_lock = threading.Lock()

    def port() -> int:
        return int(os.environ.get("PORT", "8000"))

    @app.post("/{provider}/v1/chat/completions")
    async def chat(provider: str, request: Request):
        if provider not in PROVIDERS:
            return JSONResponse({"error": {"message": "proveedor desconocido"}}, 404)
        body = await request.json()
        asked = body.get("model") or PUBLIC_MODEL
        body["model"] = REF_MODEL
        trap = TRAPS[state["trap"]] if provider == state["cheater"] else {}
        if trap.get("inject"):
            body["messages"] = [{"role": "system", "content": trap["inject"]},
                                *(body.get("messages") or [])]
        if trap.get("swap"):
            body["model"] = trap["swap"]
        for k in ("provider", "usage", "reasoning"):
            body.pop(k, None)
        try:
            resp = await upstream.post(f"{OLLAMA}/chat/completions", json=body)
            if body.get("stream") and resp.status_code == 200:
                return Response(_rename_stream(resp.text, asked), media_type="text/event-stream")
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            return JSONResponse({"error": {"message": f"upstream: {type(e).__name__}"}}, 502)
        if isinstance(data, dict) and "model" in data:
            data["model"] = asked  # el cliente ve el nombre que pidió
        return JSONResponse(data, resp.status_code)

    @app.get("/{provider}/v1/models")
    def models(provider: str):
        return {"object": "list", "data": [{"id": PUBLIC_MODEL, "object": "model"}]}

    @app.get("/api/estado")
    def estado():
        return {"ronda": state["round"], "trampa": state["trap"],
                "trampas": {k: v["label"] for k, v in TRAPS.items()},
                "auditando": audit_lock.locked()}

    @app.post("/api/ronda")
    async def ronda(request: Request):
        body = await request.json() if await request.body() else {}
        if audit_lock.locked():
            return JSONResponse({"error": "hay una auditoría en curso"}, 409)
        trap = body.get("trampa", state["trap"])
        if trap not in TRAPS:
            return JSONResponse({"error": "trampa desconocida"}, 400)
        state.update(cheater=random.choice("ab"), trap=trap, round=state["round"] + 1)
        return estado()

    @app.post("/api/revelar")
    def revelar():
        """Dice quién hacía trampa en esta ronda y arma otra: lo revelado ya no sirve.

        Es para la auditoría desde la terminal, donde no hay una página que lo muestre."""
        if audit_lock.locked():
            return JSONResponse({"error": "hay una auditoría en curso"}, 409)
        was = {"ronda": state["round"], "tramposo": state["cheater"].upper(),
               "trampa": state["trap"], "trampa_label": TRAPS[state["trap"]]["label"]}
        state.update(cheater=random.choice("ab"), round=state["round"] + 1)
        return {**was, "ronda_nueva": state["round"]}

    @app.get("/api/auditar")
    async def auditar():
        if not audit_lock.acquire(blocking=False):
            return JSONResponse({"error": "hay una auditoría en curso"}, 409)
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        base = self_url or f"http://127.0.0.1:{port()}"
        seed = secrets.randbelow(2**32)
        probes = quick_probes(seed)
        snapshot = dict(state)
        targets = [Target(name, PUBLIC_MODEL, base_url=f"{base}/{p}/v1")
                   for name, p in (("oficial", "oficial"), ("A", "a"), ("B", "b"),
                                   ("oficial (piso)", "oficial"))]

        def emit(kind: str, data: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

        def work() -> None:
            try:
                t0 = time.time()
                emit("inicio", {"semilla": seed, "pruebas": len(probes),
                                "endpoints": [t.name for t in targets]})
                with httpx.Client(timeout=300) as http:
                    replies, spend = scan.run(
                        targets, probes, http=http, max_usd=1.0, workers=len(targets),
                        on_reply=lambda r: emit("respuesta", {"endpoint": r.target, "ok": r.ok}))
                cards = checks.score(probes, replies, "oficial", "oficial (piso)")
                f = ficha.build(PUBLIC_MODEL, targets, cards, replies, reference="oficial",
                                suite="ciego", seed=seed, declared=None, spent_usd=0.0)
                emit("ficha", {"ficha": f, "markdown": ficha.to_markdown(f),
                               "segundos": round(time.time() - t0),
                               "tramposo": snapshot["cheater"].upper(),
                               "trampa": snapshot["trap"],
                               "trampa_label": TRAPS[snapshot["trap"]]["label"]})
            except Exception as e:  # la página tiene que enterarse
                emit("error", {"mensaje": f"{type(e).__name__}: {e}"})
            finally:
                audit_lock.release()
                emit("fin", {})

        threading.Thread(target=work, daemon=True).start()

        async def stream():
            while True:
                kind, data = await queue.get()
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if kind == "fin":
                    break

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/", response_class=HTMLResponse)
    def page():
        return (Path(__file__).parent / "demo.html").read_text()

    @app.get("/salud")
    def salud():
        return {"ok": True}

    return app
