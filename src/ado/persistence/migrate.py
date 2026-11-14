"""Применение схемы без Alembic: `python -m ado.persistence.migrate` (идемпотентно).

Основной путь в Docker — `alembic upgrade head` (тот же DDL из `ado.persistence.ddl`).
"""

from __future__ import annotations

import os
import sys

from ado.persistence.db import database_from_url
from ado.persistence.store import PlanStore


def main() -> int:
    url = os.environ.get("ADO_DATABASE_URL")
    if not url:
        print("ADO_DATABASE_URL не задан — миграция пропущена")
        return 0
    PlanStore(database_from_url(url)).migrate()
    print("схема БД применена")
    return 0


if __name__ == "__main__":
    sys.exit(main())
