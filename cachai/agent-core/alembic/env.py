from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

AGENT_CORE_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_CORE_DIR))

from app.config import get_settings

# Raw SQL migrations are used directly without SQLAlchemy ORM metadata.
target_metadata = None


def _to_sqlalchemy_url(url: str) -> str:
    """Convert standard PostgreSQL URLs to SQLAlchemy's psycopg3 dialect."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def _get_sqlalchemy_url() -> str:
    return _to_sqlalchemy_url(get_settings().database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    sqlalchemy_url = _get_sqlalchemy_url()
    context.configure(
        url=sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    config = context.config
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


def _run_from_alembic_context() -> None:
    try:
        config = context.config
    except Exception:
        # Allow importing helpers (e.g. _to_sqlalchemy_url) outside Alembic CLI runtime.
        return

    if config.config_file_name is not None:
        fileConfig(config.config_file_name, disable_existing_loggers=False)

    sqlalchemy_url = _get_sqlalchemy_url()
    config.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))

    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()


_run_from_alembic_context()
