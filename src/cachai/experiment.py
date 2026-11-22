"""Experimento de línea base: ¿quién detecta a un proveedor que reconoce las pruebas?

Pregunta: un benchmark de prompts fijos (como los que se publican hoy) ¿detecta a un
proveedor que hace trampa solo cuando no lo están midiendo? ¿Y el juego ciego de Cachai?

Mundo: una referencia (el endpoint honesto) y cinco proveedores propios, todos proxies
hacia el mismo upstream. Uno es honesto (control de falsas alarmas) y cuatro hacen trampa:
prompt oculto, desvío del 30 % o del 100 % a un modelo más barato, y las dos juntas.

Se audita cada repetición con dos juegos:
- fijo: el juego público, siempre igual (la línea base);
- ciego: el de Cachai, con una semilla nueva en cada repetición.

Y contra dos tramposos:
- «reconoce el juego fijo»: memorizó los prompts públicos (lo que permite un benchmark fijo);
- «reconoce las plantillas»: además reconoce la forma de las pruebas ciegas. Mide el
  límite de Cachai si su generador queda expuesto.

Un proveedor cuenta como detectado si su ficha queda «distinguible de la referencia».
"""
import json
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from cachai import checks, scan, suite
from cachai.client import OPENROUTER, Target
from cachai.villain import HIDDEN_PROMPT, create_villain

CONDITIONS: list[tuple[str, dict]] = [
    ("honesto", {}),
    ("prompt-oculto", {"inject": True}),
    ("desvia-30", {"swap": True, "fraction": 0.3}),
    ("desvia-100", {"swap": True, "fraction": 1.0}),
    ("oculto+desvia-30", {"inject": True, "swap": True, "fraction": 0.3}),
]

# Lo que un tramposo que estudió el generador de Cachai podría reconocer.
_TEMPLATES = [
    re.compile(r"^What is \d+ [+*] \d+\? Reply with only the number\.$"),
    re.compile(r"^Sort these numbers in ascending order: "),
    re.compile(r"^Repeat exactly, with nothing else: "),
    re.compile(r"^Ticket \d{6}: what is the weather in "),
    re.compile(r"^Create order ORD-\d+ with priority "),
    re.compile(r"^Incident \d{6}: emit a severity \d+ alert for service "),
    re.compile(r"^(\w+ ){1,6}\d{6}$"),
    re.compile(re.escape(suite.CANARY_ASK)),
    re.compile(r"^Write one short sentence about .+, for reader \d{4}\.$"),
]


def knows_templates(messages: list[dict]) -> bool:
    text = str((messages[-1] if messages else {}).get("content") or "")
    return any(t.search(text) for t in _TEMPLATES)


FLOOR = "piso"

EVADERS = {
    "reconoce el juego fijo": None,
    "reconoce las plantillas": knows_templates,
}


class Router(httpx.Client):
    """Manda a las apps locales (los tramposos) por TestClient y lo demás a la red."""

    def __init__(self, apps: dict, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.local = {host: TestClient(app, base_url=f"http://{host}") for host, app in apps.items()}

    def post(self, url, **kw):
        host = httpx.URL(url).host
        if host in self.local:
            return self.local[host].post(url, **kw)
        return super().post(url, **kw)


@dataclass
class Upstream:
    """El modelo honesto, el barato al que se desvía y cómo llegar a ellos."""
    model: str
    small_model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    provider_tag: str | None = None  # fija el proveedor en OpenRouter
    http: httpx.Client | None = None  # para el juguete: TestClient del modelo local
    label: str = ""
    conditions: list = field(default_factory=lambda: list(CONDITIONS))
    games: tuple = ("fijo", "ciego")
    evaders: tuple = tuple(EVADERS)

    def reference(self) -> Target:
        return Target("referencia", self.model, base_url=self.base_url, api_key=self.api_key,
                      provider_tag=self.provider_tag)

    def extra(self) -> dict | None:
        if not self.provider_tag:
            return None
        return {"provider": {"only": [self.provider_tag], "allow_fallbacks": False},
                "usage": {"include": True}, "reasoning": {"exclude": True}}


def _world(up: Upstream, is_audit, rep: int, timeout: float, n_tasks: int) -> tuple[Router, list[Target]]:
    upstream_http = up.http or httpx.Client(timeout=timeout)
    # el piso de ruido: el mismo modelo que la referencia, por el mismo camino
    floor = Target(FLOOR, up.model, base_url=up.base_url, api_key=up.api_key,
                   provider_tag=up.provider_tag)
    apps, targets = {}, [up.reference(), floor]
    for i, (name, cond) in enumerate(up.conditions):
        swap = cond.get("swap")
        host = f"proveedor-{i}"
        apps[host] = create_villain(
            up.base_url, up.api_key, http=upstream_http,
            inject=HIDDEN_PROMPT if cond.get("inject") else None,
            swap_model=(up.small_model if swap is True else swap) or None,
            fraction=cond.get("fraction", 1.0), evade=True, is_audit=is_audit,
            upstream_extra=up.extra(), public_tasks=n_tasks, rng=random.Random(1000 * rep + i))
        targets.append(Target(name, up.model, base_url=f"http://{host}/v1"))
    router = Router(apps, timeout)
    if up.http is not None:  # el juguete también vive en memoria
        router.local[httpx.URL(up.base_url).host] = up.http
    return router, targets


def _which(card: checks.Card) -> list[str]:
    out = []
    for f in card.flags:
        out.append(f.split(" ")[0].rstrip(":"))
    return out


def run(up: Upstream, *, reps: int, n_tasks: int = 20, seed0: int = 1,
        max_usd: float = 5.0, timeout: float = 120, log=print) -> dict:
    spent = 0.0
    rows = []
    for evader in up.evaders:
        is_audit = EVADERS[evader]
        # el juego fijo solo se corre contra el tramposo simple: el de plantillas también
        # lo reconoce, y gastaría el doble para confirmar lo obvio
        suites = [g for g in ("fijo", "ciego") if g in up.games and (g == "ciego" or is_audit is None)]
        for rep in range(reps):
            for suite_name in suites:
                if spent >= max_usd:
                    log(f"tope de gasto alcanzado ({spent:.4f} USD): se corta")
                    break
                seed = None if suite_name == "fijo" else seed0 + rep
                probes = suite.build(seed, n_tasks)
                router, targets = _world(up, is_audit, rep, timeout, n_tasks)
                replies, sp = scan.run(targets, probes, http=router, workers=len(targets),
                                       max_usd=max_usd - spent)
                spent += sp.spent
                cards = {c.target: c for c in checks.score(probes, replies, "referencia", FLOOR)}
                ref, fl = cards["referencia"], cards[FLOOR]
                for name, _ in up.conditions:
                    c = cards[name]
                    rows.append({"tramposo": evader, "juego": suite_name, "rep": rep,
                                 "semilla": seed, "proveedor": name,
                                 "detectado": c.distinguishable, "controles": _which(c),
                                 "errores": c.errors, "ref_errores": ref.errors,
                                 "tareas": f"{c.tasks_ok}/{c.tasks_n}",
                                 "tokens": c.token_verdict,
                                 "coincide": f"{c.agree + c.free_agree}/{c.agree_n + c.free_n}",
                                 "piso_coincide": f"{fl.agree + fl.free_agree}/{fl.agree_n + fl.free_n}",
                                 "piso_marcado": fl.distinguishable})
                log(f"{evader} · {suite_name} · rep {rep + 1}/{reps} · gastado {spent:.4f} USD")
    return {"fecha": datetime.now(UTC).isoformat(timespec="seconds"),
            "upstream": up.label or up.model, "modelo": up.model, "modelo_barato": up.small_model,
            "proveedor_fijado": up.provider_tag, "repeticiones": reps, "tareas": n_tasks,
            "gasto_usd": round(spent, 4), "condiciones": [n for n, _ in up.conditions],
            "filas": rows, "resumen": summarize(rows)}


def summarize(rows: list[dict]) -> list[dict]:
    cells: dict[tuple, list[dict]] = {}
    for r in rows:
        cells.setdefault((r["tramposo"], r["juego"], r["proveedor"]), []).append(r)
    out = []
    for (evader, game, prov), rs in cells.items():
        k, n = sum(r["detectado"] for r in rs), len(rs)
        lo, hi = checks.wilson(k, n)
        ctrl: dict[str, int] = {}
        for r in rs:
            for c in r["controles"]:
                ctrl[c] = ctrl.get(c, 0) + 1
        out.append({"tramposo": evader, "juego": game, "proveedor": prov, "detectados": k,
                    "n": n, "ic95": [round(lo, 3), round(hi, 3)], "controles": ctrl,
                    "con_errores": sum(r["errores"] > 0 for r in rs)})
    return out


def _pct(k: int, n: int, ic: list[float]) -> str:
    return f"{k}/{n} ({round(100 * k / n)} %, IC95 {round(100 * ic[0])}-{round(100 * ic[1])} %)"


_COLUMNS = {("reconoce el juego fijo", "fijo"): "Benchmark fijo",
            ("reconoce el juego fijo", "ciego"): "Cachai ciego",
            ("reconoce las plantillas", "ciego"): "Cachai ciego, tramposo que conoce las plantillas"}


def markdown(res: dict) -> str:
    s = {(c["tramposo"], c["juego"], c["proveedor"]): c for c in res["resumen"]}
    keys = [k for k in _COLUMNS if any((*k, n) in s for n in res.get("condiciones") or [])] \
        or list(_COLUMNS)
    lines = [f"# Línea base: benchmark fijo contra juego ciego ({res['upstream']})", "",
             f"{res['fecha']} · modelo `{res['modelo']}`, desvío a `{res['modelo_barato']}`"
             + (f", proveedor fijado `{res['proveedor_fijado']}`" if res["proveedor_fijado"] else "")
             + f" · {res['repeticiones']} repeticiones por celda, {res['tareas']} tareas por juego"
             f" · gasto {res['gasto_usd']} USD", "",
             "Celda: proveedores marcados «distinguible de la referencia» / repeticiones.", "",
             "| Proveedor | " + " | ".join(_COLUMNS[k] for k in keys) + " |",
             "|---|" + "---|" * len(keys)]
    for name in res.get("condiciones") or [n for n, _ in CONDITIONS]:
        cols = []
        for key in keys:
            c = s.get((*key, name))
            cols.append(_pct(c["detectados"], c["n"], c["ic95"]) if c else "sin datos")
        label = name + (" (control: debería dar 0)" if name == "honesto" else "")
        lines.append(f"| {label} | " + " | ".join(cols) + " |")
    lines += ["", "Controles que dispararon (veces):", ""]
    for c in res["resumen"]:
        if c["controles"]:
            lines.append(f"- {c['proveedor']} · {c['juego']} · {c['tramposo']}: "
                         + ", ".join(f"{k} {v}" for k, v in sorted(c["controles"].items())))
    floor_rows = [r for r in res["filas"] if "piso_marcado" in r]
    if floor_rows:
        # por semilla: al juntar corridas hechas por partes, el número de repetición se repite
        seen = {(r["tramposo"], r["juego"], r["rep"] if r.get("semilla") is None else r["semilla"]): r
                for r in floor_rows}
        marked = sum(r["piso_marcado"] for r in seen.values())
        lines += ["", f"Piso de ruido (el mismo modelo que la referencia, en otra llamada): marcado "
                  f"{marked}/{len(seen)} veces; coincidencia por corrida: "
                  + ", ".join(sorted({r["piso_coincide"] for r in seen.values()})) + "."]
    errs = [c for c in res["resumen"] if c["con_errores"]]
    if errs:
        lines += ["", "Repeticiones con errores de red o del proveedor:", ""]
        lines += [f"- {c['proveedor']} · {c['juego']} · {c['tramposo']}: {c['con_errores']}/{c['n']}"
                  for c in errs]
    return "\n".join(lines) + "\n"


def write(res: dict, out_dir: Path, stem: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j, m = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    j.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    m.write_text(markdown(res))
    return j, m


OLLAMA = "http://localhost:11434/v1"
QWEN = {"ref": "qwen2.5:1.5b-instruct-q8_0", "q4": "qwen2.5:1.5b-instruct-q4_K_M",
        "q2": "qwen2.5:1.5b-instruct-q2_K", "small": "qwen2.5:0.5b-instruct-q8_0"}
OLLAMA_CONDITIONS: list[tuple[str, dict]] = [
    ("honesto", {}),
    ("prompt-oculto", {"inject": True}),
    ("cuantizado-q4", {"swap": QWEN["q4"]}),
    ("cuantizado-q2", {"swap": QWEN["q2"]}),
    ("sustituido-0.5b", {"swap": QWEN["small"]}),
    ("desvia-30-a-0.5b", {"swap": QWEN["small"], "fraction": 0.3}),
]


def ollama_upstream(base_url: str = OLLAMA) -> Upstream:
    """Modelos servidos por nosotros con Ollama: sabemos qué sirve cada proveedor."""
    return Upstream(QWEN["ref"], QWEN["small"], base_url, label="Qwen2.5 servido con Ollama",
                    conditions=list(OLLAMA_CONDITIONS), games=("ciego",),
                    evaders=("reconoce el juego fijo",))


def toy_upstream() -> Upstream:
    from cachai.toy import create_toy

    http = TestClient(create_toy(), base_url="http://toy")
    return Upstream("toy-large", "toy-small", "http://toy/v1", http=http, label="modelo de juguete")


def openrouter_upstream(model: str, small_model: str, provider_tag: str, key: str) -> Upstream:
    return Upstream(model, small_model, OPENROUTER, api_key=key, provider_tag=provider_tag,
                    label=f"{model} @ {provider_tag}")
