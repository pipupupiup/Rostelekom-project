"""ADO-15 — хранение справочников, нарядов, событий и версий плана в PostgreSQL.

Все записи — одна транзакция на операцию; при любой ошибке — ROLLBACK, в БД ничего не остаётся.

`save_version(date, plan, expected_previous, reason, event)`:
1. `SELECT version FROM plan_current WHERE date = … FOR UPDATE` — строка текущей версии
   блокируется до конца транзакции, параллельная запись ждёт;
2. **оптимистическая блокировка**: текущая версия должна равняться `expected_previous`, а новая —
   `expected_previous + 1`; иначе `VersionConflict` — устаревшее изменение отклоняется, более
   свежее состояние не перезаписывается (в том числе «поздним» сохранением после таймаута);
3. событие пишется в `events` (PK `(date, event_id)`) — повтор даёт `DuplicateEvent`;
4. версия, остановки (уникальный индекс запрещает один наряд в двух визитах), причины
   нераспределения, аудит изменений маршрутов (добавленные/снятые наряды по сотруднику);
5. `plan_current` переключается на новую версию.

Параметры передаются как `:name`; JSON и массивы — строкой JSON с явным `CAST` в SQL, поэтому
запросы одинаково работают через SQLAlchemy/psycopg и встроенный драйвер.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ado.persistence.db import Database, DbError, Executor, IntegrityViolation
from ado.persistence.ddl import SCHEMA_VERSION, STATEMENTS


class VersionConflict(DbError):
    pass


class DuplicateEvent(DbError):
    pass


@dataclass(frozen=True, slots=True)
class SaveResult:
    plan_id: int
    version: int


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def insert_many(
    tx: Executor,
    table: str,
    columns: Sequence[str],
    casts: Mapping[str, str],
    rows: Iterable[Sequence[Any]],
    chunk: int = 400,
) -> int:
    """Пакетная вставка одним INSERT на `chunk` строк (без запросов в цикле по строкам)."""
    rows = list(rows)
    for start in range(0, len(rows), chunk):
        part = rows[start : start + chunk]
        params: dict[str, Any] = {}
        values = []
        for i, row in enumerate(part):
            ph = []
            for c, v in zip(columns, row, strict=True):
                name = f"r{i}_{c}"
                params[name] = v
                ph.append(f"CAST(:{name} AS {casts[c]})" if c in casts else f":{name}")
            values.append("(" + ", ".join(ph) + ")")
        tx.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {', '.join(values)}", params)
    return len(rows)


class PlanStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ схема
    def migrate(self) -> None:
        with self.db.transaction() as tx:
            for stmt in STATEMENTS:
                tx.execute(stmt)
            tx.execute(
                "INSERT INTO schema_version (version) VALUES (:v) ON CONFLICT DO NOTHING",
                {"v": SCHEMA_VERSION},
            )

    # ------------------------------------------------------------ справочники и день
    def save_reference(self, raw: Mapping[str, Any]) -> int:
        with self.db.transaction() as tx:
            snap = tx.execute(
                "INSERT INTO reference_snapshots (payload) VALUES (CAST(:p AS jsonb)) RETURNING snapshot_id",
                {"p": _j({k: raw[k] for k in ("service_centers", "staff", "work_types") if k in raw})},
            )
            sid = snap[0]["snapshot_id"]
            tx.execute("DELETE FROM staff")
            tx.execute("DELETE FROM service_centers")
            insert_many(
                tx,
                "service_centers",
                [
                    "service_center_id",
                    "name",
                    "lat",
                    "lon",
                    "open_time",
                    "close_time",
                    "restock_duration_min",
                    "snapshot_id",
                ],
                {},
                [
                    (
                        s["service_center_id"],
                        s["name"],
                        float(s["lat"]),
                        float(s["lon"]),
                        s["opening_hours"]["open"],
                        s["opening_hours"]["close"],
                        int(s["restock_duration_min"]),
                        sid,
                    )
                    for s in raw["service_centers"]
                ],
            )
            arr = "TEXT[]"
            insert_many(
                tx,
                "staff",
                [
                    "staff_id",
                    "service_center_id",
                    "profiles",
                    "technologies",
                    "height_permit",
                    "shift_start",
                    "shift_end",
                    "equipment_start",
                    "equipment_capacity",
                    "snapshot_id",
                ],
                {
                    "profiles": arr,
                    "technologies": arr,
                    "equipment_start": "jsonb",
                    "equipment_capacity": "jsonb",
                },
                [
                    (
                        s["staff_id"],
                        s["service_center_id"],
                        _pg_array(s["profiles"]),
                        _pg_array(s["technologies"]),
                        bool(s["height_permit"]),
                        s["shift"]["start"],
                        s["shift"]["end"],
                        _j(s["equipment_start"]),
                        _j(s["equipment_capacity"]),
                        sid,
                    )
                    for s in raw["staff"]
                ],
            )
            return int(sid)

    def save_day(
        self, date: str, now_iso: str, orders: Sequence[Mapping[str, Any]], absent: Sequence[str] = ()
    ) -> None:
        """Повторная загрузка дня удаляет его план, события и наряды (как в API: состояние сбрасывается)."""
        with self.db.transaction() as tx:
            tx.execute("DELETE FROM route_audit WHERE date = CAST(:d AS date)", {"d": date})
            tx.execute("DELETE FROM plan_current WHERE date = CAST(:d AS date)", {"d": date})
            tx.execute("DELETE FROM days WHERE date = CAST(:d AS date)", {"d": date})
            tx.execute(
                "INSERT INTO days (date, now_ts, absent_staff_ids) VALUES (CAST(:d AS date), "
                "CAST(:n AS timestamptz), CAST(:a AS TEXT[]))",
                {"d": date, "n": now_iso, "a": _pg_array(absent)},
            )
            self._insert_orders(tx, date, orders, "day")

    @staticmethod
    def _insert_orders(tx: Executor, date: str, orders: Sequence[Mapping[str, Any]], source: str) -> None:
        ts = "timestamptz"
        insert_many(
            tx,
            "orders",
            [
                "date",
                "order_id",
                "work_type_code",
                "technology",
                "building_id",
                "access_node_id",
                "registered_at",
                "slot_start",
                "slot_end",
                "latest_start",
                "deadline",
                "is_repeat",
                "required_equipment",
                "source",
            ],
            {
                "date": "date",
                "registered_at": ts,
                "slot_start": ts,
                "slot_end": ts,
                "latest_start": ts,
                "deadline": ts,
                "required_equipment": "jsonb",
            },
            [
                (
                    date,
                    o["order_id"],
                    o["work_type_code"],
                    o["technology"],
                    o["building_id"],
                    o["access_node_id"],
                    o["registered_at"],
                    o["slot"]["start"],
                    o["slot"]["end"],
                    o["latest_start"],
                    o["deadline"],
                    bool(o["is_repeat"]),
                    _j(o["required_equipment"]),
                    source,
                )
                for o in orders
            ],
        )

    # ------------------------------------------------------------ версии плана
    def save_version(
        self,
        date: str,
        plan: Mapping[str, Any],
        expected_previous: int | None,
        reason: str,
        event: Mapping[str, Any] | None = None,
    ) -> SaveResult:
        version = int(plan["plan_version"])
        with self.db.transaction() as tx:
            rows = tx.execute(
                "SELECT version, plan_id FROM plan_current WHERE date = CAST(:d AS date) FOR UPDATE",
                {"d": date},
            )
            current = rows[0]["version"] if rows else None
            prev_plan_id = rows[0]["plan_id"] if rows else None
            if expected_previous is None and current == 1 and version == 1:
                # повторное построение до первого события заменяет версию 1
                has_events = tx.execute(
                    "SELECT 1 FROM events WHERE date = CAST(:d AS date) LIMIT 1", {"d": date}
                )
                if has_events:
                    raise VersionConflict("план уже перепланировался по событиям")
                tx.execute("DELETE FROM plan_current WHERE date = CAST(:d AS date)", {"d": date})
                tx.execute(
                    "DELETE FROM plan_versions WHERE date = CAST(:d AS date) AND version = 1", {"d": date}
                )
                current, prev_plan_id = None, None
            if current != expected_previous:
                raise VersionConflict(f"текущая версия {current}, ожидалась {expected_previous}")
            if version != (expected_previous or 0) + 1:
                raise VersionConflict(f"новая версия {version} должна быть {(expected_previous or 0) + 1}")
            if event is not None:
                try:
                    tx.execute(
                        "INSERT INTO events (date, event_id, type, ts, payload, result_version) VALUES "
                        "(CAST(:d AS date), :e, :t, CAST(:ts AS timestamptz), CAST(:p AS jsonb), :v)",
                        {
                            "d": date,
                            "e": event["event_id"],
                            "t": event["type"],
                            "ts": event["timestamp"],
                            "p": _j(event),
                            "v": version,
                        },
                    )
                except IntegrityViolation as e:
                    raise DuplicateEvent(f"событие {event['event_id']} уже сохранено") from e
                if event.get("type") == "NEW_ORDER" and isinstance(event.get("order"), Mapping):
                    self._insert_orders(tx, date, [event["order"]], "event")
            created = tx.execute(
                "INSERT INTO plan_versions (date, version, reason, event_id, as_of, computation_ms, plan) VALUES "
                "(CAST(:d AS date), :v, :r, :e, CAST(:a AS timestamptz), :ms, CAST(:p AS jsonb)) RETURNING plan_id",
                {
                    "d": date,
                    "v": version,
                    "r": reason,
                    "e": event["event_id"] if event else None,
                    "a": plan["as_of"],
                    "ms": plan.get("computation_ms"),
                    "p": _j(plan),
                },
            )
            plan_id = int(created[0]["plan_id"])
            ts = "timestamptz"
            insert_many(
                tx,
                "plan_stops",
                [
                    "plan_id",
                    "staff_id",
                    "seq",
                    "type",
                    "location_id",
                    "order_id",
                    "arrival",
                    "start_ts",
                    "end_ts",
                ],
                {"arrival": ts, "start_ts": ts, "end_ts": ts},
                [
                    (
                        plan_id,
                        r["staff_id"],
                        s["seq"],
                        s["type"],
                        s["location_id"],
                        s.get("order_id"),
                        s["arrival"],
                        s["start"],
                        s["end"],
                    )
                    for r in plan["routes"]
                    for s in r["stops"]
                ],
            )
            insert_many(
                tx,
                "plan_unassigned",
                ["plan_id", "order_id", "reason_code", "reason_text", "suggested_action"],
                {},
                [
                    (plan_id, u["order_id"], u["reason_code"], u["reason_text"], u["suggested_action"])
                    for u in plan["unassigned"]
                ],
            )
            self._audit(tx, date, version, plan, prev_plan_id, reason)
            tx.execute(
                "INSERT INTO plan_current (date, version, plan_id) VALUES (CAST(:d AS date), :v, :p) "
                "ON CONFLICT (date) DO UPDATE SET version = EXCLUDED.version, plan_id = EXCLUDED.plan_id, updated_at = now()",
                {"d": date, "v": version, "p": plan_id},
            )
        return SaveResult(plan_id, version)

    def _audit(
        self,
        tx: Executor,
        date: str,
        version: int,
        plan: Mapping[str, Any],
        prev_plan_id: int | None,
        reason: str,
    ) -> None:
        new: dict[str, set[str]] = defaultdict(set)
        for r in plan["routes"]:
            new[r["staff_id"]].update(s["order_id"] for s in r["stops"] if s["type"] == "VISIT")
        old: dict[str, set[str]] = defaultdict(set)
        if prev_plan_id is not None:
            for row in tx.execute(
                "SELECT staff_id, order_id FROM plan_stops WHERE plan_id = :p AND type = 'VISIT'",
                {"p": prev_plan_id},
            ):
                old[row["staff_id"]].add(row["order_id"])
        rows = []
        for sid in sorted(set(new) | set(old)):
            added, removed = sorted(new[sid] - old[sid]), sorted(old[sid] - new[sid])
            if added or removed:
                rows.append((date, version, sid, _pg_array(added), _pg_array(removed), reason))
        insert_many(
            tx,
            "route_audit",
            ["date", "version", "staff_id", "added_orders", "removed_orders", "reason"],
            {"date": "date", "added_orders": "TEXT[]", "removed_orders": "TEXT[]"},
            rows,
        )

    # ------------------------------------------------------------ чтение
    def current(self, date: str) -> tuple[int, dict[str, Any]] | None:
        with self.db.transaction() as tx:
            rows = tx.execute(
                "SELECT c.version, v.plan FROM plan_current c JOIN plan_versions v USING (plan_id) "
                "WHERE c.date = CAST(:d AS date)",
                {"d": date},
            )
        return (rows[0]["version"], rows[0]["plan"]) if rows else None

    def version(self, date: str, version: int) -> dict[str, Any] | None:
        with self.db.transaction() as tx:
            rows = tx.execute(
                "SELECT plan FROM plan_versions WHERE date = CAST(:d AS date) AND version = :v",
                {"d": date, "v": version},
            )
        return rows[0]["plan"] if rows else None

    def versions(self, date: str) -> list[dict[str, Any]]:
        with self.db.transaction() as tx:
            return tx.execute(
                "SELECT version, reason, event_id, computation_ms FROM plan_versions "
                "WHERE date = CAST(:d AS date) ORDER BY version",
                {"d": date},
            )

    def stops(self, date: str, version: int) -> list[dict[str, Any]]:
        with self.db.transaction() as tx:
            return tx.execute(
                "SELECT s.staff_id, s.seq, s.type, s.order_id FROM plan_stops s JOIN plan_versions v "
                "USING (plan_id) WHERE v.date = CAST(:d AS date) AND v.version = :v "
                "ORDER BY s.staff_id, s.seq",
                {"d": date, "v": version},
            )

    def unassigned(self, date: str, version: int) -> list[dict[str, Any]]:
        with self.db.transaction() as tx:
            return tx.execute(
                "SELECT u.order_id, u.reason_code FROM plan_unassigned u JOIN plan_versions v "
                "USING (plan_id) WHERE v.date = CAST(:d AS date) AND v.version = :v ORDER BY u.order_id",
                {"d": date, "v": version},
            )

    def audit(self, date: str) -> list[dict[str, Any]]:
        with self.db.transaction() as tx:
            return tx.execute(
                "SELECT version, staff_id, array_to_json(added_orders) AS added, "
                "array_to_json(removed_orders) AS removed, reason FROM route_audit "
                "WHERE date = CAST(:d AS date) ORDER BY version, staff_id",
                {"d": date},
            )

    def has_event(self, date: str, event_id: str) -> bool:
        with self.db.transaction() as tx:
            return bool(
                tx.execute(
                    "SELECT 1 FROM events WHERE date = CAST(:d AS date) AND event_id = :e",
                    {"d": date, "e": event_id},
                )
            )


def _pg_array(items: Iterable[str]) -> str:
    """Литерал массива PostgreSQL `{"a","b"}` (передаётся строкой и приводится CAST(… AS TEXT[]))."""
    parts = []
    for x in items:
        s = str(x).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'"{s}"')
    return "{" + ",".join(parts) + "}"
