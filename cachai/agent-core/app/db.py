from contextlib import contextmanager
from typing import Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

_pool: ConnectionPool | None = None


def init_db_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def close_db_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_db_connection() -> Iterator:
    pool = init_db_pool()
    with pool.connection() as conn:
        yield conn
