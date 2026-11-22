"""cachai: el auditor de modelos servidos como token-as-a-service."""
import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from radar.audit import checks, endpoints, ficha, scan, suite
from radar.audit.client import Target


def _direct(spec: str) -> Target:
    """NOMBRE=URL[,VARIABLE_DE_CLAVE[,MODELO]]"""
    name, _, rest = spec.partition("=")
    parts = rest.split(",")
    if not name or not parts[0]:
        sys.exit(f"--direct mal formado: {spec!r} (NOMBRE=URL[,VARIABLE_DE_CLAVE[,MODELO]])")
    key_env = parts[1] if len(parts) > 1 else ""
    key = os.environ.get(key_env, "") if key_env else ""
    if key_env and not key:
        sys.exit(f"{key_env} no está definida")
    return Target(name=name, base_url=parts[0], api_key=key,
                  model=parts[2] if len(parts) > 2 else "")


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
    directs = [_direct(s) for s in args.direct]
    targets: list[Target] = []
    declared = None
    with httpx.Client(timeout=args.timeout) as http:
        if not args.no_openrouter:
            key = os.environ.get("OPENROUTER_API_KEY")
            if not key:
                sys.exit("OPENROUTER_API_KEY no está definida (o usá --no-openrouter con --direct)")
            eps = endpoints.list_endpoints(args.model, http)
            declared = endpoints.declared_summary(eps)
            _print_declared(args.model, declared)
            only = args.only.split(",") if args.only else None
            targets = endpoints.pick(endpoints.to_targets(args.model, eps, key),
                                     args.providers, only)
        targets += [Target(name=d.name, base_url=d.base_url, api_key=d.api_key,
                           model=d.model or args.model) for d in directs]
        if len(targets) < 2:
            sys.exit("hacen falta al menos dos endpoints para comparar")
        names = [t.name for t in targets]
        reference = args.reference or next(
            (t.name for t in targets if t.first_party), checks.CONSENSUS)
        if reference != checks.CONSENSUS and reference not in names:
            sys.exit(f"la referencia {reference!r} no está entre: {', '.join(names)}")
        seed = None if args.public else (
            args.seed if args.seed is not None else secrets.randbelow(2**32))
        probes = suite.build(seed, n_tasks=args.tasks)
        print(f"{len(targets)} endpoints × {len(probes)} pruebas · referencia: {reference} · "
              + ("juego público" if seed is None else f"juego ciego, semilla {seed}"))
        replies, spend = scan.run(
            targets, probes, http=http, max_usd=args.max_usd,
            on_reply=lambda r: print("." if r.ok else "x", end="", flush=True))
        print()
    cards = checks.score(probes, replies, reference)
    f = ficha.build(args.model, targets, cards, replies, reference=reference,
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
    if args.publish:
        _publish(f)


def _publish(f: dict) -> None:
    from radar.audit.publish import publish

    token = os.environ.get("INTERNAL_API_TOKEN")
    if not token:
        sys.exit("INTERNAL_API_TOKEN no está definida (es la de cachai/.env)")
    api = os.environ.get("CACHAI_API_URL", "http://localhost:8001")
    with httpx.Client(timeout=30) as http:
        print(f"publicada en el blog: /posts/{publish(f, api_url=api, token=token, http=http)}")


def cmd_publish(args) -> None:
    _publish(json.loads(Path(args.ficha).read_text()))


def cmd_toy(args) -> None:
    import uvicorn

    from radar.audit.toy import create_toy

    uvicorn.run(create_toy(), host="127.0.0.1", port=args.port, log_level="warning")


def cmd_villain(args) -> None:
    import uvicorn

    from radar.audit.villain import HIDDEN_PROMPT, create_villain

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
    from radar.audit import experiment

    if args.toy:
        up = experiment.toy_upstream()
        stem = f"juguete-t{args.tasks}"
    else:
        if not (args.model and args.small_model and args.provider):
            sys.exit("sin --toy hacen falta --model, --small-model y --provider")
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cachai", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("endpoints", help="lo que declara cada proveedor (sin clave)")
    p.add_argument("model")
    p.set_defaults(fn=cmd_endpoints)

    p = sub.add_parser("scan", help="audita los endpoints de un modelo y escribe la ficha")
    p.add_argument("model")
    p.add_argument("--providers", type=int, default=6, help="cuántos de OpenRouter (los más baratos)")
    p.add_argument("--only", help="tags de OpenRouter separados por coma")
    p.add_argument("--direct", action="append", default=[],
                   metavar="NOMBRE=URL[,VAR_CLAVE[,MODELO]]",
                   help="API directa compatible con OpenAI; se puede repetir")
    p.add_argument("--no-openrouter", action="store_true")
    p.add_argument("--reference", help="nombre del endpoint de referencia, o 'consenso'")
    p.add_argument("--public", action="store_true", help="usa el juego público de prompts fijos")
    p.add_argument("--seed", type=int, help="semilla del juego ciego (para volver a correrlo)")
    p.add_argument("--tasks", type=int, default=20)
    p.add_argument("--max-usd", type=float, default=2.0)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--out", default="audit/fichas")
    p.add_argument("--publish", action="store_true", help="publica la ficha en el blog de CachAI")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("publish", help="publica una ficha ya escrita en el blog de CachAI")
    p.add_argument("ficha")
    p.set_defaults(fn=cmd_publish)

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
