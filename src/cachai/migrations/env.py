"""Entorno de Alembic. La URL llega por configuración desde cachai.db.upgrade."""
from alembic import context
from sqlalchemy import create_engine

from cachai.db import Base

config = context.config
target_metadata = Base.metadata


def run() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run()
