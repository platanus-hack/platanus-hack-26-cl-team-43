"""cachai: el auditor de modelos servidos como token-as-a-service."""
import argparse
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
from dotenv import load_dotenv

from cachai import checks, endpoints, ficha, providers, salida, scan, sesion, suite
from cachai.client import Target


def _key(env: str, keys_url: str = "") -> str:
    key = os.environ.get(env, "") if env else ""
    if env and not key:
        hint = f" (la sacás en {keys_url})" if keys_url else ""
        sys.exit(f"{env} no está definida{hint}. Ponela en tu entorno o en un archivo .env")
    return key


def _endpoint(spec: str, default_model: str = "") -> Target:
    """PROVEEDOR[:MODELO] (atajo de providers.py) o NOMBRE=URL[,VARIABLE_DE_CLAVE[,MODELO]]."""
    if "=" not in spec:
        name, _, model = spec.partition(":")
        preset = providers.PRESETS.get(name)
        if preset is None:
            sys.exit(f"proveedor desconocido: {name!r}. Conocidos: {providers.names()}. "
                     "Para otro servidor: NOMBRE=URL[,VARIABLE_DE_CLAVE[,MODELO]]")
        model = model or default_model
        if not model:
            sys.exit(f"falta el modelo en {spec!r}: {name}:MODELO")
        return Target(name=spec if spec != name else name, model=model,
                      base_url=preset.base_url, api_key=_key(preset.key_env, preset.keys_url))
    name, rest = spec.split("=", 1)
    parts = rest.split(",")
    if not name or not parts[0]:
        sys.exit(f"--endpoint mal formado: {spec!r} (NOMBRE=URL[,VARIABLE_DE_CLAVE[,MODELO]])")
    return Target(name=name, base_url=parts[0],
                  api_key=_key(parts[1] if len(parts) > 1 else ""),
                  model=(parts[2] if len(parts) > 2 and parts[2] else default_model))


def _config_endpoint(e: dict) -> str:
    """Una entrada [[endpoint]] de cachai.toml como especificación de --endpoint."""
    if "provider" in e:
        return f"{e['provider']}:{e['model']}" if e.get("model") else e["provider"]
    if "url" not in e or "name" not in e:
        sys.exit(f"cada [[endpoint]] necesita provider, o name y url: {e}")
    return f"{e['name']}={e['url']},{e.get('key_env', '')},{e.get('model', '')}"


def _load_config(args) -> None:
    """Completa con cachai.toml lo que no vino por la línea de comandos."""
    path = Path(args.config) if args.config else Path("cachai.toml")
    if not path.exists():
        if args.config:
            sys.exit(f"no existe {path}")
        return
    import tomllib

    cfg = tomllib.loads(path.read_text())
    print(f"configuración: {path}")
    args.model = args.model or cfg.get("model")
    args.direct = [_config_endpoint(e) for e in cfg.get("endpoint", [])] + args.direct
    args.reference = args.reference or cfg.get("reference")
    if cfg.get("openrouter") is False:
        args.no_openrouter = True
    args.providers = cfg.get("openrouter_providers", args.providers)
    for k in ("tasks", "max_usd", "out"):
        if k in cfg and getattr(args, k) == SCAN_DEFAULTS[k]:
            setattr(args, k, cfg[k])


def _print_declared(model: str, d: dict) -> None:
    salida.title(f"{model}: lo que declaran los proveedores")
    salida.fields([
        ("endpoints", f"{d['endpoints']}, {d['sin_cuantizacion_declarada']} sin cuantización declarada"),
        ("precio de salida", f"USD {d['precio_salida_min']:.2f} a {d['precio_salida_max']:.2f} "
                             f"por millón de tokens ({d['precio_ratio']}×)"),
    ])


def cmd_endpoints(args) -> None:
    with httpx.Client(timeout=30) as http:
        eps = endpoints.list_endpoints(args.model, http)
    _print_declared(args.model, endpoints.declared_summary(eps))
    rows = []
    for t in sorted(endpoints.to_targets(args.model, eps, ""), key=lambda t: t.price_out or 0):
        rows.append([
            salida.paint(t.name, salida.BOLD),
            t.quantization or salida.paint("sin declarar", salida.DIM),
            f"{t.price_out:.2f}" if t.price_out is not None else "?",
            "autor del modelo" if t.first_party else ""])
    print()
    salida.table(["Endpoint", "Cuantización\ndeclarada", "USD por millón\nde salida", ""], rows)


def cmd_scan(args) -> None:
    _load_config(args)
    if not args.model and not args.direct:
        sys.exit("decí qué auditar: cachai scan MODELO, --endpoint, o un cachai.toml "
                 "(ver examples/). Para probar sin claves: cachai demo")
    args.cuenta = _require_account()
    targets: list[Target] = []
    declared = None
    use_openrouter = not args.no_openrouter and bool(args.model) and (
        not args.direct or os.environ.get("OPENROUTER_API_KEY"))
    with httpx.Client(timeout=args.timeout) as http:
        if use_openrouter:
            key = _key("OPENROUTER_API_KEY", providers.PRESETS["openrouter"].keys_url)
            eps = endpoints.list_endpoints(args.model, http)
            declared = endpoints.declared_summary(eps)
            _print_declared(args.model, declared)
            only = args.only.split(",") if args.only else None
            targets = endpoints.pick(endpoints.to_targets(args.model, eps, key),
                                     args.providers, only)
        targets += [_endpoint(d, args.model or "") for d in args.direct]
        if len(targets) < 2:
            sys.exit("hacen falta al menos dos endpoints para comparar")
        names = [t.name for t in targets]
        # sin first-party: la referencia es el primer --endpoint (o el consenso en OpenRouter)
        reference = args.reference or next(
            (t.name for t in targets if t.first_party),
            checks.CONSENSUS if use_openrouter else names[0])
        if reference != checks.CONSENSUS and reference not in names:
            sys.exit(f"la referencia {reference!r} no está entre: {', '.join(names)}")
        f = _audit(args.model or targets[0].model, targets, reference, http, args, declared)
    if args.strict and any(e.get("distinguible") for e in f["endpoints"]):
        sys.exit(2)


def _require_account() -> dict:
    """Auditar endpoints reales pide una cuenta: `cachai login`. La demo de juguete no."""
    cred = sesion.load()
    if cred is None:
        sys.exit(sesion.LOGIN_HINT)
    with httpx.Client(timeout=10) as http:
        me = sesion.check(cred, http)
    if me is None:
        print("aviso: no se pudo hablar con el servidor de cuentas; la ficha queda solo "
              "en este equipo", file=sys.stderr)
    elif me.get("email"):
        salida.fields([("cuenta", me["email"])])
    return cred


def _audit(model: str, targets: list[Target], reference: str, http: httpx.Client, args,
           declared: dict | None = None) -> dict:
    """Corre las pruebas, puntúa, escribe la ficha y muestra lo medido."""
    t0 = time.time()
    floor = None
    if reference != checks.CONSENSUS and not args.no_floor:
        # el piso de ruido: la misma referencia por segunda vez; lo que coincide consigo
        # misma es la vara para la coincidencia de los demás
        floor = f"{reference} (piso)"
        ref_t = next(t for t in targets if t.name == reference)
        targets = targets + [replace(ref_t, name=floor, first_party=False)]
    seed = None if args.public else (
        args.seed if args.seed is not None else secrets.randbelow(2**32))
    probes = suite.build(seed, n_tasks=args.tasks)
    salida.title(f"Auditoría de {model}")
    salida.fields([
        ("endpoints", f"{len(targets) - (1 if floor else 0)}, con {len(probes)} pruebas a cada uno"),
        ("referencia", reference + (" (se consulta dos veces: la segunda mide el ruido)"
                                    if floor else "")),
        ("juego", "público, de prompts fijos" if seed is None else f"ciego, semilla {seed}"),
    ])
    print()
    progress = salida.Progress(len(targets) * len(probes))
    replies, spend = scan.run(
        targets, probes, http=http, max_usd=args.max_usd,
        extra_tokens=args.extra_tokens, on_reply=lambda r: progress.step(r.ok))
    progress.close()
    cards = checks.score(probes, replies, reference, floor)
    f = ficha.build(model, targets, cards, replies, reference=reference,
                    suite="publico" if seed is None else "ciego", seed=seed,
                    declared=declared, spent_usd=spend.spent)
    md = ficha.to_markdown(f)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = ficha.slug(f)
    (out / f"{stem}.json").write_text(json.dumps(f, indent=2, ensure_ascii=False))
    (out / f"{stem}.md").write_text(md)
    (out / f"{stem}.evidencia.jsonl").write_text("\n".join(
        json.dumps(r.to_dict(), ensure_ascii=False, sort_keys=True)
        for per in replies.values() for r in per.values()) + "\n")
    salida.results(f)
    summary = [("ficha", str(out / (stem + ".md"))),
               ("gasto", f"USD {spend.spent:.4f}"),
               ("duración", f"{round(time.time() - t0)} s")]
    cred = getattr(args, "cuenta", None)
    if cred and not getattr(args, "no_subir", False):
        with httpx.Client(timeout=20) as web:
            panel = sesion.upload(cred, f, web, seconds=round(time.time() - t0))
        summary.append(("panel", panel or "no se pudo subir la ficha; quedó en este equipo"))
    print()
    salida.fields(summary)
    return f


def cmd_login(args) -> None:
    with httpx.Client(timeout=15) as http:
        cred = sesion.login(http, args.server, browser=not args.no_browser)
    print("\n" + salida.paint("Listo: este equipo quedó conectado.", salida.BOLD, salida.GREEN))
    salida.fields(([("cuenta", cred["email"])] if cred.get("email") else [])
                  + [("panel", f"{cred['servidor']}/panel")])


def cmd_logout(args) -> None:
    with httpx.Client(timeout=10) as http:
        had = sesion.logout(http)
    print("Sesión cerrada en este equipo." if had else "No había una sesión en este equipo.")


def cmd_cuenta(args) -> None:
    cred = sesion.load()
    if cred is None:
        sys.exit("No hay una sesión en este equipo. Corré `cachai login`")
    with httpx.Client(timeout=10) as http:
        me = sesion.check(cred, http)
    if me is None:
        sys.exit(f"no se pudo hablar con {cred['servidor']}")
    salida.fields([("cuenta", me.get("email") or me["id"]),
                   ("auditorías guardadas", str(me["auditorias"])),
                   ("gasto medido", f"USD {me['gasto_usd']:.4f}"),
                   ("panel", me["panel"])], indent=0)


def cmd_demo(args) -> None:
    """Un mundo en memoria: el modelo de juguete, un proveedor honesto y dos tramposos."""
    import warnings

    # starlette avisa que su TestClient pasará a httpx2; la demo no depende de eso
    warnings.filterwarnings("ignore", message=r".*starlette\.testclient.*")
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from fastapi.testclient import TestClient

    from cachai.experiment import Router
    from cachai.toy import create_toy
    from cachai.villain import HIDDEN_PROMPT, create_villain

    toy = TestClient(create_toy(), base_url="http://toy")
    up = "http://toy/v1"
    apps = {
        "honesto": create_villain(up, "", http=toy),
        "prompt-oculto": create_villain(up, "", http=toy, inject=HIDDEN_PROMPT),
        "modelo-chico": create_villain(up, "", http=toy, swap_model="toy-small"),
    }
    router = Router(apps)
    router.local["toy"] = toy
    targets = [Target("referencia", "toy-large", base_url=up)] + [
        Target(name, "toy-large", base_url=f"http://{name}/v1") for name in apps]
    print("Demo sin claves ni red. Un modelo de juguete servido por cuatro endpoints:\n")
    salida.fields([
        ("referencia", "el modelo tal cual, contra el que se compara"),
        ("honesto", "sirve lo mismo que la referencia"),
        ("prompt-oculto", "agrega un system prompt delante de cada pedido"),
        ("modelo-chico", "responde con un modelo más chico con el mismo nombre"),
    ])
    _audit("toy-large", targets, "referencia", router, args)
    salida.title("Siguiente paso: auditar tus endpoints")
    print("  uv tool install git+https://github.com/platanus-hack/platanus-hack-26-cl-team-43")
    salida.fields([
        ("cachai login", "una vez: conecta tu cuenta"),
        ("cachai scan z-ai/glm-5.3", "necesita OPENROUTER_API_KEY"),
    ], dim=False)
    print("\n  Más ejemplos: "
          "https://github.com/platanus-hack/platanus-hack-26-cl-team-43/tree/main/examples")


DEMO_TRAPS = {"cuantizado": "la versión cuantizada a 4 bits",
              "modelo-chico": "un modelo tres veces más chico",
              "prompt-oculto": "un system prompt oculto delante de cada pedido"}


def cmd_ronda(args) -> None:
    """La demo en vivo desde la terminal: armar una ronda, ver su estado o revelar al tramposo."""
    base = (args.server or sesion.server()).rstrip("/")
    with httpx.Client(timeout=20) as http:
        try:
            if args.revelar:
                r = http.post(f"{base}/api/revelar")
            elif args.trampa:
                r = http.post(f"{base}/api/ronda", json={"trampa": args.trampa})
            else:
                r = http.get(f"{base}/api/estado")
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            sys.exit(f"no se pudo hablar con {base}: {type(e).__name__}")
    if r.status_code != 200:
        sys.exit(f"{base} respondió {r.status_code}: {data.get('error') or data}")
    if args.revelar:
        print("\n" + salida.paint(f"El tramposo de la ronda {data['ronda']} era el proveedor "
                                  f"{data['tramposo']}.", salida.BOLD, salida.RED))
        salida.fields([("su trampa", data["trampa_label"]),
                       ("ahora", f"quedó armada la ronda {data['ronda_nueva']}, con otro "
                                 "tramposo elegido al azar")])
        return
    salida.title(f"Demo en vivo, ronda {data['ronda']}")
    salida.fields([
        ("modelo", "qwen2.5-1.5b-instruct, el nombre que anuncian los tres"),
        ("oficial", f"{base}/oficial/v1  (la referencia)"),
        ("A", f"{base}/a/v1"),
        ("B", f"{base}/b/v1"),
        ("trampa", data["trampas"][data["trampa"]]),
        ("quién", "A o B, elegido al azar. Se sabe con: cachai ronda --revelar"),
    ])
    print("\n  Para auditarlos:")
    print(f"  cachai scan qwen2.5-1.5b-instruct --no-openrouter -e oficial={base}/oficial/v1 "
          f"-e A={base}/a/v1 -e B={base}/b/v1")


def cmd_toy(args) -> None:
    import uvicorn

    from cachai.toy import create_toy

    uvicorn.run(create_toy(), host="127.0.0.1", port=args.port, log_level="warning")


def cmd_serve(args) -> None:
    import uvicorn

    os.environ.setdefault("CACHAI_OLLAMA_URL", args.ollama_url)
    os.environ["PORT"] = str(args.port)
    from cachai.serve import create_app

    print(f"demo en vivo en http://{args.host}:{args.port}  (Ollama: {args.ollama_url})")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


def cmd_villain(args) -> None:
    import uvicorn

    from cachai.villain import HIDDEN_PROMPT, create_villain

    key = os.environ.get(args.upstream_key_env, "") if args.upstream_key_env else ""
    if args.upstream_key_env and not key:
        sys.exit(f"{args.upstream_key_env} no está definida")
    if not (args.inject or args.swap_model):
        sys.exit("elegí al menos una trampa: --inject y/o --swap-model")
    app = create_villain(args.upstream, key, http=httpx.Client(timeout=120),
                         inject=HIDDEN_PROMPT if args.inject else None,
                         swap_model=args.swap_model, fraction=args.fraction,
                         evade=args.evade)
    print(f"proveedor tramposo en http://127.0.0.1:{args.port}/v1 → {args.upstream}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def cmd_experimento(args) -> None:
    from cachai import experiment

    if args.toy:
        up = experiment.toy_upstream()
        stem = f"juguete-t{args.tasks}"
    elif args.ollama:
        up = experiment.ollama_upstream(args.ollama_url)
        stem = f"ollama-qwen2.5-t{args.tasks}"
    else:
        if not (args.model and args.small_model and args.provider):
            sys.exit("sin --toy ni --ollama hacen falta --model, --small-model y --provider")
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            sys.exit("OPENROUTER_API_KEY no está definida")
        up = experiment.openrouter_upstream(args.model, args.small_model, args.provider, key)
        stem = f"{args.model}@{args.provider}-t{args.tasks}".replace("/", "_")
    res = experiment.run(up, reps=args.reps, n_tasks=args.tasks, seed0=args.seed,
                         max_usd=args.max_usd, timeout=args.timeout,
                         log=lambda m: print(m, file=sys.stderr))
    j, m = experiment.write(res, Path(args.out), stem)
    salida.markdown(m.read_text())
    print()
    salida.fields([("resultados", f"{j} y {m}")])


SCAN_DEFAULTS = {"tasks": 20, "max_usd": 2.0, "out": "audit/fichas"}


def _audit_options(p) -> None:
    p.add_argument("--public", action="store_true", help="usa el juego público de prompts fijos")
    p.add_argument("--seed", type=int, help="semilla del juego ciego (para volver a correrlo)")
    p.add_argument("--tasks", type=int, default=SCAN_DEFAULTS["tasks"])
    p.add_argument("--no-floor", action="store_true",
                   help="no consulta la referencia dos veces (sin control de coincidencia)")
    p.add_argument("--max-usd", type=float, default=SCAN_DEFAULTS["max_usd"],
                   help="tope de gasto de la auditoría en USD")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--extra-tokens", type=int, default=0,
                   help="tokens de salida de más por prueba, para modelos que razonan "
                        "siempre y gastan el límite antes de responder")
    p.add_argument("--out", default=SCAN_DEFAULTS["out"])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cachai", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("endpoints", help="lo que declara cada proveedor (sin clave)")
    p.add_argument("model")
    p.set_defaults(fn=cmd_endpoints)

    p = sub.add_parser(
        "scan", help="audita endpoints de un modelo y escribe la ficha",
        description="Audita endpoints de un modelo contra una referencia y escribe la ficha.",
        epilog="ejemplos:\n"
        "  cachai scan qwen/qwen-2.5-7b-instruct                 # los proveedores de OpenRouter\n"
        "  cachai scan -e deepseek:deepseek-chat -e together:deepseek-ai/DeepSeek-V3\n"
        "  cachai scan -e ollama:qwen2.5:7b -e mio=http://10.0.0.5:8000/v1,MI_KEY,qwen2.5-7b\n"
        "  cachai scan                                           # lee ./cachai.toml\n"
        f"proveedores conocidos: {providers.names()}",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("model", nargs="?", help="nombre del modelo en OpenRouter (opcional con -e)")
    p.add_argument("-e", "--endpoint", "--direct", dest="direct", action="append", default=[],
                   metavar="PROVEEDOR[:MODELO] | NOMBRE=URL[,VAR_CLAVE[,MODELO]]",
                   help="un endpoint compatible con OpenAI; se puede repetir. El primero es "
                   "la referencia si no hay --reference")
    p.add_argument("-c", "--config", help="archivo TOML (por defecto ./cachai.toml si existe)")
    p.add_argument("--providers", type=int, default=6, help="cuántos de OpenRouter (los más baratos)")
    p.add_argument("--only", help="tags de OpenRouter separados por coma")
    p.add_argument("--no-openrouter", action="store_true")
    p.add_argument("--reference", help="nombre del endpoint de referencia, o 'consenso'")
    p.add_argument("--strict", action="store_true",
                   help="sale con código 2 si algún endpoint es distinguible (para CI)")
    p.add_argument("--no-subir", action="store_true",
                   help="no manda la ficha al panel de tu cuenta (queda solo en este equipo)")
    _audit_options(p)
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("login", help="conecta este equipo con tu cuenta (se abre el navegador)")
    p.add_argument("--server", default=None, help="otro servidor de cuentas")
    p.add_argument("--no-browser", action="store_true", help="solo muestra el enlace")
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("logout", help="cierra la sesión de este equipo y revoca su clave")
    p.set_defaults(fn=cmd_logout)

    p = sub.add_parser("cuenta", help="quién sos, cuántas auditorías llevás y el enlace al panel")
    p.set_defaults(fn=cmd_cuenta)

    p = sub.add_parser("demo", help="prueba en 10 segundos, sin claves: auditor contra "
                       "un proveedor honesto y dos tramposos de juguete")
    _audit_options(p)
    p.set_defaults(fn=cmd_demo, out="audit/demo")

    p = sub.add_parser("ronda", help="la demo en vivo desde la terminal: arma una ronda con "
                       "una trampa, muestra los endpoints o revela al tramposo")
    p.add_argument("trampa", nargs="?", choices=list(DEMO_TRAPS),
                   help="arma una ronda nueva con esta trampa; sin esto, muestra la ronda actual")
    p.add_argument("--revelar", action="store_true",
                   help="dice quién hacía trampa en la ronda actual y arma otra")
    p.add_argument("--server", default=None, help="otra demo (por defecto, la pública)")
    p.set_defaults(fn=cmd_ronda)

    p = sub.add_parser("serve", help="la demo en vivo: tres proveedores (uno tramposo) sobre "
                       "Qwen2.5 en Ollama, y una página que los audita")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--ollama-url", default=os.environ.get("CACHAI_OLLAMA_URL",
                                                         "http://127.0.0.1:11434/v1"))
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("villain", help="proveedor tramposo propio, para la demo")
    p.add_argument("--upstream", required=True, help="URL base del proveedor real (…/v1)")
    p.add_argument("--upstream-key-env", default="")
    p.add_argument("--port", type=int, default=9102)
    p.add_argument("--inject", action="store_true", help="agrega un system prompt oculto")
    p.add_argument("--swap-model", help="modelo más barato al que desviar tráfico")
    p.add_argument("--fraction", type=float, default=1.0, help="fracción desviada")
    p.add_argument("--evade", action="store_true",
                   help="se porta bien con los prompts del juego público")
    p.set_defaults(fn=cmd_villain)

    p = sub.add_parser("experimento",
                       help="línea base: benchmark fijo contra juego ciego, con tramposos propios")
    p.add_argument("--toy", action="store_true", help="contra el modelo de juguete, sin claves")
    p.add_argument("--ollama", action="store_true",
                   help="contra Qwen2.5 servido por nosotros con Ollama (Q8, Q4, Q2 y 0.5B)")
    p.add_argument("--ollama-url", default="http://localhost:11434/v1")
    p.add_argument("--model", help="modelo honesto en OpenRouter (p. ej. z-ai/glm-5.3)")
    p.add_argument("--small-model", help="modelo barato al que desvían los tramposos")
    p.add_argument("--provider", help="tag del proveedor de referencia en OpenRouter")
    p.add_argument("--reps", type=int, default=20, help="repeticiones por celda")
    p.add_argument("--tasks", type=int, default=20)
    p.add_argument("--seed", type=int, default=1, help="semilla de la primera repetición ciega")
    p.add_argument("--max-usd", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--out", default="results/linea-base")
    p.set_defaults(fn=cmd_experimento)

    p = sub.add_parser("toy", help="modelo de juguete local, para probar sin claves")
    p.add_argument("--port", type=int, default=9101)
    p.set_defaults(fn=cmd_toy)

    args = parser.parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.WARNING)
    args.fn(args)


if __name__ == "__main__":
    main()
