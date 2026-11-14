"""Alembic: URL из ADO_DATABASE_URL; метаданные — ado.persistence.tables (SQLAlchemy Core)."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from ado.persistence.tables import metadata

target_metadata = metadata


def run_migrations_online() -> None:
    url = os.environ["ADO_DATABASE_URL"]
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=os.environ.get("ADO_DATABASE_URL", "postgresql://"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
