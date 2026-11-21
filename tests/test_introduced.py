"""Modo «vulnerabilidad introducida»: el equipo rojo contra el PR y contra la base."""

from cachai import pipeline
from cachai.gate import Evidence, decide
from cachai.holdout import Attack

INV = {"users": {"alice": {"Authorization": "Bearer a"}, "bob": {"Authorization": "Bearer b"}}}


def attack(path, user="bob", marker="FAC-A-001"):
    return Attack.from_dict({
        "id": "authz-" + path.strip("/").replace("/", "-") + f"-{user}",
        "family": "authz",
        "request": {"method": "GET", "path": path, "headers": INV["users"][user]},
        "succeeded_if": {"body_contains_any": [marker]},
    })


def test_only_attacks_missing_from_the_base_are_introduced():
    old, new = attack("/invoices/1"), attack("/invoices/1/summary")
    r = pipeline.compare_red_team([old, new], [old, attack("/invoices/2")])
    assert [a.id for a in r.introduced] == [new.id]
    assert [a.id for a in r.existing] == [old.id]


def test_introduced_evidence_names_request_user_and_marker():
    text = pipeline.describe_introduced(attack("/invoices/1/summary"), INV)
    assert text == "authz: GET /invoices/1/summary como bob recibe datos ajenos (FAC-A-001)"


def test_introduced_rejects_and_existing_is_only_a_note():
    v = decide(Evidence(True, introduced_attacks=["authz: GET /invoices/1/summary como bob recibe datos ajenos (FAC-A-001)"]))
    assert not v.approved and v.outcome == "rejected"
    assert v.reasons == ["vulnerabilidad introducida por el PR: authz: GET /invoices/1/summary como bob recibe datos ajenos (FAC-A-001)"]
    assert decide(Evidence(True)).approved


def test_existing_note_counts_and_lists_routes():
    note = pipeline.existing_note([attack(f"/invoices/{i}") for i in range(1, 4)])
    assert note.startswith("3 vulnerabilidades que ya estaban en la base siguen abiertas")


def test_introduced_authn_evidence_says_there_was_no_session():
    a = Attack.from_dict({
        "id": "authn-invoices-1-resumen-none", "family": "authn",
        "request": {"method": "GET", "path": "/invoices/1/resumen", "headers": {}},
        "succeeded_if": {"body_contains_any": ["FAC-A-001"]},
    })
    assert pipeline.describe_introduced(a, INV) == "authn: GET /invoices/1/resumen sin sesión válida recibe datos de un usuario (FAC-A-001)"
