"""Análisis estático como nota (#32): solo líneas cambiadas, severidad media o alta, nunca bloquea."""

import subprocess

from cachai import static

REPORT = {"results": [
    {"filename": "/code/app/db.py", "line_number": 12, "test_id": "B608", "issue_severity": "MEDIUM",
     "issue_text": "Possible SQL injection vector through string-based query construction."},
    {"filename": "/code/app/db.py", "line_number": 3, "test_id": "B608", "issue_severity": "MEDIUM", "issue_text": "vieja"},
    {"filename": "/code/app/x.py", "line_number": 5, "test_id": "B101", "issue_severity": "LOW", "issue_text": "assert"},
]}


def test_only_changed_lines_with_medium_or_high_severity():
    notes = static.parse(REPORT, {"app/db.py": {10, 11, 12}, "app/x.py": {5}})
    assert notes == ["análisis estático (bandit B608, severidad medium): app/db.py:12 "
                     "Possible SQL injection vector through string-based query construction."]


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_targets_are_changed_app_files_and_their_lines(tmp_path):
    r = tmp_path / "r"
    (r / "app").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "app" / "db.py").write_text("a = 1\nb = 2\nc = 3\n")
    (r / "tests" / "test_a.py").write_text("x = 1\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    base = _git(r, "rev-parse", "HEAD")
    (r / "app" / "db.py").write_text("a = 1\nb = 20\nc = 3\nd = 4\n")
    (r / "app" / "nuevo.py").write_text("y = 1\n")
    (r / "tests" / "test_a.py").write_text("x = 2\n")
    (r / "README.md").write_text("doc\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "pr")
    pr = _git(r, "rev-parse", "HEAD")
    assert static.targets(r, base, pr, "tests") == {"app/db.py": {2, 4}, "app/nuevo.py": {1}}
