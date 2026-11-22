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
    lines = {l.split("|")[1].strip(" `"): l for l in out.splitlines() if l.startswith("| `")}
    assert "sin desviación detectada" in lines["honesto"]
    assert "DISTINGUIBLE" in lines["prompt-oculto"] and "DISTINGUIBLE" in lines["modelo-chico"]
