"""Dependencias del PR sin red al construir (#30): wheels antes, build sin red, scripts de instalación bloqueados."""

from cachai import sandbox
from cachai.integrity import _dependency_diff, classify


def test_setup_py_in_the_pr_blocks_and_dependencies_are_a_note():
    flags = classify([("A", "malo/setup.py"), ("M", "requirements.txt"), ("D", "viejo/setup.py")])
    by = {f.path: f for f in flags}
    assert by["malo/setup.py"].severity == "block" and "instalación" in by["malo/setup.py"].reason
    assert by["requirements.txt"].severity == "info"
    assert "viejo/setup.py" not in by  # borrar un script de instalación no corre nada


def test_dependency_note_says_what_changed():
    d = _dependency_diff(b"fastapi==0.115.6\nhttpx==0.28.1\n# comentario\n", b"fastapi==0.115.6\nrequests==2.32.3\n")
    assert d == "+requests==2.32.3, -httpx==0.28.1"


def test_local_requirements_are_built_offline_not_downloaded():
    assert sandbox._local_requirement("./malo")
    assert sandbox._local_requirement("-e .")
    assert sandbox._local_requirement("-r otros.txt")
    assert not sandbox._local_requirement("fastapi==0.115.6")
