"""SQLAlchemy Core-описание таблиц (для Alembic и интроспекции). Источник DDL — `ddl.py`;
соответствие колонок проверяется тестом `tests/integration/test_persistence_pg.py` по
information_schema живой БД (без SQLAlchemy тест сверяет имена колонок с этим модулем как текстом).
"""

from __future__ import annotations

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
TZ = DateTime(timezone=True)

reference_snapshots = Table(
    "reference_snapshots",
    metadata,
    Column("snapshot_id", BigInteger, primary_key=True),
    Column("loaded_at", TZ),
    Column("payload", JSONB),
)
service_centers = Table(
    "service_centers",
    metadata,
    Column("service_center_id", Text, primary_key=True),
    Column("name", Text),
    Column("lat", Float),
    Column("lon", Float),
    Column("open_time", Text),
    Column("close_time", Text),
    Column("restock_duration_min", Integer),
    Column("snapshot_id", BigInteger, ForeignKey("reference_snapshots.snapshot_id")),
)
staff = Table(
    "staff",
    metadata,
    Column("staff_id", Text, primary_key=True),
    Column("service_center_id", Text, ForeignKey("service_centers.service_center_id")),
    Column("profiles", ARRAY(Text)),
    Column("technologies", ARRAY(Text)),
    Column("height_permit", Boolean),
    Column("shift_start", Text),
    Column("shift_end", Text),
    Column("equipment_start", JSONB),
    Column("equipment_capacity", JSONB),
    Column("snapshot_id", BigInteger, ForeignKey("reference_snapshots.snapshot_id")),
)
days = Table(
    "days",
    metadata,
    Column("date", Date, primary_key=True),
    Column("now_ts", TZ),
    Column("loaded_at", TZ),
    Column("absent_staff_ids", ARRAY(Text)),
)
orders = Table(
    "orders",
    metadata,
    Column("date", Date, ForeignKey("days.date"), primary_key=True),
    Column("order_id", Text, primary_key=True),
    Column("work_type_code", Text),
    Column("technology", Text),
    Column("building_id", Text),
    Column("access_node_id", Text),
    Column("registered_at", TZ),
    Column("slot_start", TZ),
    Column("slot_end", TZ),
    Column("latest_start", TZ),
    Column("deadline", TZ),
    Column("is_repeat", Boolean),
    Column("required_equipment", JSONB),
    Column("source", Text),
)
events = Table(
    "events",
    metadata,
    Column("date", Date, ForeignKey("days.date"), primary_key=True),
    Column("event_id", Text, primary_key=True),
    Column("type", Text),
    Column("ts", TZ),
    Column("received_at", TZ),
    Column("payload", JSONB),
    Column("result_version", Integer),
)
plan_versions = Table(
    "plan_versions",
    metadata,
    Column("plan_id", BigInteger, primary_key=True),
    Column("date", Date, ForeignKey("days.date")),
    Column("version", Integer),
    Column("created_at", TZ),
    Column("reason", Text),
    Column("event_id", Text),
    Column("as_of", TZ),
    Column("computation_ms", Integer),
    Column("plan", JSONB),
)
plan_current = Table(
    "plan_current",
    metadata,
    Column("date", Date, ForeignKey("days.date"), primary_key=True),
    Column("version", Integer),
    Column("plan_id", BigInteger, ForeignKey("plan_versions.plan_id")),
    Column("updated_at", TZ),
)
plan_stops = Table(
    "plan_stops",
    metadata,
    Column("plan_id", BigInteger, ForeignKey("plan_versions.plan_id"), primary_key=True),
    Column("staff_id", Text, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("type", Text),
    Column("location_id", Text),
    Column("order_id", Text),
    Column("arrival", TZ),
    Column("start_ts", TZ),
    Column("end_ts", TZ),
)
plan_unassigned = Table(
    "plan_unassigned",
    metadata,
    Column("plan_id", BigInteger, ForeignKey("plan_versions.plan_id"), primary_key=True),
    Column("order_id", Text, primary_key=True),
    Column("reason_code", Text),
    Column("reason_text", Text),
    Column("suggested_action", Text),
)
route_audit = Table(
    "route_audit",
    metadata,
    Column("audit_id", BigInteger, primary_key=True),
    Column("date", Date),
    Column("version", Integer),
    Column("staff_id", Text),
    Column("added_orders", ARRAY(Text)),
    Column("removed_orders", ARRAY(Text)),
    Column("reason", Text),
    Column("created_at", TZ),
)
schema_version = Table(
    "schema_version", metadata, Column("version", Text, primary_key=True), Column("applied_at", TZ)
)
