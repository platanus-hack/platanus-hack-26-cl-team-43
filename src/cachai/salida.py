"""Lo que el CLI escribe en la terminal: tablas alineadas, color y avance.

Sin dependencias. El color y la barra de avance solo salen en una terminal interactiva (y
sin NO_COLOR); en una tubería o en el log de CI el texto queda plano y con una fila por línea.
"""
import os
import re
import shutil
import sys
import textwrap

from cachai.checks import ALPHA

RESET, BOLD, DIM, RED, GREEN, YELLOW = "\033[0m", "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m"
_ANSI = re.compile(r"\033\[[0-9;]*m")


def interactive(stream=None) -> bool:
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


def colors(stream=None) -> bool:
    return interactive(stream) and "NO_COLOR" not in os.environ


def paint(text: str, *codes: str, on: bool | None = None) -> str:
    on = colors() if on is None else on
    return f"{''.join(codes)}{text}{RESET}" if on and codes else text


def width() -> int:
    """Columnas disponibles. Fuera de una terminal no hay límite: una fila por línea."""
    return shutil.get_terminal_size((100, 24)).columns if interactive() else 10_000


def _len(text: str) -> int:
    return len(_ANSI.sub("", text))


def _pad(text: str, size: int) -> str:
    return text + " " * max(0, size - _len(text))


def title(text: str) -> None:
    print("\n" + paint(text, BOLD))


def fields(rows: list[tuple[str, str]], indent: int = 2, dim: bool = True) -> None:
    """Pares etiqueta y valor, con los valores alineados. dim=False: la etiqueta es un comando."""
    size = max(len(k) for k, _ in rows)
    for k, v in rows:
        if dim:
            print(" " * indent + paint(_pad(k, size), DIM) + "  " + v)
        else:
            print(" " * indent + _pad(k, size) + "  " + paint(v, DIM))


def table(headers: list[str], rows: list[list[str]], indent: int = 2) -> None:
    """Columnas alineadas. Un encabezado con salto de línea ocupa dos renglones."""
    head = [h.split("\n") for h in headers]
    depth = max(len(h) for h in head)
    head = [[""] * (depth - len(h)) + h for h in head]
    sizes = [max([_len(x) for x in head[i]] + [_len(r[i]) for r in rows])
             for i in range(len(headers))]
    pad = " " * indent
    for line in range(depth):
        print((pad + "  ".join(paint(_pad(head[i][line], sizes[i]), DIM)
                               for i in range(len(headers)))).rstrip())
    for r in rows:
        print((pad + "  ".join(_pad(c, sizes[i]) for i, c in enumerate(r))).rstrip())


class Progress:
    """El avance de la auditoría: una barra en la terminal, puntos fuera de ella."""

    def __init__(self, total: int):
        self.total, self.done, self.errors = max(1, total), 0, 0
        self.live = interactive()

    def step(self, ok: bool) -> None:
        self.done += 1
        self.errors += 0 if ok else 1
        if not self.live:
            print("." if ok else "x", end="", flush=True)
            return
        size = 28
        filled = min(size, size * self.done // self.total)
        bar = paint("━" * filled, GREEN) + paint("━" * (size - filled), DIM)
        errors = paint(f"  ·  {self.errors} con error", YELLOW) if self.errors else ""
        print(f"\r  {bar}  {self.done}/{self.total} respuestas{errors}\033[K", end="", flush=True)

    def close(self) -> None:
        print()


def _verdict(e: dict) -> tuple[str, tuple[str, ...]]:
    if e["is_reference"]:
        return "referencia", (DIM,)
    if e.get("is_floor"):
        return "piso de ruido", (DIM,)
    if e["errors"] == e["n"]:
        return "sin respuesta", (YELLOW,)
    if e["distinguible"]:
        return "DISTINGUIBLE", (BOLD, RED)
    return "sin desviación detectada", (GREEN,)


def _tokens(e: dict) -> tuple[str, bool]:
    """El control de tokens en corto, y si es lo que marcó al endpoint."""
    deltas = e.get("token_deltas") or []
    if not deltas:
        return "sin datos", False
    if all(d == 0 for d in deltas):
        return "sin excedente", False
    if len(set(deltas)) > 1:
        return "conteo variable", True
    if e.get("hidden_tokens") is None:
        return f"{deltas[0]:+d} (plantilla)", False
    return f"{deltas[0]:+d} tokens", True


def _ratio(k: int, n: int, p: float | None = None, high_is_bad: bool = False) -> str:
    """k/n, en rojo si esa comparación con la referencia pasó el umbral."""
    if not n:
        return paint("sin datos", DIM)
    off = k > 0 if high_is_bad else k < n
    return paint(f"{k}/{n}", RED) if p is not None and p < ALPHA and off else f"{k}/{n}"


def _cells(e: dict) -> list[str]:
    tokens, flagged = _tokens(e)
    plain = e["is_reference"] or e.get("is_floor")
    p = (lambda key: None) if plain else e.get
    return [
        paint(tokens, RED) if flagged and not plain else tokens,
        _ratio(e["tasks_ok"], e["tasks_n"], p("tasks_p")),
        _ratio(e["agree"], e["agree_n"], p("agree_p")),
        _ratio(e["free_agree"], e["free_n"], p("agree_p")),
        _ratio(e["tools_ok"], e["tools_n"], p("tools_p")),
        _ratio(e["canary_leaks"], e["canary_n"], p("canary_p"), high_is_bad=True),
        paint(f"{e['errors']}/{e['n']}", YELLOW) if e["errors"] else f"{e['errors']}/{e['n']}",
    ]


LABELS = ["Tokens de\nprompt", "Tareas\ncorrectas", "Igual a la\nreferencia", "Texto libre\nigual",
          "Tool calls\nválidas", "Filtra el\nsecreto", "Errores"]


def results(f: dict, cols: int | None = None) -> None:
    """Lo medido en una ficha: una fila por endpoint, los motivos y un resumen."""
    cols = cols or width()
    eps = f["endpoints"]
    declared = any(e.get("cuantizacion_declarada") for e in eps)
    name = max(len(e["target"]) for e in eps)
    title("Medido desde afuera")
    table_width = 2 + name + 2 + 24 + (14 if declared else 0) + 2 + 13 + 6 * 13
    if cols >= table_width:
        headers = ["Endpoint", "Resultado"] + (["Cuantización\ndeclarada"] if declared else []) + LABELS
        rows = []
        for e in eps:
            text, style = _verdict(e)
            quant = [e.get("cuantizacion_declarada") or paint("sin declarar", DIM)] if declared else []
            rows.append([paint(e["target"], BOLD), paint(text, *style)] + quant + _cells(e))
        table(headers, rows)
    else:  # terminal angosta: un endpoint por bloque, en tres renglones
        short = ["prompt", "tareas", "igual a la referencia", "texto libre",
                 "tool calls", "filtra el secreto", "errores"]
        for e in eps:
            text, style = _verdict(e)
            quant = f"  (declara {e['cuantizacion_declarada']})" if e.get(
                "cuantizacion_declarada") else ""
            print(f"  {paint(_pad(e['target'], name), BOLD)}  {paint(text, *style)}"
                  + paint(quant, DIM))
            pairs = [f"{paint(label, DIM)} {cell}" for label, cell in zip(short, _cells(e))]
            print("      " + " · ".join(pairs[:3]))
            print("      " + " · ".join(pairs[3:]))
    flagged = [e for e in eps if e["flags"]]
    if flagged:
        title("Por qué se distinguen")
        for e in flagged:
            print(f"  {paint(e['target'], BOLD, RED)}")
            for reason in e["flags"]:
                print("    - " + "\n      ".join(textwrap.wrap(reason, max(40, cols - 6))))
    audited = [e for e in eps if not e["is_reference"] and not e.get("is_floor")]
    marked = sum(1 for e in audited if e["distinguible"])
    print()
    if marked:
        print("  " + paint(f"{marked} de {len(audited)} endpoints se distinguen de la referencia.",
                           BOLD, RED))
    else:
        print("  " + paint(f"Ninguno de los {len(audited)} endpoints se distingue de la referencia.",
                           BOLD, GREEN))
        print("  " + paint("Eso no es lo mismo que «es el mismo modelo».", DIM))


def markdown(text: str) -> None:
    """Un Markdown simple (títulos, tablas y listas) como texto de terminal."""
    lines, i = text.splitlines(), 0
    clean = lambda s: s.replace("`", "").replace("**", "")  # noqa: E731
    while i < len(lines):
        line = lines[i]
        if line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append([clean(c.strip()) for c in lines[i].strip().strip("|").split("|")])
                i += 1
            rows = [r for r in block[1:] if not all(set(c) <= set("-: ") for c in r)]
            table(block[0], rows)
            continue
        if line.startswith("#"):
            title(clean(line.lstrip("# ")))
        else:
            print(clean(line))
        i += 1
