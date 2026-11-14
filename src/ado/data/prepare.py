"""Чистый интерфейс подготовки данных для остальных модулей (ADO-01).

* `prepare_reference(raw)` — тело `PUT /reference` или собранные файлы справочников;
* `prepare_day(raw, reference)` — тело `PUT /days/{date}` (`DayInput`);
* `prepare_event(raw, day)` — тело `POST /events` (только формат; ссылки проверяет ADO-13);
* `prepare_scenario(raw)` — `events_example.json`;
* `prepare_data(path)` — каталог с файлами организаторов → `Dataset` (для бенчмарков и отладки).

Функции чистые: повторный вызов с теми же данными даёт равные, независимые объекты; дубликаты
идентификаторов во входе — ошибка, а не тихая перезапись (п. 9 задания).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Generic, TypeVar

from ado.data.calendar import DEFAULT_CALENDAR, ProductionCalendar
from ado.data.errors import BusinessValidationError, Issue, IssueCollector
from ado.data.loaders import (
    check_order,
    check_reference,
    parse_access_nodes,
    parse_event,
    parse_order,
    parse_service_centers,
    parse_staff,
    parse_trigger,
    parse_work_types,
    read_work_types_csv,
    slot_date,
)
from ado.data.reader import Reader, ok
from ado.domain.events import AnyEvent, NewOrderEvent, ScenarioTrigger
from ado.domain.models import DayInstance, Order, ReferenceData
from ado.domain.timebase import EKB_TZ, TimeBase

T = TypeVar("T")

FILE_NAMES = {
    "service_centers": "service_centers.json",
    "staff": "staff.json",
    "access_nodes": "access_nodes.json",
    "work_types": "work_types.csv",
    "orders": "orders_train.json",
    "events": "events_example.json",
    "travel_matrix": "travel_matrix.json",
}


@dataclass(frozen=True, slots=True)
class Prepared(Generic[T]):
    value: T
    warnings: tuple[Issue, ...] = ()


# ------------------------------------------------------------------ справочники


def prepare_reference_report(raw: Mapping[str, Any]) -> Prepared[ReferenceData]:
    issues = IssueCollector()
    r = Reader(issues)
    obj = r.obj(raw, "")
    if obj is None:
        issues.raise_all()
    for key in ("service_centers", "staff", "access_nodes", "work_types"):
        if key not in obj:
            issues.format(key, "обязательное поле отсутствует")
    issues.raise_format()
    scs = parse_service_centers(obj["service_centers"], issues)
    staff = parse_staff(obj["staff"], issues)
    nodes, buildings = parse_access_nodes(obj["access_nodes"], issues)
    work_types = parse_work_types(obj["work_types"], issues)
    ref = ReferenceData(scs, staff, nodes, buildings, work_types)
    issues.raise_format()
    check_reference(ref, issues)
    issues.raise_all()
    return Prepared(ref, tuple(issues.warnings))


def prepare_reference(raw: Mapping[str, Any]) -> ReferenceData:
    return prepare_reference_report(raw).value


# ------------------------------------------------------------------ день


def prepare_day_report(
    raw: Mapping[str, Any], reference: ReferenceData, calendar: ProductionCalendar = DEFAULT_CALENDAR
) -> Prepared[DayInstance]:
    issues = IssueCollector()
    r = Reader(issues)
    obj = r.obj(raw, "")
    if obj is None:
        issues.raise_all()
    day_str = r.string(obj, "date", "", pattern=r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
    now = r.datetime(obj, "now", "")
    orders_raw = r.array(obj.get("orders"), "orders") if "orders" in obj else None
    if "orders" not in obj:
        issues.format("orders", "обязательное поле отсутствует")
    absent_raw = obj.get("absent_staff_ids", [])
    absent = r.array(absent_raw, "absent_staff_ids") if absent_raw is not None else []
    if not ok(day_str, now) or orders_raw is None or absent is None:
        issues.raise_all()
    try:
        day = date.fromisoformat(day_str)
    except ValueError:
        issues.format("date", f"некорректная дата {day_str!r}")
        issues.raise_all()
    tb = TimeBase(day, EKB_TZ)
    now_sec = tb.to_seconds(now)
    if now.astimezone(EKB_TZ).date() != day:
        issues.warn("now", f"модельное время {now.isoformat()} не относится к дню {day_str}")

    parsed: list[tuple[int, Order, datetime, datetime]] = []
    for i, raw_order in enumerate(orders_raw):
        res = parse_order(raw_order, f"orders[{i}]", issues, tb)
        if res is not None:
            parsed.append((i, *res))
    absent_ids: list[str] = []
    for i, sid in enumerate(absent):
        if not isinstance(sid, str):
            issues.format(f"absent_staff_ids[{i}]", "ожидается строка")
        elif sid in absent_ids:
            issues.format(f"absent_staff_ids[{i}]", "значения должны быть уникальными")
        else:
            absent_ids.append(sid)
    issues.raise_format()

    orders: dict[str, Order] = {}
    for i, order, reg, latest in parsed:
        path = f"orders[{i}]"
        if order.order_id in orders:
            issues.business(f"{path}.order_id", f"дубликат наряда {order.order_id}")
            continue
        if check_order(order, reg, latest, path, reference, tb, issues, calendar):
            orders[order.order_id] = order
        if order.registered_at > now_sec:
            issues.warn(
                f"{path}.registered_at", "наряд зарегистрирован позже now; ожидается событием NEW_ORDER"
            )
    for i, sid in enumerate(absent_ids):
        if sid not in reference.employees:
            issues.business(f"absent_staff_ids[{i}]", f"сотрудник {sid} не найден в справочнике")
    issues.raise_all()
    instance = DayInstance(day_str, now_sec, orders, frozenset(absent_ids), reference)
    return Prepared(instance, tuple(issues.warnings))


def prepare_day(
    raw: Mapping[str, Any], reference: ReferenceData, calendar: ProductionCalendar = DEFAULT_CALENDAR
) -> DayInstance:
    return prepare_day_report(raw, reference, calendar).value


def timebase_of(day: DayInstance) -> TimeBase:
    return TimeBase(date.fromisoformat(day.date), EKB_TZ)


# ------------------------------------------------------------------ события


def prepare_event(
    raw: Mapping[str, Any], day: DayInstance, calendar: ProductionCalendar = DEFAULT_CALENDAR
) -> AnyEvent:
    """Формат события; для `NEW_ORDER` дополнительно — бизнес-проверки наряда по справочникам."""
    issues = IssueCollector()
    tb = timebase_of(day)
    event = parse_event(raw, "", issues, tb)
    issues.raise_format()
    assert event is not None
    if isinstance(event, NewOrderEvent):
        o = event.order
        reg = tb.to_datetime(o.registered_at)
        latest = tb.to_datetime(o.latest_start)
        check_order(o, reg, latest, "order", day.reference, tb, issues, calendar)
    issues.raise_all()
    return event


# ------------------------------------------------------------------ сценарий


@dataclass(frozen=True, slots=True)
class Scenario:
    date: str
    now: int
    events: tuple[AnyEvent, ...]
    triggers: tuple[ScenarioTrigger, ...]


def prepare_scenario(raw: Mapping[str, Any]) -> Prepared[Scenario]:
    issues = IssueCollector()
    r = Reader(issues)
    obj = r.obj(raw, "")
    if obj is None:
        issues.raise_all()
    day_str = r.string(obj, "date", "", pattern=r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
    now = r.datetime(obj, "now", "")
    events_raw = r.array(obj.get("events"), "events") if "events" in obj else None
    triggers_raw = r.array(obj.get("triggers"), "triggers") if "triggers" in obj else None
    for key in ("events", "triggers"):
        if key not in obj:
            issues.format(key, "обязательное поле отсутствует")
    if not ok(day_str, now) or events_raw is None or triggers_raw is None:
        issues.raise_all()
    tb = TimeBase(date.fromisoformat(day_str), EKB_TZ)
    events = [
        e for i, raw_e in enumerate(events_raw) if (e := parse_event(raw_e, f"events[{i}]", issues, tb))
    ]
    triggers = [
        t for i, raw_t in enumerate(triggers_raw) if (t := parse_trigger(raw_t, f"triggers[{i}]", issues))
    ]
    seen: set[str] = set()
    for i, e in enumerate(events):
        if e.event_id in seen:
            issues.warn(f"events[{i}].event_id", f"повтор события {e.event_id} (будет идемпотентным)")
        seen.add(e.event_id)
    for i in range(1, len(events)):
        if events[i].timestamp < events[i - 1].timestamp:
            issues.business(f"events[{i}].timestamp", "события должны идти в порядке неубывания timestamp")
    issues.raise_all()
    return Prepared(
        Scenario(day_str, tb.to_seconds(now), tuple(events), tuple(triggers)), tuple(issues.warnings)
    )


# ------------------------------------------------------------------ каталог файлов


@dataclass(slots=True)
class TrainingDay:
    """Все наряды одного обучающего дня (день = дата `slot.start`)."""

    date: str
    orders_raw: list[dict[str, Any]]

    def day_input(self, now: time = time(8, 30)) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Тело `PUT /days/{date}` на момент `now` + наряды, которые придут событиями NEW_ORDER.

        Так же разделяет наряды оркестратор (схема `DayInput.orders`).
        """
        now_dt = datetime.combine(date.fromisoformat(self.date), now, tzinfo=EKB_TZ)
        early, late = [], []
        for o in self.orders_raw:
            reg = datetime.fromisoformat(str(o["registered_at"]).replace("Z", "+00:00"))
            (early if reg <= now_dt else late).append(o)
        late.sort(key=lambda o: (o["registered_at"], o["order_id"]))
        body = {"date": self.date, "now": now_dt.isoformat(timespec="seconds"), "orders": early}
        return body, late


@dataclass(slots=True)
class Dataset:
    reference: ReferenceData
    reference_raw: dict[str, Any]
    days: dict[str, TrainingDay]
    scenario: Scenario | None
    travel_matrix_raw: dict[str, Any] | None
    warnings: list[Issue] = field(default_factory=list)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_reference_files(data_dir: Path) -> dict[str, Any]:
    """Файлы справочников → тело `PUT /reference` (как его собирает оркестратор)."""
    missing = [
        FILE_NAMES[k]
        for k in ("service_centers", "staff", "access_nodes", "work_types")
        if not (data_dir / FILE_NAMES[k]).exists()
    ]
    if missing:
        raise BusinessValidationError([Issue(str(data_dir), f"нет файлов: {missing}")])
    return {
        "service_centers": _read_json(data_dir / FILE_NAMES["service_centers"]),
        "staff": _read_json(data_dir / FILE_NAMES["staff"]),
        "access_nodes": _read_json(data_dir / FILE_NAMES["access_nodes"]),
        "work_types": read_work_types_csv((data_dir / FILE_NAMES["work_types"]).read_text(encoding="utf-8")),
    }


def prepare_data(data_dir: str | Path) -> Dataset:
    """Каталог с файлами организаторов → проверенный `Dataset`.

    Наряды делятся по дням по дате `slot.start`; каждый день проверяется отдельно (как при
    `PUT /days/{date}`) со всеми нарядами дня. Отсутствие `events_example.json` или
    `travel_matrix.json` допустимо.
    """
    data_dir = Path(data_dir)
    ref_raw = load_reference_files(data_dir)
    ref_prepared = prepare_reference_report(ref_raw)
    warnings = list(ref_prepared.warnings)

    days: dict[str, TrainingDay] = {}
    orders_path = data_dir / FILE_NAMES["orders"]
    if orders_path.exists():
        orders = _read_json(orders_path)
        if not isinstance(orders, list):
            raise BusinessValidationError(
                [Issue(FILE_NAMES["orders"], "корневой элемент должен быть массивом")]
            )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for i, o in enumerate(orders):
            d = slot_date(o) if isinstance(o, dict) else None
            if d is None:
                warnings.append(Issue(f"orders[{i}]", "не удалось определить день наряда по slot.start"))
                continue
            grouped[d.isoformat()].append(o)
        for d in sorted(grouped):
            day = TrainingDay(d, grouped[d])
            body = {"date": d, "now": f"{d}T00:00:00+05:00", "orders": day.orders_raw}
            checked = prepare_day_report(body, ref_prepared.value)
            warnings.extend(
                Issue(f"{d}:{w.field}", w.message) for w in checked.warnings if "позже now" not in w.message
            )
            days[d] = day

    scenario = None
    events_path = data_dir / FILE_NAMES["events"]
    if events_path.exists():
        prepared = prepare_scenario(_read_json(events_path))
        scenario = prepared.value
        warnings.extend(prepared.warnings)

    matrix_path = data_dir / FILE_NAMES["travel_matrix"]
    matrix_raw = _read_json(matrix_path) if matrix_path.exists() else None
    return Dataset(ref_prepared.value, ref_raw, days, scenario, matrix_raw, warnings)


class DataPreparer:
    """Реализация протокола `ado.domain.contracts.DataPreparer`."""

    def __init__(self, calendar: ProductionCalendar = DEFAULT_CALENDAR) -> None:
        self.calendar = calendar

    def prepare_reference(self, raw: Mapping[str, Any]) -> ReferenceData:
        return prepare_reference(raw)

    def prepare_day(self, raw: Mapping[str, Any], reference: ReferenceData) -> DayInstance:
        return prepare_day(raw, reference, self.calendar)
