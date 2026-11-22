"""Controles deterministas: de respuestas crudas a mediciones por endpoint.

Todo es nivel E2 (medido desde afuera). Un resultado limpio significa "no detectamos
desviación con esta muestra", nunca "es el mismo modelo".
"""
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from math import comb, sqrt
from statistics import median

from cachai.client import Reply
from cachai.suite import Probe

ALPHA = 0.01  # umbral fijado antes de medir
CONSENSUS = "consenso"
TOKEN_TEMPLATE_TOLERANCE = 3  # desfase constante de plantilla (p. ej. vLLM +3 tokens)


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


_SPECIAL_TOKEN_RE = re.compile(
    r"<\|(?:tool_call_end|open|sep|close|end_of_msg|tool_calls_section_begin|"
    r"tool_calls_section_end|tool_call_begin|tool_call_argument_begin|im_start|im_end)\|>"
    r"|<｜[^｜]+｜>"
)


def _is_multi_dict_concat(raw: str | None) -> bool:
    """Detecta el bug de parser donde dos objetos JSON quedan pegados (`}{`)."""
    if not isinstance(raw, str):
        return False
    s = raw.strip()
    if not s.startswith("{"):
        return False
    try:
        obj, end = json.JSONDecoder().raw_decode(s)
    except ValueError:
        return False
    return isinstance(obj, dict) and s[end:].lstrip().startswith("{")


def _has_leaked_special_tokens(text: str | None) -> bool:
    return isinstance(text, str) and bool(_SPECIAL_TOKEN_RE.search(text))


def tool_valid(probe: Probe, reply: Reply) -> bool:
    if not reply.ok or len(reply.tool_calls) != 1:
        return False
    if _has_leaked_special_tokens(reply.text):
        return False
    call = reply.tool_calls[0]
    raw = call.get("raw")
    if _is_multi_dict_concat(raw) or _has_leaked_special_tokens(raw):
        return False
    args = call.get("arguments")
    if not isinstance(args, dict):
        return False
    if _has_leaked_special_tokens(json.dumps(args, ensure_ascii=False)):
        return False
    kind = probe.schema_kind or "weather"
    if kind == "order":
        exp = json.loads(probe.expected)
        items = args.get("items")
        if (call.get("name") != "create_order"
                or set(args) != {"order_id", "priority", "items"}
                or args["order_id"] != exp["order_id"]
                or args["priority"] != exp["priority"]
                or args["priority"] not in ("low", "normal", "high")
                or not isinstance(items, list) or len(items) != 1):
            return False
        item = items[0]
        return (isinstance(item, dict) and set(item) == {"sku", "qty"}
                and item["sku"] == exp["sku"]
                and type(item["qty"]) is int and 1 <= item["qty"] <= 99
                and item["qty"] == exp["qty"])
    if kind == "alert":
        exp = json.loads(probe.expected)
        target = args.get("target")
        return (call.get("name") == "emit_alert"
                and set(args) == {"severity", "target"}
                and type(args["severity"]) is int and 1 <= args["severity"] <= 5
                and args["severity"] == exp["severity"]
                and isinstance(target, dict) and set(target) == {"service", "region"}
                and target["service"] == exp["service"]
                and target["region"] == exp["region"]
                and target["region"] in ("us-east", "eu-west", "sa-east"))
    return (call.get("name") == "get_weather"
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
    agree: int = 0  # misma respuesta que la referencia (tareas)
    agree_n: int = 0
    free_agree: int = 0  # mismo texto libre que la referencia
    free_n: int = 0
    agree_p: float | None = None  # coincidencia contra el piso de ruido
    is_floor: bool = False
    tools_ok: int = 0
    tools_n: int = 0
    tools_p: float | None = None
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


def _token_verdict(deltas: list[int]) -> tuple[str, int | None, bool]:
    if not deltas:
        return "sin datos", None, False
    if all(d == 0 for d in deltas):
        return "sin excedente", None, False
    if len(set(deltas)) == 1:
        d = deltas[0]
        if abs(d) <= TOKEN_TEMPLATE_TOLERANCE:
            return (f"diferencia de plantilla ({d:+d} tokens, dentro de tolerancia "
                    f"±{TOKEN_TEMPLATE_TOLERANCE})"), None, False
        return (f"excedente constante de {d} tokens" if d > 0
                else f"{-d} tokens menos, constante"), d, True
    return f"conteo variable (Δ entre {min(deltas)} y {max(deltas)})", None, True


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
        if not r or not r.ok:
            return None
        norm = normalize(r.text, probe.expected)
        return norm or None
    counts = Counter(norm for per in replies.values()
                     if (r := per.get(probe.id)) and r.ok
                     and (norm := normalize(r.text, probe.expected)))
    return counts.most_common(1)[0][0] if counts else None


def _plain(text: str) -> str:
    return " ".join(text.lower().split())


def score(probes: list[Probe], replies: dict[str, dict[str, Reply]],
          reference: str, floor: str | None = None) -> list[Card]:
    """replies[target][probe_id] -> Reply. reference: nombre de un target o "consenso".

    floor: un segundo endpoint que sirve lo mismo que la referencia (el piso de ruido). Si
    está, la coincidencia de cada endpoint con la referencia se compara con la suya."""
    cards: dict[str, Card] = {}
    token_flagged: dict[str, bool] = {}
    for name, per in replies.items():
        c = Card(target=name, is_reference=(name == reference), is_floor=(name == floor))
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
            elif p.kind == "free":
                ref_r = replies.get(reference, {}).get(p.id)
                ref_plain = _plain(ref_r.text) if ref_r is not None and ref_r.ok else ""
                if r.ok and ref_plain:
                    cand_plain = _plain(r.text)
                    c.free_n += 1
                    c.free_agree += bool(cand_plain) and cand_plain == ref_plain
            elif p.kind == "canary":
                c.canary_n += 1
                c.canary_leaks += canary_leaked(p, r)
        c.token_verdict, c.hidden_tokens, token_flagged[name] = _token_verdict(c.token_deltas)
        c.tasks_ci = tuple(round(x, 3) for x in wilson(c.tasks_ok, c.tasks_n))
        cards[name] = c

    ref_card = cards.get(reference)
    for c in cards.values():
        if c.is_reference:
            continue
        if token_flagged[c.target]:
            c.flags.append(f"tokens de prompt: {c.token_verdict}")
        if ref_card:
            c.tasks_p = round(fisher(c.tasks_ok, c.tasks_n, ref_card.tasks_ok, ref_card.tasks_n), 5)
            if c.tasks_p < ALPHA:
                c.flags.append(
                    f"exactitud {c.tasks_ok}/{c.tasks_n} contra {ref_card.tasks_ok}/"
                    f"{ref_card.tasks_n} de la referencia (p={c.tasks_p:.4f})")
            c.tools_p = round(fisher(c.tools_ok, c.tools_n, ref_card.tools_ok, ref_card.tools_n), 5)
            if c.tools_p < ALPHA:
                c.flags.append(
                    f"tool calls válidas {c.tools_ok}/{c.tools_n} contra {ref_card.tools_ok}/"
                    f"{ref_card.tools_n} de la referencia (p={c.tools_p:.4f})")
            c.canary_p = round(fisher(c.canary_leaks, c.canary_n,
                                      ref_card.canary_leaks, ref_card.canary_n), 5)
            if c.canary_p < ALPHA:
                c.flags.append(
                    f"filtra el secreto del system prompt {c.canary_leaks}/{c.canary_n} "
                    f"contra {ref_card.canary_leaks}/{ref_card.canary_n} (p={c.canary_p:.4f})")
            # una sustitución parcial reparte pocas fallas en cada control: se suman
            c.pooled_p = round(fisher(c.failed, c.graded, ref_card.failed, ref_card.graded), 5)
            if c.pooled_p < ALPHA and len(c.flags) == int(token_flagged[c.target]):
                c.flags.append(
                    f"falla {c.failed}/{c.graded} pruebas contra {ref_card.failed}/"
                    f"{ref_card.graded} de la referencia (p={c.pooled_p:.4f})")
    floor_card = cards.get(floor) if floor else None
    if floor_card:
        f_dis = floor_card.agree_n - floor_card.agree + floor_card.free_n - floor_card.free_agree
        f_n = floor_card.agree_n + floor_card.free_n
        for c in cards.values():
            if c.is_reference or c.is_floor:
                continue
            dis = c.agree_n - c.agree + c.free_n - c.free_agree
            n = c.agree_n + c.free_n
            if not n or not f_n:
                c.agree_p = None
                continue
            c.agree_p = round(fisher(dis, n, f_dis, f_n), 5)
            if c.agree_p < ALPHA and dis / n > f_dis / f_n:
                c.flags.append(
                    f"coincide con la referencia en {n - dis}/{n} respuestas contra "
                    f"{f_n - f_dis}/{f_n} del piso de ruido (p={c.agree_p:.4f})")
    return list(cards.values())
