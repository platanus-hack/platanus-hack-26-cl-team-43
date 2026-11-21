"""Cliente LLM compatible con OpenAI: esquemas, errores, timeouts y respaldo sin red."""

import json
import http.server
import threading
import time
from pathlib import Path

import pytest

from cachai import fixer, gitio, llm, redteam
from cachai.case import load_case, materialize

FACTURAS = Path(__file__).resolve().parent.parent / "cases" / "facturas"

SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2}},
    "required": ["n", "tags"],
    "additionalProperties": False,
}


class Mock:
    """Servidor HTTP falso: responde con `reply(path, body)` y anota cada pedido."""

    def __init__(self, content=None, status=200, delay=0.0, raw=None, statuses=None, location=None):
        self.requests: list[dict] = []
        self.content, self.status, self.delay, self.raw = content, status, delay, raw
        self.statuses, self.location = list(statuses or []), location
        mock = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                mock.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                time.sleep(mock.delay)
                payload = mock.raw if mock.raw is not None else json.dumps(
                    {"choices": [{"message": {"content": mock.content if isinstance(mock.content, str) else json.dumps(mock.content)}}]})
                try:
                    self.send_response(mock.statuses.pop(0) if mock.statuses else mock.status)
                    if mock.location:
                        self.send_header("Location", mock.location)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload.encode())
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


def cfg(mock, **kw):
    return llm.LLMConfig(base_url=mock.url, api_key=kw.pop("api_key", "secreto-123"), red_model=kw.pop("red", "org/red-lora"),
                         blue_model=kw.pop("blue", "org/blue-lora"), timeout=kw.pop("timeout", 5.0))


# --- configuración ---------------------------------------------------------


def test_config_from_env_is_none_without_base_url():
    assert llm.LLMConfig.from_env({}) is None
    assert llm.LLMConfig.from_env({"CACHAI_LLM_API_KEY": "x"}) is None


def test_config_from_env_reads_all_variables():
    c = llm.LLMConfig.from_env({"CACHAI_LLM_BASE_URL": "https://h/v1/", "CACHAI_LLM_API_KEY": "k",
                                "CACHAI_RED_MODEL": "r", "CACHAI_BLUE_MODEL": "b", "CACHAI_LLM_TIMEOUT": "7"})
    assert (c.base_url, c.api_key, c.red_model, c.blue_model, c.timeout) == ("https://h/v1", "k", "r", "b", 7.0)
    assert c.max_tokens == 8192


def test_config_rejects_non_http_base_url():
    with pytest.raises(llm.LLMError):
        llm.LLMConfig.from_env({"CACHAI_LLM_BASE_URL": "file:///etc/passwd"})


def test_config_reads_max_tokens_and_rejects_garbage():
    base = {"CACHAI_LLM_BASE_URL": "http://localhost/v1"}
    assert llm.LLMConfig.from_env({**base, "CACHAI_LLM_MAX_TOKENS": "100"}).max_tokens == 100
    for bad in ("abc", "0", "-5"):
        with pytest.raises(llm.LLMError):
            llm.LLMConfig.from_env({**base, "CACHAI_LLM_MAX_TOKENS": bad})


def test_invalid_timeout_is_an_llm_error():
    with pytest.raises(llm.LLMError):
        llm.LLMConfig.from_env({"CACHAI_LLM_BASE_URL": "http://localhost/v1", "CACHAI_LLM_TIMEOUT": "abc"})


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/v1", "http://localhost/v1", "https://api.example.com/v1"])
def test_api_key_allowed_on_https_or_localhost(url):
    assert llm.LLMConfig(url, "k", "r", "b").api_key == "k"


@pytest.mark.parametrize("url", ["http://api.example.com/v1", "http://10.0.0.5/v1", "http://localhost.evil.com/v1"])
def test_api_key_refused_over_plain_http_to_remote_hosts(url):
    with pytest.raises(llm.LLMError, match="http"):
        llm.LLMConfig(url, "k", "r", "b")
    assert llm.LLMConfig(url, "", "r", "b").base_url == url  # sin clave no hay nada que proteger


def test_load_config_warns_on_stderr_without_leaking_the_key(capsys, monkeypatch):
    monkeypatch.setenv("CACHAI_LLM_BASE_URL", "ftp://h/v1")
    monkeypatch.setenv("CACHAI_LLM_API_KEY", "secreto-123")
    assert llm.load_config() is None
    err = capsys.readouterr().err
    assert "aviso" in err and "CACHAI_LLM_BASE_URL" in err and "secreto-123" not in err
    monkeypatch.setenv("CACHAI_LLM_BASE_URL", "http://localhost/v1")
    monkeypatch.setenv("CACHAI_LLM_TIMEOUT", "abc")
    assert llm.load_config() is None and "CACHAI_LLM_TIMEOUT" in capsys.readouterr().err
    monkeypatch.delenv("CACHAI_LLM_BASE_URL")
    assert llm.load_config() is None and capsys.readouterr().err == ""  # sin configurar no es un aviso


def test_config_repr_hides_api_key():
    assert "secreto-123" not in repr(llm.LLMConfig("https://h/v1", "secreto-123", "r", "b"))


# --- cliente ---------------------------------------------------------------


def test_valid_structured_response_is_parsed_and_request_is_well_formed():
    with Mock({"n": "3", "tags": ["a"]}) as m:
        out = llm.complete_json(cfg(m), "red-lora", "sistema", "usuario", "demo", SCHEMA)
    assert out == {"n": "3", "tags": ["a"]}
    (req,) = m.requests
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer secreto-123"
    assert req["body"]["model"] == "red-lora"
    assert req["body"]["messages"] == [{"role": "system", "content": "sistema"}, {"role": "user", "content": "usuario"}]
    rf = req["body"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "demo" and rf["json_schema"]["schema"] == SCHEMA


def test_max_tokens_is_sent():
    with Mock({"n": "1", "tags": []}) as m:
        llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA)
    assert m.requests[0]["body"]["max_tokens"] == 8192


@pytest.mark.parametrize("wrap", [
    lambda t: "<think>\nrazono {con llaves}\n</think>\n" + t,
    lambda t: "```json\n" + t + "\n```",
    lambda t: "<think>x</think>\n```\n" + t + "\n```\n",
])
def test_think_blocks_and_code_fences_are_stripped(wrap):
    with Mock(wrap('{"n": "7", "tags": []}')) as m:
        assert llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA) == {"n": "7", "tags": []}


def test_400_on_response_format_retries_once_without_it_and_validates_locally():
    with Mock({"n": "1", "tags": []}, statuses=[400, 200]) as m:
        assert llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA) == {"n": "1", "tags": []}
    assert "response_format" in m.requests[0]["body"] and "response_format" not in m.requests[1]["body"]
    with Mock({"n": 1, "tags": []}, statuses=[400, 200]) as m:  # el reintento también se valida
        with pytest.raises(llm.LLMError):
            llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA)
    with Mock({"n": "1", "tags": []}, status=400) as m:  # un segundo 400 no se reintenta de nuevo
        with pytest.raises(llm.LLMError, match="400"):
            llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA)
    assert len(m.requests) == 2


def test_http_error_carries_its_status():
    with Mock({"n": "1", "tags": []}, status=429) as m:
        with pytest.raises(llm.LLMHTTPError) as e:
            llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA)
    assert e.value.status == 429 and isinstance(e.value, llm.LLMError)


def test_deeply_nested_json_is_an_llm_error_not_a_recursion_error():
    with Mock("[" * 200_000 + "]" * 200_000) as m:
        with pytest.raises(llm.LLMError):
            llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA)


@pytest.mark.parametrize("status", [301, 302, 307])
def test_redirects_are_never_followed_so_the_key_is_not_forwarded(status):
    with Mock({"n": "1", "tags": []}) as target:
        with Mock(status=status, location=target.url + "/chat/completions") as m:
            with pytest.raises(llm.LLMError, match=str(status)):
                llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA)
    assert target.requests == []


def test_no_authorization_header_without_api_key():
    with Mock({"n": "1", "tags": []}) as m:
        llm.complete_json(cfg(m, api_key=""), "m", "s", "u", "demo", SCHEMA)
    assert "Authorization" not in m.requests[0]["headers"]


@pytest.mark.parametrize("content", [
    "esto no es json",
    '{"n": 1',
    {"n": 1, "tags": ["a"]},           # tipo incorrecto
    {"tags": []},                      # falta una clave
    {"n": "1", "tags": [], "extra": 1},  # clave no declarada
    {"n": "1", "tags": ["a", "b", "c"]},  # más elementos de los permitidos
    [1, 2],
])
def test_malformed_or_schema_invalid_content_is_rejected(content):
    with Mock(content) as m:
        with pytest.raises(llm.LLMError):
            llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA)


@pytest.mark.parametrize("raw", ["no json", "{}", '{"choices": []}', '{"choices": [{"message": {}}]}', '{"choices": [{"message": {"content": null}}]}'])
def test_unexpected_envelope_is_rejected(raw):
    with Mock(raw=raw) as m:
        with pytest.raises(llm.LLMError):
            llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA)


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_http_errors_are_wrapped_and_never_leak_the_key(status):
    with Mock({"n": "1", "tags": []}, status=status) as m:
        with pytest.raises(llm.LLMError) as e:
            llm.complete_json(cfg(m), "m", "s", "u", "demo", SCHEMA)
    assert str(status) in str(e.value) and "secreto-123" not in str(e.value)


def test_timeout_is_an_llm_error():
    with Mock({"n": "1", "tags": []}, delay=1.0) as m:
        t0 = time.monotonic()
        with pytest.raises(llm.LLMError, match="tiempo"):
            llm.complete_json(cfg(m, timeout=0.2), "m", "s", "u", "demo", SCHEMA)
    assert time.monotonic() - t0 < 0.9


def test_connection_refused_is_an_llm_error():
    with Mock() as m:
        c = cfg(m)
    with pytest.raises(llm.LLMError):
        llm.complete_json(c, "m", "s", "u", "demo", SCHEMA)


def test_validate_schema_integer_and_boolean_never_confuse_bool_with_int():
    llm.validate_schema(3, {"type": "integer"})
    llm.validate_schema(True, {"type": "boolean"})
    for value, t in ((True, "integer"), (False, "integer"), (1, "boolean"), ("1", "integer"), (1.5, "integer")):
        with pytest.raises(llm.LLMError):
            llm.validate_schema(value, {"type": t})


def test_validate_schema_nested_and_enum():
    schema = {"type": "object", "properties": {"k": {"type": "string", "enum": ["a", "b"]},
                                               "l": {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "string", "minLength": 1}},
                                                                                "required": ["x"]}}},
              "required": ["k", "l"]}
    llm.validate_schema({"k": "a", "l": [{"x": "y"}]}, schema)
    for bad in ({"k": "z", "l": []}, {"k": "a", "l": [{"x": ""}]}, {"k": "a", "l": [{}]}):
        with pytest.raises(llm.LLMError):
            llm.validate_schema(bad, schema)


# --- equipo rojo -----------------------------------------------------------

INVENTORY = redteam.Inventory.from_dict({
    "users": {"ana": {"Authorization": "Bearer a"}, "beto": {"Authorization": "Bearer b"}},
    "records": [{"id": "10", "owner": "ana", "markers": ["MARCA-ANA"]}, {"id": "20", "owner": "beto", "markers": ["MARCA-BETO"]}],
    "files": [{"path": "beto/doc.txt", "owner": "beto", "markers": ["MARCA-DOC"]}],
})


def variant(**kw):
    base = {"family": "authz", "path": "/invoices/20", "attacker": "ana", "target": "20", "owner_path": "/invoices/20"}
    return {**base, **kw}


def test_red_variants_use_inventory_identities_and_the_red_model():
    with Mock({"variants": [variant()]}) as m:
        variants = redteam.llm_variants(cfg(m), INVENTORY)
    assert variants == [variant()]
    (c,) = redteam.variant_candidates(variants, INVENTORY, set())
    assert c.family == "authz"
    assert c.request == {"method": "GET", "path": "/invoices/20", "headers": {"Authorization": "Bearer a"}}
    assert c.owner_request["headers"] == {"Authorization": "Bearer b"} and c.markers == ("MARCA-BETO",)
    assert m.requests[0]["body"]["model"] == "org/red-lora"  # cambiar de LoRA es solo configuración
    assert "MARCA-BETO" not in json.dumps(m.requests[0]["body"])  # las marcas no salen hacia el modelo


BAD_VARIANTS = [variant(attacker="nadie"), variant(attacker="beto"), variant(target="99"), variant(path="http://evil/x"),
                variant(path="//evil"), variant(family="files"), variant(owner_path="relativa")]


def test_red_variants_drop_invalid_ones_but_keep_valid_ones():
    with Mock({"variants": [*BAD_VARIANTS, variant(path="/invoices/20?x=1")]}) as m:
        assert [v["path"] for v in redteam.llm_variants(cfg(m), INVENTORY)] == ["/invoices/20?x=1"]


def test_variant_candidates_revalidate_untrusted_data():
    """Dentro del contenedor el archivo se trata como no confiable: se valida igual que en el host."""
    cands = redteam.variant_candidates([*BAD_VARIANTS, variant(path="/invoices/20?x=1")], INVENTORY, set())
    assert [c.request["path"] for c in cands] == ["/invoices/20?x=1"]
    assert redteam.variant_candidates([variant(), variant()], INVENTORY, set())[0].id  # duplicados: queda uno
    assert len(redteam.variant_candidates([variant(), variant()], INVENTORY, set())) == 1


def test_group_is_derived_from_the_normalized_path_not_from_the_model():
    a = redteam.variant_candidates([variant(), variant(attacker="beto", target="10", path="/invoices/10", owner_path="/invoices/10")], INVENTORY, set())
    assert len(a) == 2 and a[0].group == a[1].group  # los ids se normalizan: misma ruta, mismo grupo
    (pdf,) = redteam.variant_candidates([variant(path="/invoices/20/pdf")], INVENTORY, set())
    (plain,) = redteam.variant_candidates([variant()], INVENTORY, set())
    assert pdf.group != plain.group
    # El modelo no puede colapsar la partición eligiendo un grupo: la clave ya no existe en el esquema.
    assert "group" not in llm.VARIANTS_SCHEMA["properties"]["variants"]["items"]["properties"]
    with pytest.raises(llm.LLMError):
        llm.validate_schema({"variants": [{**variant(), "group": "todo"}]}, llm.VARIANTS_SCHEMA)


def test_group_ignores_query_values_so_the_model_cannot_forge_groups():
    paths = [f"/invoices/20?n={i}" for i in range(5)]
    cands = redteam.variant_candidates([variant(path=p, owner_path="/invoices/20") for p in paths], INVENTORY, set())
    assert len(cands) == 5 and len({c.group for c in cands}) == 1
    other = redteam.variant_candidates([variant(path="/invoices/20?otro=1", owner_path="/invoices/20")], INVENTORY, set())
    assert other[0].group != cands[0].group  # cambian las claves de la consulta: otro grupo
    assert redteam._normalized_group("/x?b=1&a=2", INVENTORY) == redteam._normalized_group("/x?a=9&b=8", INVENTORY)


def test_group_replaces_ids_per_segment_not_per_substring():
    norm = lambda p: redteam._normalized_group(p, INVENTORY)  # noqa: E731
    assert norm("/v1/invoices/20") != "/v{id}/invoices/{id}" and "/v1/" in norm("/v1/invoices/20")
    assert norm("/v1/invoices/20") == norm("/v1/invoices/10")
    assert norm("/v10/invoices/20").startswith("/v10/")  # `10` es un id, pero `v10` no es un segmento igual a él
    assert norm("/files/beto/doc.txt") == norm("/files/beto%2Fdoc.txt") == "/files/{file}"
    assert norm("/files/doc.txt") == "/files/{file}"


def test_group_for_files_hides_the_file():
    (c,) = redteam.variant_candidates([variant(family="files", target="beto/doc.txt", path="/download?name=../beto/doc.txt", owner_path="/download?name=doc.txt")], INVENTORY, set())
    assert "doc" not in c.group


def test_red_prompt_includes_route_templates_but_no_data():
    routes = [("/invoices/{invoice_id}", ["invoice_id"], []), ("/download", [], ["name"])]
    with Mock({"variants": []}) as m:
        redteam.llm_variants(cfg(m), INVENTORY, routes)
    prompt = m.requests[0]["body"]["messages"][1]["content"]
    assert "/invoices/{invoice_id}" in prompt and "/download" in prompt and "MARCA" not in prompt


def test_variant_candidates_skip_nothing_already_taken_by_id():
    (c,) = redteam.variant_candidates([variant()], INVENTORY, set())
    assert redteam.variant_candidates([variant()], INVENTORY, {c.id}) == []


def test_load_variants_survives_deeply_nested_json(tmp_path, capsys):
    f = tmp_path / "v.json"
    f.write_text("[" * 200_000 + "]" * 200_000)
    assert redteam.load_variants(f, INVENTORY) == []
    assert "aviso" in capsys.readouterr().err


def test_load_variants_file_rejects_tampered_content(tmp_path, capsys):
    f = tmp_path / "v.json"
    assert redteam.load_variants(None, INVENTORY) == []
    for content in ("no json", '{"variants": "x"}', json.dumps({"variants": [{"family": "authz"}]}), json.dumps([variant()])):
        f.write_text(content)
        assert redteam.load_variants(f, INVENTORY) == []
        assert "aviso" in capsys.readouterr().err
    f.write_text(json.dumps({"variants": [variant(), *BAD_VARIANTS]}))
    assert len(redteam.load_variants(f, INVENTORY)) == 1
    assert redteam.load_variants(tmp_path / "no-existe.json", INVENTORY) == []


@pytest.mark.parametrize("content", ["basura", {"variants": "no es lista"}, {"variants": [{"family": "authz"}]}])
def test_red_variants_degrade_to_nothing_on_bad_output_and_warn(content, capsys):
    with Mock(content) as m:
        assert redteam.llm_variants(cfg(m), INVENTORY) == []
    assert "aviso" in capsys.readouterr().err


def test_red_variants_degrade_on_http_error_and_timeout_with_warning(capsys):
    with Mock({"variants": [variant()]}, status=500) as m:
        assert redteam.llm_variants(cfg(m), INVENTORY) == []
    err = capsys.readouterr().err
    assert "aviso" in err and "500" in err and "secreto-123" not in err
    with Mock({"variants": [variant()]}, delay=1.0) as m:
        assert redteam.llm_variants(cfg(m, timeout=0.2), INVENTORY) == []
    assert "tiempo" in capsys.readouterr().err


def test_red_variants_without_config_make_no_request():
    assert redteam.llm_variants(None, INVENTORY) == []


def test_red_without_red_model_makes_no_request():
    with Mock({"variants": [variant()]}) as m:
        assert redteam.llm_variants(cfg(m, red=""), INVENTORY) == []
        assert m.requests == []


def test_container_side_redteam_does_not_read_llm_environment():
    """El contenedor no lee CACHAI_LLM_*: solo recibe candidatos ya validados por el host."""
    source = Path(redteam.__file__).read_text()
    assert "from_env" not in source and "load_config" not in source and "os.environ" not in source


# --- cableado del sandbox --------------------------------------------------


def test_run_redteam_mounts_the_variants_file_and_passes_no_llm_env(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from cachai import sandbox

    seen = {}

    def fake_run(app, name, mounts, args, budget, entrypoint=None, aliases=()):
        seen.update(mounts={dst: src.read_text() for src, dst in mounts}, args=list(args), entrypoint=entrypoint)
        return 0, json.dumps({"attacks": []}), ""

    monkeypatch.setattr(sandbox, "_run_trusted", fake_run)
    monkeypatch.setenv("CACHAI_LLM_API_KEY", "secreto-123")
    app = SimpleNamespace(run_id="r", base_url="http://app:8000", container="c")
    case = SimpleNamespace(app=SimpleNamespace(health="/health"))
    inv = tmp_path / "inventory.json"
    inv.write_text("{}")
    sandbox.run_redteam(app, case, inv, 5.0, llm_variants=[variant()])
    assert json.loads(seen["mounts"]["/work/llm_variants.json"]) == {"variants": [variant()]}
    assert seen["args"][seen["args"].index("--llm-variants") + 1] == "/work/llm_variants.json"
    assert "secreto-123" not in json.dumps(seen)  # el contenedor no recibe la clave

    seen.clear()
    sandbox.run_redteam(app, case, inv, 5.0)
    assert "/work/llm_variants.json" not in seen["mounts"] and "--llm-variants" not in seen["args"]


def test_fetch_routes_runs_the_trusted_image_and_returns_only_templates(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from cachai import sandbox

    seen = {}

    def fake_run(app, name, mounts, args, budget, entrypoint=None, aliases=()):
        seen.update(args=list(args), entrypoint=entrypoint, mounts=[dst for _, dst in mounts])
        routes = [["/invoices/{invoice_id}", ["invoice_id"], []], ["/q", [], ["name"]], "basura", [1, 2, 3], ["//evil", [], []], ["/x", "no", []]]
        return 0, json.dumps({"routes": routes}), ""

    monkeypatch.setattr(sandbox, "_run_trusted", fake_run)
    app = SimpleNamespace(run_id="r", base_url="http://app:8000", container="c")
    case = SimpleNamespace(app=SimpleNamespace(health="/health"))
    inv = tmp_path / "inventory.json"
    inv.write_text("{}")
    assert sandbox.fetch_routes(app, case, inv) == [("/invoices/{invoice_id}", ["invoice_id"], []), ("/q", [], ["name"])]
    assert "--routes-only" in seen["args"] and seen["entrypoint"][-1] == "cachai.redteam"
    monkeypatch.setattr(sandbox, "_run_trusted", lambda *a, **k: (1, "", "boom"))
    assert sandbox.fetch_routes(app, case, inv) == []  # sin rutas el equipo rojo sigue con lo que tiene


def test_fetch_routes_bounds_what_goes_into_the_prompt(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from cachai import sandbox

    ok = ["/ok/{id}", ["id"], []]
    big = [
        ["/" + "a" * 200, [], []],                       # plantilla de 201: se descarta
        ["/p", [f"p{i}" for i in range(21)], []],        # 21 parámetros
        ["/q", [], ["x" * 61]],                          # parámetro de 61 caracteres
        ["/edge/" + "b" * 194, ["x" * 60] * 20, []],    # justo en los límites: se conserva
    ]
    many = [[f"/r{i}/" + "c" * 140, ["id"], []] for i in range(200)]
    monkeypatch.setattr(sandbox, "_run_trusted", lambda *a, **k: (0, json.dumps({"routes": [ok, *big, *many]}), ""))
    app = SimpleNamespace(run_id="r", base_url="http://app:8000", container="c")
    case = SimpleNamespace(app=SimpleNamespace(health="/health"))
    inv = tmp_path / "inventory.json"
    inv.write_text("{}")
    routes = sandbox.fetch_routes(app, case, inv)
    templates = [t for t, _, _ in routes]
    assert templates[0] == "/ok/{id}" and any(t.startswith("/edge/") for t in templates)
    assert not any(len(t) > 200 for t in templates) and "/p" not in templates and "/q" not in templates
    assert sum(len(json.dumps(list(r))) for r in routes) <= 16_384 and 3 < len(routes) < 200  # el tope corta el resto


def test_routes_payload_lists_get_templates_only():
    spec = {"paths": {"/a/{id}": {"get": {"parameters": [{"in": "path", "name": "id"}]}}, "/b": {"post": {}}}}
    assert redteam.routes_payload(spec) == {"routes": [["/a/{id}", ["id"], []]]}


# --- parchador -------------------------------------------------------------


@pytest.fixture(scope="module")
def case():
    return load_case(FACTURAS)


@pytest.fixture
def trusted(case, tmp_path):
    repo = materialize(case, tmp_path / "repo")
    return repo, gitio.resolve(repo, "main")


def run_propose(case, trusted, monkeypatch, mock=None, name="bot-llm-1"):
    repo, base = trusted
    if mock is None:
        monkeypatch.delenv("CACHAI_LLM_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("CACHAI_LLM_BASE_URL", mock.url)
        monkeypatch.setenv("CACHAI_LLM_API_KEY", "secreto-123")
        monkeypatch.setenv("CACHAI_BLUE_MODEL", "org/blue-lora")
        monkeypatch.setenv("CACHAI_LLM_TIMEOUT", "0.5")
    return fixer.propose(repo, base, name, "llm", "org/blue-lora", "arregla", [], [], isolate=False)


def patch_reply(path="invoices/main.py", content="# parche\n"):
    return {"summary": "listo", "files": [{"path": path, "content": content}]}


def test_llm_agent_writes_validated_patch_and_uses_blue_model(case, trusted, monkeypatch):
    with Mock(patch_reply(content="# parche del modelo\n")) as m:
        prop = run_propose(case, trusted, monkeypatch, m)
    assert prop.error is None and prop.changed == ["invoices/main.py"] and "+# parche del modelo" in prop.diff
    assert m.requests[0]["body"]["model"] == "org/blue-lora"
    assert "invoices/main.py" in m.requests[0]["body"]["messages"][1]["content"]  # el modelo ve los archivos de la base


@pytest.mark.parametrize("path", ["../fuera.py", "/etc/passwd", ".git/config", "tests/test_x.py", "no_existe.py", "invoices/../invoices/main.py"])
def test_llm_agent_rejects_unsafe_or_unseen_paths(case, trusted, monkeypatch, path):
    with Mock(patch_reply(path=path)) as m:
        prop = run_propose(case, trusted, monkeypatch, m)
    assert prop.changed == [] and prop.error


@pytest.mark.parametrize("reply", ["basura", {"summary": "x", "files": []}, {"summary": "x"}])
def test_llm_agent_rejects_malformed_replies_without_touching_the_branch(case, trusted, monkeypatch, reply):
    with Mock(reply) as m:
        prop = run_propose(case, trusted, monkeypatch, m)
    repo, base = trusted
    assert prop.changed == [] and prop.error and "secreto-123" not in prop.error
    assert gitio.resolve(repo, "main") == base


def test_llm_agent_uses_the_given_tests_dir(case, trusted, monkeypatch):
    repo, base = trusted
    monkeypatch.setenv("CACHAI_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    with Mock(patch_reply()) as m:
        monkeypatch.setenv("CACHAI_LLM_BASE_URL", m.url)
        # Si la carpeta de tests fuera "invoices", ese código no se enviaría ni se podría editar.
        prop = fixer.propose(repo, base, "bot-llm-2", "llm", "org/blue-lora", "arregla", [], [], isolate=False, tests_dir="invoices")
    assert prop.changed == [] and prop.error
    assert "invoices/main.py" not in m.requests[0]["body"]["messages"][1]["content"]


def test_fix_with_bad_llm_config_prints_clean_error(monkeypatch, capsys):
    from cachai import cli
    monkeypatch.setenv("CACHAI_LLM_BASE_URL", "ftp://x")
    monkeypatch.setenv("CACHAI_LLM_API_KEY", "secreto-123")
    assert cli.main(["fix", str(FACTURAS), "--agent", "llm", "--out", "/nonexistent-out"]) != 0
    err = capsys.readouterr().err
    assert "CACHAI_LLM_BASE_URL" in err and "Traceback" not in err and "secreto-123" not in err
    monkeypatch.delenv("CACHAI_LLM_BASE_URL")
    assert cli.main(["fix", str(FACTURAS), "--agent", "llm"]) != 0
    assert "CACHAI_LLM_BASE_URL" in capsys.readouterr().err


def test_fix_survives_model_ids_with_slashes(case, tmp_path, monkeypatch):
    """Los ids reales son `org/nombre`: el nombre del intento y de la rama no pueden llevar `/`."""
    monkeypatch.setattr(fixer.pipeline, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sin docker en este test")))
    with Mock(patch_reply()) as m:
        monkeypatch.setenv("CACHAI_LLM_BASE_URL", m.url)
        monkeypatch.setenv("CACHAI_BLUE_MODEL", "Qwen/Qwen3-8B")
        rows = fixer.run(case, "llm", runs=1, out_dir=tmp_path / "out")
    (row,) = rows
    assert "/" not in row["name"] and row["name"].startswith("bot-llm-qwen-qwen3-8b") and row["model"] == "Qwen/Qwen3-8B"
    assert row["changed"] == ["invoices/main.py"]
    assert m.requests[0]["body"]["model"] == "Qwen/Qwen3-8B"  # el id real viaja intacto al servidor
    assert (tmp_path / "out" / case.id / row["name"] / "parche.diff").is_file()


def test_attempt_name_truncates_long_model_ids():
    name = fixer.attempt_name("llm", "org/" + "m" * 400, "normal", "20260101t000000z-ab12", 1)
    assert len(name) < 100 and name.endswith("-20260101t000000z-ab12-1") and "/" not in name


def test_opener_ignores_proxy_environment():
    from urllib.request import ProxyHandler
    # `ProxyHandler({})` no registra nada y desplaza al ProxyHandler por defecto, que leería HTTP_PROXY.
    assert not any(isinstance(h, ProxyHandler) for h in llm._OPENER.handlers)


def test_external_agents_do_not_inherit_llm_environment(case, trusted, monkeypatch):
    repo, base = trusted
    for k in ("CACHAI_LLM_API_KEY", "CACHAI_LLM_BASE_URL", "CACHAI_RED_MODEL", "CACHAI_BLUE_MODEL", "CACHAI_LLM_TIMEOUT"):
        monkeypatch.setenv(k, "secreto-123" if k.endswith("KEY") else "x")
    monkeypatch.setenv("CACHAI_MASK", "")  # otras variables de CACHAI_ sí pasan
    code = "import os; print(sorted(k for k in os.environ if k.startswith('CACHAI')))"
    monkeypatch.setitem(fixer.AGENTS, "espia-env", ["python3", "-c", code])
    prop = fixer.propose(repo, base, "bot-env-1", "espia-env", "m", "p", [], [], isolate=False)
    assert prop.transcript == "['CACHAI_MASK']" and "secreto-123" not in prop.transcript


def test_attempt_name_is_a_safe_ref():
    assert fixer.attempt_name("llm", "Org/Mi Adapter..v1.lock", "minimo", "20260101t000000z-ab12", 2) == \
        "bot-llm-org-mi-adapter-v1-lock-minimo-20260101t000000z-ab12-2"


def test_llm_agent_without_config_reports_error_and_makes_no_request(case, trusted, monkeypatch):
    prop = run_propose(case, trusted, monkeypatch, None)
    assert prop.changed == [] and "CACHAI_LLM_BASE_URL" in prop.error


def test_llm_agent_timeout_is_reported_not_raised(case, trusted, monkeypatch):
    with Mock(patch_reply(), delay=1.5) as m:
        prop = run_propose(case, trusted, monkeypatch, m)
    assert prop.changed == [] and "tiempo" in prop.error
