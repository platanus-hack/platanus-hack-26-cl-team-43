import os

import pytest

from radar import db

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/radar_test"
)


@pytest.fixture
def conn():
    c = db.connect(TEST_DATABASE_URL)
    db.migrate(c)
    tables = [
        r["tablename"]
        for r in c.execute(
            "select tablename from pg_tables where schemaname='public' "
            "and tablename <> 'schema_migrations'"
        )
    ]
    c.execute(
        "truncate " + ", ".join(f'"{t}"' for t in tables) + " restart identity cascade"
    )
    c.commit()
    yield c
    c.rollback()
    c.close()
