"""El CLI: atajos de proveedor, cachai.toml y la demo sin claves."""
import argparse

import pytest

from cachai import cli


def test_atajo_de_proveedor(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "k")
    t = cli._endpoint("together:Qwen/Qwen2.5-7B-Instruct-Turbo")
    assert t.base_url == "https://api.together.xyz/v1" and t.api_key == "k"
    assert t.model == "Qwen/Qwen2.5-7B-Instruct-Turbo"


def test_modelo_con_dos_puntos_y_sin_clave():
    t = cli._endpoint("ollama:qwen2.5:1.5b-instruct-q8_0")
    assert t.model == "qwen2.5:1.5b-instruct-q8_0" and t.api_key == ""
    assert t.base_url == "http://localhost:11434/v1"


def test_modelo_por_defecto_y_url_propia():
    assert cli._endpoint("vllm", "mi-modelo").model == "mi-modelo"
    t = cli._endpoint("mio=http://10.0.0.5:8000/v1,,qwen")
    assert (t.name, t.base_url, t.model) == ("mio", "http://10.0.0.5:8000/v1", "qwen")


def test_errores_claros(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="console.groq.com"):
        cli._endpoint("groq:llama")
    with pytest.raises(SystemExit, match="desconocido"):
        cli._endpoint("acme:x")


def test_config_toml(tmp_path):
    cfg = tmp_path / "cachai.toml"
    cfg.write_text('model = "m"\ntasks = 10\nopenrouter = false\n'
                   '[[endpoint]]\nprovider = "ollama"\nmodel = "a"\n'
                   '[[endpoint]]\nname = "b"\nurl = "http://x/v1"\nkey_env = "K"\n')
    args = argparse.Namespace(config=str(cfg), model=None, direct=["vllm:c"], reference=None,
                              no_openrouter=False, providers=6, tasks=20, max_usd=2.0,
                              out="audit/fichas")
    cli._load_config(args)
    assert args.model == "m" and args.tasks == 10 and args.no_openrouter
    assert args.direct == ["ollama:a", "b=http://x/v1,K,", "vllm:c"]


def test_demo_marca_a_los_tramposos(tmp_path, capsys):
    cli.main(["demo", "--seed", "3", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert "\033[" not in out  # fuera de una terminal no hay códigos de color
    table = out[out.index("Medido desde afuera"):out.index("Por qué se distinguen")]
    lines = {l.split()[0]: l for l in table.splitlines() if l.startswith("  ") and l.split()}
    assert "sin desviación detectada" in lines["honesto"]
    assert "DISTINGUIBLE" in lines["prompt-oculto"] and "+32 tokens" in lines["prompt-oculto"]
    assert "DISTINGUIBLE" in lines["modelo-chico"] and "10/20" in lines["modelo-chico"]
    assert "2 de 3 endpoints se distinguen de la referencia." in out
    # cada motivo en su renglón
    assert "    - tokens de prompt: excedente constante de 32 tokens\n" in out


def test_salida_en_terminal_angosta_y_sin_marcas(capsys):
    import json
    from pathlib import Path

    from cachai import salida

    f = json.loads(next((Path(__file__).parent.parent / "results" / "proveedores").glob(
        "ficha-*07-31-56z.json")).read_text())
    salida.results(f, cols=80)
    out = capsys.readouterr().out
    assert max(len(l) for l in out.splitlines()) <= 80
    assert "wafer/us" in out and "(declara fp4)" in out
    assert "Ninguno de los 5 endpoints se distingue de la referencia." in out
    assert "Por qué se distinguen" not in out
    salida.results(f, cols=200)
    wide = capsys.readouterr().out
    row = next(l for l in wide.splitlines() if l.strip().startswith("siliconflow/fp8"))
    assert "sin desviación detectada" in row and "fp8" in row and "19/20" in row


def test_markdown_a_texto_de_terminal(capsys):
    from cachai import salida

    salida.markdown("# Línea base\n\n| Proveedor | Ciego |\n|---|---|\n| `honesto` | 0/30 |\n"
                    "| prompt-oculto | 30/30 |\n\n- nota con `código`\n")
    out = capsys.readouterr().out
    assert "Línea base" in out and "|" not in out and "`" not in out
    assert "  honesto        0/30" in out and "  prompt-oculto  30/30" in out
