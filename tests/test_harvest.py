"""Cosecha de trazas: solo entra lo que confirmó el evaluador y `gate.decide`; nada reservado llega al parchador."""

import json
from pathlib import Path

import pytest

from cachai import bench, llm
from cachai.case import load_case
from cachai.holdout import load_attacks

FACTURAS = Path(__file__).resolve().parent.parent / "cases" / "facturas"
COMMIT = "c0ffee"


def make_case(root: Path, cid: str, extra_attack_path: str = "/v2/secret-route/1", visible_path: str = "/invoices/1") -> Path:
    """Caso mínimo: dos grupos de ataques (uno visible, otro reservado) y cuatro PR."""
    case = root / cid
    (case / "base" / "tests").mkdir(parents=True)
    (case / "base" / "app.py").write_text("print('base')\n")
    (case / "base" / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    inv = {"users": {"alice": {"Authorization": "Bearer tok-alice"}, "bob": {"Authorization": "Bearer tok-bob"}},
           "records": [{"id": "1", "owner": "alice", "markers": ["FAC-A-001"]}, {"id": "2", "owner": "bob", "markers": ["FAC-B-001"]}]}

    def attack(aid, group, path):
        return {"id": aid, "family": "authz", "group": group,
                "request": {"method": "GET", "path": path, "headers": {"Authorization": "Bearer tok-bob"}},
                "succeeded_if": {"body_contains_any": ["FAC-A-001"]},
                "control": {"request": {"method": "GET", "path": path, "headers": {"Authorization": "Bearer tok-alice"}},
                            "expect": {"body_contains_any": ["FAC-A-001"]}}}

    (case / "attacks.json").write_text(json.dumps([
        attack(f"{cid}-visible-1", "invoices", visible_path), attack(f"{cid}-reserved-1", "secret", extra_attack_path)]))
    prs = []
    for name, kind in [("good", "correct_patch"), ("half", "half_patch"), ("breaks", "breaking_patch"), ("bad", "half_patch")]:
        (case / "prs" / name).mkdir(parents=True)
        (case / "prs" / name / "app.py").write_text(f"print('{name}')\n")
        prs.append({"name": name, "kind": kind, "description": f"parche {name}"})
    (case / "case.json").write_text(json.dumps({
        "id": cid, "app": {"module": "app:app", "port": 8000, "health": "/health"}, "attacks": "attacks.json",
        "split": {"shown": 0.5, "seed": 0}, "inventory": inv, "prs": prs}))
    return case


def suite(ok=True):
    failures = [] if ok else [{"nodeid": "t::test_x", "file": "t.py", "line": 1, "message": "falla"}]
    return {"ok": ok, "complete": True, "timed_out": False, "passed": 3 if ok else 1, "failures": failures, "skipped": []}


TIMED_OUT = {"ok": False, "complete": False, "timed_out": True, "passed": 0, "failures": [], "skipped": []}


def row(case, pr, kind, approved, outcome, confirmed=(), blocked=(), base_ok=True, errors=(), base_suite=None):
    results = [{"id": i, "outcome": "confirmed", "http_status": 200, "error": None} for i in confirmed]
    results += [{"id": i, "outcome": "blocked", "http_status": 403, "error": None} for i in blocked]
    results += [{"id": i, "outcome": "error", "http_status": None, "error": "caída"} for i in errors]
    return {"case": case, "pr": pr, "kind": kind, "mode": "full", "approved": approved, "outcome": outcome,
            "result": {"approved": approved, "outcome": outcome, "reasons": ["motivo"], "split": None,
                       "evaluator": {"schema": 1, "status": "ok" if approved else "failed", "suites": {"base": base_suite or suite(base_ok)},
                                     "attacks": {"results": results, "complete": True}}}}


def rows_for(cid):
    v, r = f"{cid}-visible-1", f"{cid}-reserved-1"
    return [
        row(cid, "good", "correct_patch", True, "approved", blocked=[v, r]),
        row(cid, "half", "half_patch", False, "rejected", confirmed=[r], blocked=[v]),
        row(cid, "breaks", "breaking_patch", False, "rejected", blocked=[v, r], base_ok=False),
        row(cid, "bad", "half_patch", False, "rejected", confirmed=[v, r]),  # no tapa ni lo visible: no es un parche a medias
    ]


@pytest.fixture
def cases(tmp_path):
    return [load_case(make_case(tmp_path, cid)) for cid in ("c1", "c2", "c3", "c4", "c5")]


def run(cases, **kw):
    rows = [r for c in cases for r in rows_for(c.id)]
    return bench.harvest(cases, rows, COMMIT, **kw)


def records(out, prefix):
    return out[f"{prefix}.train"] + out[f"{prefix}.val"]


def test_only_confirmed_attacks_become_red_traces(cases):
    out = run(cases)
    red = records(out, "red")
    assert red, "debe haber trazas rojas"
    for rec in red:
        confirmed = {e["attack"] for e in rec["metadata"]["evidence"]}
        assert confirmed and all(a.endswith("visible-1") for a in confirmed)
        assert all(e["outcome"] == "confirmed" for e in rec["metadata"]["evidence"])
        # el ataque visible de `good` y de `half` fue bloqueado: solo entra si otro PR lo confirmó (`bad`)
    sources = {e["attack"]: e["pr"] for rec in red for e in rec["metadata"]["evidence"]}
    assert sources == {f"{c.id}-visible-1": "bad" for c in cases}
    assert "reserved-1" not in json.dumps(red)  # el rojo solo usa ataques visibles


def test_unconfirmed_attacks_are_excluded(cases):
    rows = [row(c.id, "good", "correct_patch", True, "approved", blocked=[f"{c.id}-visible-1", f"{c.id}-reserved-1"]) for c in cases]
    assert records(bench.harvest(cases, rows, COMMIT), "red") == []


def test_red_record_matches_the_llm_schema_and_prompt(cases):
    rec = records(run(cases), "red")[0]
    data = json.loads(rec["completion"])
    llm.validate_schema(data, llm.VARIANTS_SCHEMA)
    v = data["variants"][0]
    assert v["attacker"] == "bob" and v["target"] == "1" and v["family"] == "authz"
    assert "owner_path" in v and json.loads(rec["prompt"])["record_ids"] == ["1", "2"]


def test_red_prompt_carries_route_templates(cases):
    routes = {c.id: [("/invoices/{invoice_id}", ["invoice_id"], [])] for c in cases}
    rec = records(run(cases, routes=routes), "red")[0]
    assert json.loads(rec["prompt"])["get_routes"][0]["template"] == "/invoices/{invoice_id}"


def test_chosen_rejected_pairing_comes_from_real_verdicts(cases):
    out = run(cases)
    pairs = records(out, "blue_pref")
    assert len(pairs) == 2 * len(cases)  # half y breaks; `bad` no tapa lo visible
    for p in pairs:
        assert json.loads(p["chosen"])["files"][0]["content"] == "print('good')\n"
        rejected = json.loads(p["rejected"])["files"][0]["content"]
        assert rejected in ("print('half')\n", "print('breaks')\n")
        assert p["metadata"]["chosen"]["verdict"] == {"approved": True, "outcome": "approved"}
        assert p["metadata"]["rejected"]["verdict"]["approved"] is False
        kinds = {"half": ["reserved_attack"], "breaks": ["functional_test"]}[p["metadata"]["rejected"]["pr"]]
        assert p["metadata"]["rejected"]["failure_kinds"] == kinds
        llm.validate_schema(json.loads(p["chosen"]), llm.PATCH_SCHEMA)
        llm.validate_schema(json.loads(p["rejected"]), llm.PATCH_SCHEMA)


def test_sft_only_has_patches_approved_by_the_gate(cases):
    sft = records(run(cases), "blue_sft")
    assert len(sft) == len(cases) and {r["metadata"]["pr"] for r in sft} == {"good"}
    assert all(r["metadata"]["verdict"]["approved"] for r in sft)


def test_no_pair_without_an_approved_patch(cases):
    rows = [r for c in cases for r in rows_for(c.id) if r["pr"] != "good"]
    out = bench.harvest(cases, rows, COMMIT)
    assert records(out, "blue_pref") == [] and records(out, "blue_sft") == []


def test_incomplete_runs_are_never_rejected_examples(cases):
    rows = [r | {"outcome": "incomplete"} if r["pr"] == "half" else r for c in cases for r in rows_for(c.id)]
    kinds = {p["metadata"]["rejected"]["pr"] for p in records(bench.harvest(cases, rows, COMMIT), "blue_pref")}
    assert kinds == {"breaks"}


def test_blue_prompt_has_visible_attacks_and_never_reserved(cases):
    for rec in records(run(cases), "blue_sft") + records(run(cases), "blue_pref"):
        assert "/invoices/1" in rec["prompt"] and "/v2/secret-route/1" not in rec["prompt"]
        assert "reserved-1" not in json.dumps(rec)


def test_metadata_fields_present(cases):
    out = run(cases)
    for prefix in ("red", "blue_sft", "blue_pref"):
        assert records(out, prefix)
        for rec in records(out, prefix):
            m = rec["metadata"]
            assert m["commit"] == COMMIT and m["case"] in {c.id for c in cases}
    sft = records(out, "blue_sft")[0]["metadata"]
    assert sft["verdict"] == {"approved": True, "outcome": "approved"}
    assert sft["evaluator"]["status"] == "ok" and sft["evaluator"]["suites"]["base"] == {"ok": True, "passed": 3}
    assert sft["evaluator"]["attacks"] == {"confirmed": 0, "blocked": 2, "error": 0}
    ev = records(out, "red")[0]["metadata"]["evidence"][0]
    assert {"attack", "pr", "kind", "outcome", "http_status", "verdict"} <= set(ev)


def test_blue_metadata_never_names_reserved_attacks(cases):
    for rec in records(run(cases), "blue_pref") + records(run(cases), "blue_sft"):
        assert "reserved-1" not in json.dumps(rec["metadata"])


def test_guard_raises_when_a_reserved_attack_leaks(cases, tmp_path):
    c = cases[0]
    # el parche «filtra» la ruta reservada en su código
    (c.root / "prs" / "good" / "app.py").write_text("# ver /v2/secret-route/1\n")
    with pytest.raises(bench.ContaminationError) as e:
        run(cases)
    assert c.id in str(e.value) and "/v2/secret-route/1" not in str(e.value)


def test_guard_raises_on_reserved_id_in_prompt_or_target(cases):
    c = cases[1]
    (c.root / "prs" / "half" / "app.py").write_text(f"# {c.id}-reserved-1\n")
    with pytest.raises(bench.ContaminationError):
        run(cases)


def test_guard_ignores_strings_shared_with_visible_attacks(tmp_path):
    case = load_case(make_case(tmp_path, "same", extra_attack_path="/invoices/1"))
    out = bench.harvest([case], rows_for("same"), COMMIT)  # misma ruta en visible y reservado: no es una fuga
    assert records(out, "blue_sft")


def test_guard_on_real_case_does_not_raise_with_honest_fixtures():
    case = load_case(FACTURAS)
    ids = case.split().visible + case.split().reserved
    rows = [row(case.id, "parche-correcto", "correct_patch", True, "approved", blocked=list(ids)),
            row(case.id, "parche-a-medias", "half_patch", False, "rejected", confirmed=list(case.split().reserved), blocked=list(case.split().visible))]
    out = bench.harvest([case], rows, COMMIT)
    assert len(records(out, "blue_pref")) == 1
    reserved = {a.id for a in load_attacks(FACTURAS / "attacks.json") if a.id in case.split().reserved}
    assert not any(i in json.dumps(out["blue_pref.train"] + out["blue_sft.train"]) for i in reserved)


def test_split_is_deterministic_and_by_case(cases):
    a, b = run(cases, seed=7), run(cases, seed=7)
    assert a == b
    for prefix in ("red", "blue_sft", "blue_pref"):
        train = {r["metadata"]["case"] for r in a[f"{prefix}.train"]}
        val = {r["metadata"]["case"] for r in a[f"{prefix}.val"]}
        assert train and val and not train & val
    # la misma partición para rojo y azul
    assert {r["metadata"]["case"] for r in a["red.val"]} == {r["metadata"]["case"] for r in a["blue_sft.val"]}


def test_split_changes_with_seed_and_one_case_has_no_val(cases):
    vals = {frozenset(r["metadata"]["case"] for r in run(cases, seed=s)["red.val"]) for s in range(6)}
    assert len(vals) > 1
    assert run(cases[:1])["red.val"] == [] and run(cases[:1])["red.train"]


def test_write_harvest_outputs_valid_jsonl(cases, tmp_path):
    out = run(cases)
    paths = bench.write_harvest(out, tmp_path / "traces")
    assert {p.name for p in paths} == {f"{k}.jsonl" for k in out}
    for p in paths:
        lines = p.read_text().splitlines()
        assert [json.loads(x) for x in lines] == out[p.stem]


def test_harvest_without_inventory_or_attacks_skips_red_and_blue(tmp_path):
    root = make_case(tmp_path, "plain")
    data = json.loads((root / "case.json").read_text())
    del data["attacks"], data["inventory"]
    (root / "case.json").write_text(json.dumps(data))
    out = bench.harvest([load_case(root)], rows_for("plain"), COMMIT)
    assert all(v == [] for v in out.values())


# --- revisión: evidencia estricta, guardia, material sellado, parches expresables ---


def pairs_of(cases, rows):
    return records(bench.harvest(cases, rows, COMMIT), "blue_pref")


def test_visible_attack_with_error_or_missing_result_is_not_a_rejected_example(cases):
    c = cases[0]
    v, r = f"{c.id}-visible-1", f"{c.id}-reserved-1"
    good = row(c.id, "good", "correct_patch", True, "approved", blocked=[v, r])
    for half in (row(c.id, "half", "half_patch", False, "rejected", confirmed=[r], errors=[v]),
                 row(c.id, "half", "half_patch", False, "rejected", confirmed=[r])):  # sin resultado del visible
        assert pairs_of([c], [good, half]) == []


def test_error_on_a_reserved_attack_is_not_a_rejected_example(cases):
    c = cases[0]
    v, r = f"{c.id}-visible-1", f"{c.id}-reserved-1"
    rows = [row(c.id, "good", "correct_patch", True, "approved", blocked=[v, r]),
            row(c.id, "half", "half_patch", False, "rejected", blocked=[v], errors=[r], base_ok=False)]
    assert pairs_of([c], rows) == []


def test_timed_out_suite_is_not_a_functional_failure(cases):
    c = cases[0]
    v, r = f"{c.id}-visible-1", f"{c.id}-reserved-1"
    good = row(c.id, "good", "correct_patch", True, "approved", blocked=[v, r])
    assert pairs_of([c], [good, row(c.id, "breaks", "breaking_patch", False, "rejected", blocked=[v, r], base_suite=TIMED_OUT)]) == []
    # una suite incompleta invalida la evidencia entera, aunque haya un reservado confirmado
    rows = [good, row(c.id, "half", "half_patch", False, "rejected", blocked=[v], confirmed=[r], base_suite=TIMED_OUT)]
    assert pairs_of([c], rows) == []


def test_guard_decodes_urls_and_ignores_case(cases):
    c = cases[0]
    (c.root / "prs" / "good" / "app.py").write_text("# /V2/Secret%2DRoute/1\n")
    with pytest.raises(bench.ContaminationError):
        run(cases)


def test_guard_matches_json_bodies_in_any_formatting(tmp_path):
    root = make_case(tmp_path, "j")
    attacks = json.loads((root / "attacks.json").read_text())
    attacks[1]["request"] = {"method": "POST", "path": "/things", "json": {"owner": "bob", "item": "zzz"}, "headers": attacks[1]["request"]["headers"]}
    (root / "attacks.json").write_text(json.dumps(attacks))
    case = load_case(root)
    (case.root / "prs" / "good" / "app.py").write_text('X = {"item":"zzz", "owner":"bob"}\n')
    with pytest.raises(bench.ContaminationError):
        bench.harvest([case], rows_for("j"), COMMIT)


def test_guard_distinguishes_method_on_a_shared_path(tmp_path):
    root = make_case(tmp_path, "m", extra_attack_path="/invoices/1")
    attacks = json.loads((root / "attacks.json").read_text())
    attacks[1]["request"]["method"] = "DELETE"
    attacks[1]["control"]["request"]["method"] = "DELETE"
    (root / "attacks.json").write_text(json.dumps(attacks))
    case = load_case(root)
    assert bench.harvest([case], rows_for("m"), COMMIT)["blue_sft.train"]  # solo comparte la ruta
    (case.root / "prs" / "good" / "app.py").write_text("# DELETE /invoices/1\n")
    with pytest.raises(bench.ContaminationError):
        bench.harvest([case], rows_for("m"), COMMIT)


def test_guard_keeps_visible_per_case(tmp_path):
    a = load_case(make_case(tmp_path, "a", visible_path="/shared/1"))
    b = load_case(make_case(tmp_path, "b", extra_attack_path="/shared/1"))
    (b.root / "prs" / "good" / "app.py").write_text("# /shared/1\n")
    rows = rows_for("a") + rows_for("b")
    with pytest.raises(bench.ContaminationError) as e:
        bench.harvest([a, b], rows, COMMIT)
    assert "b" in str(e.value)


def test_guard_also_scans_red_records(cases):
    c = cases[0]
    red = records(run(cases), "red")[0]
    bench.check_clean([red], cases)
    leaky = red | {"completion": red["completion"].replace("/invoices/1", "/v2/secret-route/1")}
    with pytest.raises(bench.ContaminationError):
        bench.check_clean([leaky], cases)


def test_red_never_uses_reserved_attacks(cases):
    rows = [row(c.id, "half", "half_patch", False, "rejected", confirmed=[f"{c.id}-reserved-1"]) for c in cases]
    assert records(bench.harvest(cases, rows, COMMIT), "red") == []


def fake_check(calls):
    """Un `pipeline.check` sin Docker: ataca el plan completo y confirma todo solo en la rama base y en PR `vulnerable`."""
    def check(repo, base, pr, mode, target=None, holdout_tests=None, budget=0, plan=None):
        calls.append((pr, mode, plan))
        open_flaw = pr in ("main", "vuln")
        results = [{"id": a.id, "outcome": "confirmed" if open_flaw else "blocked", "http_status": 200, "error": None}
                   for a in (plan.attacks if plan else ())]
        ev = {"schema": 1, "status": "failed" if open_flaw else "ok", "suites": {"base": suite(True)},
              "attacks": {"results": results, "complete": True} if plan else None}
        return bench.pipeline.CheckResult(mode, "b", pr, not open_flaw, evaluator=ev, outcome="rejected" if open_flaw else "approved")
    return check


def test_red_evidence_comes_from_rows_shaped_like_run_produces(cases, monkeypatch):
    c = cases[0]
    (c.root / "prs" / "vuln").mkdir()
    (c.root / "prs" / "vuln" / "app.py").write_text("print('vuln')\n")
    data = json.loads((c.root / "case.json").read_text())
    data["prs"].append({"name": "vuln", "kind": "vulnerable", "description": "introduce una falla"})
    (c.root / "case.json").write_text(json.dumps(data))
    c = load_case(c.root)
    calls = []
    monkeypatch.setattr(bench.sandbox, "build_evaluator_image", lambda: None)
    monkeypatch.setattr(bench.pipeline, "check", fake_check(calls))
    # La corrida normal no cambia: ni fila de la base ni ataques contra el PR vulnerable.
    normal = bench.run([c], modes=("full",), jobs=1)
    assert "main" not in {r["pr"] for r in normal} and dict((p, plan) for p, _, plan in calls)["vuln"] is None
    calls.clear()
    rows = bench.run([c], modes=("full",), jobs=1, harvest=True)
    assert {r["pr"] for r in rows} == {p.name for p in c.prs} | {"main"}
    assert dict((p, plan is not None) for p, _, plan in calls)["main"] and dict((p, plan is not None) for p, _, plan in calls)["vuln"]
    red = records(bench.harvest([c], rows, COMMIT), "red")
    assert [e["attack"] for e in red[0]["metadata"]["evidence"]] == [f"{c.id}-visible-1"]  # solo el visible
    assert {e["pr"] for e in red[0]["metadata"]["evidence"]} == {"main"}  # la base, primera en orden


def test_red_ignores_confirmations_from_prs_that_are_not_vulnerable_or_half(cases):
    c = cases[0]
    v = f"{c.id}-visible-1"
    out = bench.harvest([c], [row(c.id, "x", "breaking_patch", False, "rejected", confirmed=[v])], COMMIT)
    assert records(out, "red") == []


def test_sealed_material_is_refused(cases):
    (cases[0].base_dir / ".cachai.json").write_text(json.dumps({"hidden": {"package": "r.sealed", "key_env": "CACHAI_KEY"}}))
    with pytest.raises(ValueError, match="reservado sellado"):
        run(cases)


def patch_is_dropped(cases, capsys, prepare):
    prepare(cases[0].root / "prs" / "half")
    capsys.readouterr()
    out = run(cases)
    assert {p["metadata"]["rejected"]["pr"] for p in records(out, "blue_pref") if p["metadata"]["case"] == cases[0].id} == {"breaks"}
    assert "half" in capsys.readouterr().err


def test_patch_with_new_file_is_dropped_with_a_note(cases, capsys):
    patch_is_dropped(cases, capsys, lambda d: (d / "nuevo.py").write_text("x = 1\n"))


def test_patch_touching_tests_is_dropped(cases, capsys):
    patch_is_dropped(cases, capsys, lambda d: ((d / "tests").mkdir(), (d / "tests" / "test_x.py").write_text("def test_x(): pass\n")))


def test_patch_touching_conftest_is_dropped(cases, capsys):
    patch_is_dropped(cases, capsys, lambda d: (d / "conftest.py").write_text("x = 1\n"))


def test_patch_with_non_python_file_is_dropped(cases, capsys):
    patch_is_dropped(cases, capsys, lambda d: (d / "requirements.txt").write_text("x\n"))


def test_patch_that_deletes_files_is_dropped(cases, capsys):
    path = cases[0].root / "case.json"
    data = json.loads(path.read_text())
    next(p for p in data["prs"] if p["name"] == "half")["delete"] = ["app.py"]
    path.write_text(json.dumps(data))
    patch_is_dropped([load_case(cases[0].root)] + cases[1:], capsys, lambda d: None)


def test_identical_patches_do_not_duplicate_pairs(cases):
    c = cases[0]
    (c.root / "prs" / "good2").mkdir()
    (c.root / "prs" / "good2" / "app.py").write_text("print('good')\n")
    data = json.loads((c.root / "case.json").read_text())
    data["prs"].append({"name": "good2", "kind": "correct_patch", "description": "parche good"})
    (c.root / "case.json").write_text(json.dumps(data))
    c = load_case(c.root)
    v, r = f"{c.id}-visible-1", f"{c.id}-reserved-1"
    rows = rows_for(c.id) + [row(c.id, "good2", "correct_patch", True, "approved", blocked=[v, r])]
    out = bench.harvest([c], rows, COMMIT)
    assert len(out["blue_sft.train"]) == 1 and len(out["blue_pref.train"]) == 2


def test_skipped_red_attacks_are_counted_on_stderr(cases, capsys):
    c = cases[0]
    attacks = json.loads((c.root / "attacks.json").read_text())
    del attacks[0]["control"]  # sin control no hay `owner_path`: no cabe en VARIANTS_SCHEMA
    (c.root / "attacks.json").write_text(json.dumps(attacks))
    capsys.readouterr()
    bench.harvest([load_case(c.root)], [row(c.id, "bad", "half_patch", False, "rejected", confirmed=[f"{c.id}-visible-1"])], COMMIT)
    assert "1 ataque" in capsys.readouterr().err


def test_summary_is_neutral_and_never_a_pr_description(cases):
    out = run(cases)
    for rec in records(out, "blue_sft") + records(out, "blue_pref"):
        for key in ("completion", "chosen", "rejected"):
            if key in rec:
                assert json.loads(rec[key])["summary"] == "Modifica app.py."
    assert not any(f"parche {n}" in json.dumps(records(out, k)) for k in ("blue_sft", "blue_pref") for n in ("good", "half", "breaks"))


def test_pairs_are_capped_per_case_with_round_robin_chosen(cases):
    c = cases[0]
    v, r = f"{c.id}-visible-1", f"{c.id}-reserved-1"
    data = json.loads((c.root / "case.json").read_text())
    for n in ("good2", "good3"):
        (c.root / "prs" / n).mkdir()
        (c.root / "prs" / n / "app.py").write_text(f"print('{n}')\n")
        data["prs"].append({"name": n, "kind": "correct_patch"})
    (c.root / "case.json").write_text(json.dumps(data))
    c = load_case(c.root)
    rows = rows_for(c.id) + [row(c.id, n, "correct_patch", True, "approved", blocked=[v, r]) for n in ("good2", "good3")]
    pairs = bench.harvest([c], rows, COMMIT)["blue_pref.train"] + bench.harvest([c], rows, COMMIT)["blue_pref.val"]
    assert len(pairs) == 2  # half y breaks, cada uno con un solo chosen
    chosen = {p["metadata"]["rejected"]["pr"]: p["metadata"]["chosen"]["pr"] for p in pairs}
    assert chosen == {"half": "good", "breaks": "good2"}  # chosen por orden de nombre, en rotación
    assert bench.harvest([c], rows, COMMIT) == bench.harvest([c], rows, COMMIT)


def test_undecodable_patch_file_drops_the_pr_without_crashing(cases, capsys):
    patch_is_dropped(cases, capsys, lambda d: (d / "app.py").write_bytes(b"\xff\xfe\x00"))
