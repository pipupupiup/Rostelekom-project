"""Начальная схема ADO (DDL — ado.persistence.ddl)

Revision ID: 0001
Revises:
"""

from alembic import op

from ado.persistence.ddl import DROP_ALL, SCHEMA_VERSION, STATEMENTS

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)
    op.execute(f"INSERT INTO schema_version (version) VALUES ('{SCHEMA_VERSION}') ON CONFLICT DO NOTHING")


def downgrade() -> None:
    for stmt in DROP_ALL:
        op.execute(stmt)
