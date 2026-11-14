"""Схема БД ADO (ADO-15) — единственный источник DDL.

Применяется миграцией Alembic `migrations/versions/0001_initial.py` и `ado.persistence.migrate`
(запасной путь без Alembic). Таблицы SQLAlchemy Core (`ado.persistence.tables`) описывают те же
структуры для запросов и автогенерации миграций.

* **Актуальное состояние** — `plan_current` (дата → номер версии; строка блокируется
  `SELECT … FOR UPDATE` при записи новой версии — оптимистическая блокировка по номеру);
* **история** — `plan_versions` (+ остановки `plan_stops`, причины `plan_unassigned`, аудит
  `route_audit`), неизменяемая;
* **события** — `events`, первичный ключ `(date, event_id)` — дедупликация;
* **справочники и наряды** — `service_centers`, `staff` (с остатками/вместимостью АО), `orders`.

Запрет конфликтующего назначения: в одной версии наряд может стоять только в одном визите —
частичный уникальный индекс `plan_stops(plan_id, order_id) WHERE type = 'VISIT'`.
"""

from __future__ import annotations

SCHEMA_VERSION = "0001"

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS reference_snapshots (
        snapshot_id BIGSERIAL PRIMARY KEY,
        loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        payload JSONB NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS service_centers (
        service_center_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        lat DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
        lon DOUBLE PRECISION NOT NULL CHECK (lon BETWEEN -180 AND 180),
        open_time TEXT NOT NULL,
        close_time TEXT NOT NULL,
        restock_duration_min INTEGER NOT NULL CHECK (restock_duration_min >= 0),
        snapshot_id BIGINT NOT NULL REFERENCES reference_snapshots(snapshot_id)
    )""",
    """CREATE TABLE IF NOT EXISTS staff (
        staff_id TEXT PRIMARY KEY,
        service_center_id TEXT NOT NULL REFERENCES service_centers(service_center_id),
        profiles TEXT[] NOT NULL,
        technologies TEXT[] NOT NULL,
        height_permit BOOLEAN NOT NULL,
        shift_start TEXT NOT NULL,
        shift_end TEXT NOT NULL,
        equipment_start JSONB NOT NULL,
        equipment_capacity JSONB NOT NULL,
        snapshot_id BIGINT NOT NULL REFERENCES reference_snapshots(snapshot_id)
    )""",
    """CREATE TABLE IF NOT EXISTS days (
        date DATE PRIMARY KEY,
        now_ts TIMESTAMPTZ NOT NULL,
        loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        absent_staff_ids TEXT[] NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        date DATE NOT NULL REFERENCES days(date) ON DELETE CASCADE,
        order_id TEXT NOT NULL,
        work_type_code TEXT NOT NULL,
        technology TEXT NOT NULL,
        building_id TEXT NOT NULL,
        access_node_id TEXT NOT NULL,
        registered_at TIMESTAMPTZ NOT NULL,
        slot_start TIMESTAMPTZ NOT NULL,
        slot_end TIMESTAMPTZ NOT NULL,
        latest_start TIMESTAMPTZ NOT NULL,
        deadline TIMESTAMPTZ NOT NULL,
        is_repeat BOOLEAN NOT NULL,
        required_equipment JSONB NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('day', 'event')),
        PRIMARY KEY (date, order_id),
        CHECK (slot_end > slot_start)
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        date DATE NOT NULL REFERENCES days(date) ON DELETE CASCADE,
        event_id TEXT NOT NULL,
        type TEXT NOT NULL,
        ts TIMESTAMPTZ NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        payload JSONB NOT NULL,
        result_version INTEGER NOT NULL,
        PRIMARY KEY (date, event_id)
    )""",
    """CREATE TABLE IF NOT EXISTS plan_versions (
        plan_id BIGSERIAL PRIMARY KEY,
        date DATE NOT NULL REFERENCES days(date) ON DELETE CASCADE,
        version INTEGER NOT NULL CHECK (version >= 1),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        reason TEXT NOT NULL,
        event_id TEXT,
        as_of TIMESTAMPTZ NOT NULL,
        computation_ms INTEGER,
        plan JSONB NOT NULL,
        UNIQUE (date, version)
    )""",
    """CREATE TABLE IF NOT EXISTS plan_current (
        date DATE PRIMARY KEY REFERENCES days(date) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        plan_id BIGINT NOT NULL REFERENCES plan_versions(plan_id),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS plan_stops (
        plan_id BIGINT NOT NULL REFERENCES plan_versions(plan_id) ON DELETE CASCADE,
        staff_id TEXT NOT NULL,
        seq INTEGER NOT NULL CHECK (seq >= 1),
        type TEXT NOT NULL CHECK (type IN ('DEPOT_START', 'VISIT', 'BREAK', 'RESTOCK', 'DEPOT_END')),
        location_id TEXT NOT NULL,
        order_id TEXT,
        arrival TIMESTAMPTZ NOT NULL,
        start_ts TIMESTAMPTZ NOT NULL,
        end_ts TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (plan_id, staff_id, seq),
        CHECK (start_ts >= arrival AND end_ts >= start_ts),
        CHECK ((type = 'VISIT') = (order_id IS NOT NULL))
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS plan_stops_one_visit_per_order
        ON plan_stops (plan_id, order_id) WHERE type = 'VISIT'""",
    """CREATE INDEX IF NOT EXISTS plan_stops_order ON plan_stops (order_id)""",
    """CREATE TABLE IF NOT EXISTS plan_unassigned (
        plan_id BIGINT NOT NULL REFERENCES plan_versions(plan_id) ON DELETE CASCADE,
        order_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        reason_text TEXT NOT NULL,
        suggested_action TEXT NOT NULL,
        PRIMARY KEY (plan_id, order_id)
    )""",
    """CREATE TABLE IF NOT EXISTS route_audit (
        audit_id BIGSERIAL PRIMARY KEY,
        date DATE NOT NULL,
        version INTEGER NOT NULL,
        staff_id TEXT NOT NULL,
        added_orders TEXT[] NOT NULL,
        removed_orders TEXT[] NOT NULL,
        reason TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS route_audit_date_version ON route_audit (date, version)""",
    """CREATE INDEX IF NOT EXISTS events_date_ts ON events (date, ts)""",
    """CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
]

DROP_ALL = [
    "DROP TABLE IF EXISTS route_audit, plan_unassigned, plan_stops, plan_current, plan_versions, events, orders, "
    "days, staff, service_centers, reference_snapshots, schema_version CASCADE",
]
