from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
    # the vector type only exists once the extension is created
    exists = conn.execute(
        "select 1 from pg_extension where extname='vector'"
    ).fetchone()
    conn.commit()
    if exists:
        register_vector(conn)
    return conn


def migrate(conn: psycopg.Connection) -> None:
    conn.execute(
        "create table if not exists schema_migrations("
        "name text primary key, applied_at timestamptz not null default now())"
    )
    conn.commit()
    applied = {r["name"] for r in conn.execute("select name from schema_migrations")}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        conn.execute(path.read_text())
        conn.execute("insert into schema_migrations(name) values (%s)", (path.name,))
        conn.commit()
    if conn.execute("select 1 from pg_extension where extname='vector'").fetchone():
        register_vector(conn)
    conn.commit()
