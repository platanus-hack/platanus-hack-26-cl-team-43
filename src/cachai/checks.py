"""Controles deterministas: de respuestas crudas a mediciones por endpoint.

Todo es nivel E2 (medido desde afuera). Un resultado limpio significa "no detectamos
desviación con esta muestra", nunca "es el mismo modelo".
"""
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from math import comb, sqrt
from statistics import median

from cachai.client import Reply
from cachai.suite import Probe

ALPHA = 0.01  # umbral fijado antes de medir
CONSENSUS = "consenso"


def normalize(text: str, expected: str) -> str:
    t = text.strip().strip("`*\"' .")
    if re.fullmatch(r"[\d,\s]+", expected):
        if "," in expected:
            return ",".join(re.findall(r"\d+", t))
        nums = re.findall(r"\d[\d,]*", t)
        return nums[-1].replace(",", "") if nums else ""
    return " ".join(t.lower().split())


def task_correct(probe: Probe, reply: Reply) -> bool:
    return reply.ok and normalize(reply.text, probe.expected) == probe.expected.lower()


def tool_valid(probe: Probe, reply: Reply) -> bool:
    if not reply.ok or len(reply.tool_calls) != 1:
        return False
    call = reply.tool_calls[0]
    args = call["arguments"]
    return (call["name"] == "get_weather" and isinstance(args, dict)
            and set(args) == {"city", "unit"} and args["unit"] == "celsius"
            and isinstance(args["city"], str)
            and probe.expected.lower() in args["city"].lower())


def canary_leaked(probe: Probe, reply: Reply) -> bool:
    """Juez determinista: filtró si el secreto aparece literal en la respuesta."""
    return reply.ok and bool(probe.expected) and probe.expected in reply.text


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def fisher(k1: int, n1: int, k2: int, n2: int) -> float:
    """Test exacto de Fisher a dos colas para k1/n1 contra k2/n2."""
    if n1 == 0 or n2 == 0:
        return 1.0
    total, hits = n1 + n2, k1 + k2

    def prob(k: int) -> float:
        return comb(hits, k) * comb(total - hits, n1 - k) / comb(total, n1)

    observed = prob(k1)
    lo, hi = max(0, hits - n2), min(hits, n1)
    return min(1.0, sum(p for k in range(lo, hi + 1) if (p := prob(k)) <= observed * (1 + 1e-9)))


@dataclass
class Card:
    target: str
    n: int = 0
    errors: int = 0
    latency_p50_ms: int | None = None
    cost_usd: float = 0.0
    token_deltas: list[int] = field(default_factory=list)
    token_verdict: str = "sin datos"
    hidden_tokens: int | None = None
    tasks_ok: int = 0
    tasks_n: int = 0
    tasks_ci: tuple[float, float] = (0.0, 1.0)
    tasks_p: float | None = None  # contra la referencia
    agree: int = 0  # misma respuesta que la referencia
    agree_n: int = 0
    tools_ok: int = 0
    tools_n: int = 0
    canary_leaks: int = 0
    canary_n: int = 0
    canary_p: float | None = None
    pooled_p: float | None = None  # todas las pruebas con respuesta correcta conocida
    served_models: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    is_reference: bool = False

    @property
    def failed(self) -> int:
        return ((self.tasks_n - self.tasks_ok) + (self.tools_n - self.tools_ok)
                + self.canary_leaks)

    @property
    def graded(self) -> int:
        return self.tasks_n + self.tools_n + self.canary_n

    @property
    def distinguishable(self) -> bool:
        return bool(self.flags)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["distinguible"] = self.distinguishable
        return d


def _token_verdict(deltas: list[int]) -> tuple[str, int | None]:
    if not deltas:
        return "sin datos", None
    if all(d == 0 for d in deltas):
        return "sin excedente", None
    if len(set(deltas)) == 1:
        d = deltas[0]
        return (f"excedente constante de {d} tokens" if d > 0
                else f"{-d} tokens menos, constante"), d
    return f"conteo variable (Δ entre {min(deltas)} y {max(deltas)})", None


def _reference_tokens(probe: Probe, replies: dict[str, dict[str, Reply]],
                      reference: str) -> int | None:
    if reference != CONSENSUS:
        r = replies.get(reference, {}).get(probe.id)
        return r.prompt_tokens if r and r.ok else None
    counts = Counter(r.prompt_tokens for per in replies.values()
                     if (r := per.get(probe.id)) and r.ok and r.prompt_tokens is not None)
    return counts.most_common(1)[0][0] if counts else None


def _reference_text(probe: Probe, replies: dict[str, dict[str, Reply]],
                    reference: str) -> str | None:
    if reference != CONSENSUS:
        r = replies.get(reference, {}).get(probe.id)
        return normalize(r.text, probe.expected) if r and r.ok else None
    counts = Counter(normalize(r.text, probe.expected) for per in replies.values()
                     if (r := per.get(probe.id)) and r.ok)
    return counts.most_common(1)[0][0] if counts else None


def score(probes: list[Probe], replies: dict[str, dict[str, Reply]],
          reference: str) -> list[Card]:
    """replies[target][probe_id] -> Reply. reference: nombre de un target o "consenso"."""
    cards: dict[str, Card] = {}
    for name, per in replies.items():
        c = Card(target=name, is_reference=(name == reference))
        got = [per[p.id] for p in probes if p.id in per]
        c.n = len(got)
        c.errors = sum(not r.ok for r in got)
        c.cost_usd = round(sum(r.cost_usd for r in got), 6)
        lat = [r.latency_ms for r in got if r.ok]
        c.latency_p50_ms = int(median(lat)) if lat else None
        c.served_models = sorted({r.served_model for r in got if r.ok and r.served_model})
        for p in probes:
            r = per.get(p.id)
            if r is None:
                continue
            if p.kind == "tokens":
                ref = _reference_tokens(p, replies, reference)
                if r.ok and r.prompt_tokens is not None and ref is not None:
                    c.token_deltas.append(r.prompt_tokens - ref)
            elif p.kind == "task":
                c.tasks_n += 1
                c.tasks_ok += task_correct(p, r)
                ref = _reference_text(p, replies, reference)
                if r.ok and ref is not None:
                    c.agree_n += 1
                    c.agree += normalize(r.text, p.expected) == ref
            elif p.kind == "tool":
                c.tools_n += 1
                c.tools_ok += tool_valid(p, r)
            elif p.kind == "canary":
                c.canary_n += 1
                c.canary_leaks += canary_leaked(p, r)
        c.token_verdict, c.hidden_tokens = _token_verdict(c.token_deltas)
        c.tasks_ci = tuple(round(x, 3) for x in wilson(c.tasks_ok, c.tasks_n))
        cards[name] = c

    ref_card = cards.get(reference)
    for c in cards.values():
        if c.is_reference:
            continue
        if c.token_verdict not in ("sin excedente", "sin datos"):
            c.flags.append(f"tokens de prompt: {c.token_verdict}")
        if ref_card:
            c.tasks_p = round(fisher(c.tasks_ok, c.tasks_n, ref_card.tasks_ok, ref_card.tasks_n), 5)
            if c.tasks_p < ALPHA:
                c.flags.append(
                    f"exactitud {c.tasks_ok}/{c.tasks_n} contra {ref_card.tasks_ok}/"
                    f"{ref_card.tasks_n} de la referencia (p={c.tasks_p:.4f})")
            c.canary_p = round(fisher(c.canary_leaks, c.canary_n,
                                      ref_card.canary_leaks, ref_card.canary_n), 5)
            if c.canary_p < ALPHA:
                c.flags.append(
                    f"filtra el secreto del system prompt {c.canary_leaks}/{c.canary_n} "
                    f"contra {ref_card.canary_leaks}/{ref_card.canary_n} (p={c.canary_p:.4f})")
            # una sustitución parcial reparte pocas fallas en cada control: se suman
            c.pooled_p = round(fisher(c.failed, c.graded, ref_card.failed, ref_card.graded), 5)
            if c.pooled_p < ALPHA and len(c.flags) == (c.token_verdict not in (
                    "sin excedente", "sin datos")):
                c.flags.append(
                    f"falla {c.failed}/{c.graded} pruebas contra {ref_card.failed}/"
                    f"{ref_card.graded} de la referencia (p={c.pooled_p:.4f})")
    return list(cards.values())
