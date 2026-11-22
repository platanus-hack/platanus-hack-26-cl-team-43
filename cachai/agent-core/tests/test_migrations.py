"""Offline validation tests for Alembic database migrations."""

import io
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

AGENT_CORE_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = AGENT_CORE_DIR / "alembic.ini"

INITIAL_REVISION = "0001_initial_schema"
EXPECTED_TABLES = (
    "model_releases",
    "eval_datasets",
    "eval_results",
    "blog_posts",
    "audit_runs",
)


def _make_alembic_config(output_buffer: io.StringIO | None = None) -> Config:
    """Create an Alembic Config bound to agent-core/alembic.ini."""
    if output_buffer is not None:
        return Config(str(ALEMBIC_INI), output_buffer=output_buffer)
    return Config(str(ALEMBIC_INI))


@pytest.fixture(autouse=True)
def _chdir_agent_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run migration tests with agent-core as cwd so relative paths in alembic.ini resolve."""
    monkeypatch.chdir(AGENT_CORE_DIR)


def test_alembic_script_directory_loads_from_ini() -> None:
    assert ALEMBIC_INI.is_file(), f"Missing Alembic configuration at {ALEMBIC_INI}"

    cfg = _make_alembic_config()
    script_dir = ScriptDirectory.from_config(cfg)

    assert script_dir is not None
    assert Path(script_dir.dir).is_dir()


def test_single_head_and_base_initial_revision() -> None:
    cfg = _make_alembic_config()
    script_dir = ScriptDirectory.from_config(cfg)

    heads = script_dir.get_heads()
    bases = script_dir.get_bases()

    assert list(heads) == [INITIAL_REVISION]
    assert list(bases) == [INITIAL_REVISION]

    revision = script_dir.get_revision(INITIAL_REVISION)
    assert revision is not None
    assert revision.revision == INITIAL_REVISION
    assert revision.down_revision is None


def test_initial_revision_exposes_upgrade_and_downgrade() -> None:
    cfg = _make_alembic_config()
    script_dir = ScriptDirectory.from_config(cfg)

    revision = script_dir.get_revision(INITIAL_REVISION)
    assert revision is not None

    module = revision.module
    assert callable(getattr(module, "upgrade", None))
    assert callable(getattr(module, "downgrade", None))


def test_offline_upgrade_emits_idempotent_create_table_ddl() -> None:
    sql_buffer = io.StringIO()
    cfg = _make_alembic_config(output_buffer=sql_buffer)

    command.upgrade(cfg, "head", sql=True)
    generated_sql = sql_buffer.getvalue()

    assert generated_sql.strip(), "Expected offline upgrade to emit SQL DDL"
    for table in EXPECTED_TABLES:
        pattern = rf'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"?{table}"?\b'
        assert re.search(pattern, generated_sql, re.IGNORECASE), (
            f"Expected CREATE TABLE IF NOT EXISTS for '{table}' in upgrade SQL"
        )


def test_offline_downgrade_emits_drop_table_ddl() -> None:
    sql_buffer = io.StringIO()
    cfg = _make_alembic_config(output_buffer=sql_buffer)

    command.downgrade(cfg, "head:base", sql=True)
    generated_sql = sql_buffer.getvalue()

    assert generated_sql.strip(), "Expected offline downgrade to emit SQL DDL"
    for table in EXPECTED_TABLES:
        pattern = rf'DROP\s+TABLE\s+IF\s+EXISTS\s+"?{table}"?\b'
        assert re.search(pattern, generated_sql, re.IGNORECASE), (
            f"Expected DROP TABLE IF EXISTS for '{table}' in downgrade SQL"
        )
