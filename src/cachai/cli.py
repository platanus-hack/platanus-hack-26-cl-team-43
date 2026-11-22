"""cachai: el auditor de modelos servidos como token-as-a-service."""
import argparse
import json
import logging
import os
import secrets
import sys
from dataclasses import replace
from pathlib import Path

import httpx
from dotenv import load_dotenv

from cachai import checks, endpoints, ficha, providers, scan, suite
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
    print(f"{model}: {d['endpoints']} endpoints, {d['sin_cuantizacion_declarada']} sin "
          f"cuantización declarada, precio de salida USD {d['precio_salida_min']:.2f}"
          f"–{d['precio_salida_max']:.2f} por millón ({d['precio_ratio']}×)")


def cmd_endpoints(args) -> None:
    with httpx.Client(timeout=30) as http:
        eps = endpoints.list_endpoints(args.model, http)
    _print_declared(args.model, endpoints.declared_summary(eps))
    for t in sorted(endpoints.to_targets(args.model, eps, ""),
                    key=lambda t: t.price_out or 0):
        price = f"{t.price_out:8.2f}" if t.price_out is not None else "       ?"
        print(f"  {price}  {t.quantization or 'sin declarar':<13} {t.name}"
              f"{'  (first-party)' if t.first_party else ''}")


def cmd_scan(args) -> None:
    _load_config(args)
    if not args.model and not args.direct:
        sys.exit("decí qué auditar: cachai scan MODELO, --endpoint, o un cachai.toml "
                 "(ver examples/). Para probar sin claves: cachai demo")
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


def _audit(model: str, targets: list[Target], reference: str, http: httpx.Client, args,
           declared: dict | None = None) -> dict:
    """Corre las pruebas, puntúa, escribe la ficha y muestra lo medido."""
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
    print(f"{len(targets)} endpoints × {len(probes)} pruebas · referencia: {reference} · "
          + ("juego público" if seed is None else f"juego ciego, semilla {seed}"))
    replies, spend = scan.run(
        targets, probes, http=http, max_usd=args.max_usd,
        on_reply=lambda r: print("." if r.ok else "x", end="", flush=True))
    print()
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
    print(md[md.index("## Medido"):md.index("## Qué no afirma")])
    print(f"ficha: {out / (stem + '.md')}  ·  gasto: USD {spend.spent:.4f}")
    return f


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
    print("demo sin claves ni red: el modelo de juguete servido por una referencia, un "
          "proveedor honesto,\nuno que agrega un prompt oculto y uno que responde con un "
          "modelo más chico con el mismo nombre.\n")
    _audit("toy-large", targets, "referencia", router, args)
    print("\nSiguiente paso: auditar tus endpoints. Para tener el comando instalado:\n"
          "  uv tool install git+https://github.com/platanus-hack/platanus-hack-26-cl-team-43\n"
          "  cachai scan z-ai/glm-5.3            # necesita OPENROUTER_API_KEY\n"
          "Más ejemplos: https://github.com/platanus-hack/platanus-hack-26-cl-team-43/tree/main/examples")


def cmd_toy(args) -> None:
    import uvicorn

    from cachai.toy import create_toy

    uvicorn.run(create_toy(), host="127.0.0.1", port=args.port, log_level="warning")


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
    print(m.read_text())
    print(f"resultados en {j} y {m}")


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
    _audit_options(p)
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("demo", help="prueba en 10 segundos, sin claves: auditor contra "
                       "un proveedor honesto y dos tramposos de juguete")
    _audit_options(p)
    p.set_defaults(fn=cmd_demo, out="audit/demo")

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
