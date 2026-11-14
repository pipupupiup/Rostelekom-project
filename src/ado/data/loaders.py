"""Разбор входных структур организаторов в доменные объекты (ADO-01).

Схемы — строго из `spec/api_spec.yaml` (`ServiceCenter`, `Staff`, `AccessNode`, `Building`,
`WorkType`, `Order`, `DayInput`, `*Event`, `EventScenarioFile`). Неизвестные поля игнорируются
(п. 7 спецификации), в том числе `fact` обучающих нарядов.

Функции `parse_*` проверяют формат (→ `InputFormatError`), функции `check_*` — ссылки и
согласованность (→ `BusinessValidationError` или предупреждение).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from ado.data.calendar import DEFAULT_CALENDAR, ProductionCalendar, compute_deadline
from ado.data.errors import IssueCollector
from ado.data.reader import MISSING, Reader, ok
from ado.domain.enums import (
    BuildingType,
    CancelReason,
    DeadlineKind,
    EventType,
    NodeType,
    Profile,
    Technology,
    TransferTarget,
    UnavailableReason,
    WorkTypeCode,
)
from ado.domain.equipment import EquipmentInventory
from ado.domain.events import (
    AnyEvent,
    NewOrderEvent,
    OrderCancelledEvent,
    OrderTransferredEvent,
    ScenarioTrigger,
    StaffUnavailableEvent,
    TimeTickEvent,
    VisitDelayedEvent,
)
from ado.domain.models import (
    AccessNode,
    Building,
    Employee,
    Order,
    ReferenceData,
    ServiceCenter,
    Shift,
    TimeWindow,
    WorkType,
)
from ado.domain.timebase import TimeBase, minutes

SC_ID = r"SC-[0-9]{2}"
STAFF_ID = r"ST-[0-9]{3}"
BUILDING_ID = r"B-[0-9]{5}"
NODE_ID = r"AN-[0-9]{4}"
ORDER_ID = r"ORD-[0-9]{8}-[0-9]{4}"
EQUIPMENT_KEYS = ("ONT", "ROUTER", "STB")
SLOT_LENGTH_SEC = 2 * 3600


# ------------------------------------------------------------------ общие части


def parse_equipment(r: Reader, value: Any, path: str) -> Any:
    obj = r.obj(value, path)
    if obj is None:
        return MISSING
    extra = sorted(set(obj) - set(EQUIPMENT_KEYS))
    if extra:
        r.issues.format(path, f"неизвестные виды АО: {extra}")
        return MISSING
    vals = [r.integer(obj, k, path, minimum=0) for k in EQUIPMENT_KEYS]
    if not ok(*vals):
        return MISSING
    return EquipmentInventory(*vals)


def _window(r: Reader, value: Any, path: str, tb: TimeBase) -> Any:
    obj = r.obj(value, path)
    if obj is None:
        return MISSING
    start, end = r.datetime(obj, "start", path), r.datetime(obj, "end", path)
    if not ok(start, end):
        return MISSING
    return TimeWindow(tb.to_seconds(start), tb.to_seconds(end))


def _unique(items: Sequence[tuple[str, Any]], kind: str, path: str, issues: IssueCollector) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, (key, item) in enumerate(items):
        if key in out:
            issues.business(f"{path}[{i}]", f"дубликат идентификатора {kind} {key}")
            continue
        out[key] = item
    return out


# ------------------------------------------------------------------ справочники


def parse_service_centers(
    raw: Any, issues: IssueCollector, path: str = "service_centers"
) -> dict[str, ServiceCenter]:
    r = Reader(issues)
    arr = r.array(raw, path, allow_empty=False)
    if arr is None:
        return {}
    items: list[tuple[str, ServiceCenter]] = []
    for i, item in enumerate(arr):
        p = f"{path}[{i}]"
        o = r.obj(item, p)
        if o is None:
            continue
        sid = r.string(o, "service_center_id", p, pattern=SC_ID)
        name, address = r.string(o, "name", p), r.string(o, "address", p)
        lat = r.number(o, "lat", p, minimum=-90, maximum=90)
        lon = r.number(o, "lon", p, minimum=-180, maximum=180)
        hours = r.obj(o.get("opening_hours"), f"{p}.opening_hours") if "opening_hours" in o else None
        if hours is None and "opening_hours" not in o:
            issues.format(f"{p}.opening_hours", "обязательное поле отсутствует")
        open_, close = (
            (
                r.local_time(hours, "open", f"{p}.opening_hours"),
                r.local_time(hours, "close", f"{p}.opening_hours"),
            )
            if hours is not None
            else (MISSING, MISSING)
        )
        restock = r.integer(o, "restock_duration_min", p, minimum=0)
        if not ok(sid, name, address, lat, lon, open_, close, restock):
            continue
        if open_ >= close:
            issues.business(f"{p}.opening_hours", f"open {open_ // 60} мин ≥ close {close // 60} мин")
            continue
        items.append(
            (sid, ServiceCenter(sid, name, address, lat, lon, TimeWindow(open_, close), minutes(restock)))
        )
    return _unique(items, "СЦ", path, issues)


def _parse_shift(r: Reader, value: Any, path: str) -> Any:
    o = r.obj(value, path)
    if o is None:
        return MISSING
    start, end = r.local_time(o, "start", path), r.local_time(o, "end", path)
    dur = r.integer(o, "break_duration_min", path, minimum=0)
    bw = r.obj(o.get("break_window"), f"{path}.break_window") if "break_window" in o else None
    if bw is None:
        if "break_window" not in o:
            r.issues.format(f"{path}.break_window", "обязательное поле отсутствует")
        return MISSING
    bf, bt = r.local_time(bw, "from", f"{path}.break_window"), r.local_time(bw, "to", f"{path}.break_window")
    if not ok(start, end, dur, bf, bt):
        return MISSING
    return Shift(start, end, minutes(dur), TimeWindow(bf, bt))


def parse_staff(raw: Any, issues: IssueCollector, path: str = "staff") -> dict[str, Employee]:
    r = Reader(issues)
    arr = r.array(raw, path, allow_empty=False)
    if arr is None:
        return {}
    items: list[tuple[str, Employee]] = []
    for i, item in enumerate(arr):
        p = f"{path}[{i}]"
        o = r.obj(item, p)
        if o is None:
            continue
        sid = r.string(o, "staff_id", p, pattern=STAFF_ID)
        sc = r.string(o, "service_center_id", p)
        profiles = r.enum_set(o, "profiles", p, Profile)
        techs = r.enum_set(o, "technologies", p, Technology)
        height = r.boolean(o, "height_permit", p)
        shift = _parse_shift(r, o.get("shift"), f"{p}.shift") if "shift" in o else MISSING
        if "shift" not in o:
            issues.format(f"{p}.shift", "обязательное поле отсутствует")
        eq: dict[str, Any] = {}
        for key in ("equipment_start", "equipment_capacity"):
            if key in o:
                eq[key] = parse_equipment(r, o[key], f"{p}.{key}")
            else:
                issues.format(f"{p}.{key}", "обязательное поле отсутствует")
                eq[key] = MISSING
        eq_start, eq_cap = eq["equipment_start"], eq["equipment_capacity"]
        name = r.string(o, "display_name", p, required=False)
        exp = r.number(o, "experience_years", p, minimum=0, required=False)
        if not ok(sid, sc, profiles, techs, height, shift, eq_start, eq_cap, name, exp):
            continue
        items.append((sid, Employee(sid, sc, profiles, techs, height, shift, eq_start, eq_cap, name, exp)))
    return _unique(items, "сотрудника", path, issues)


def _parse_building(r: Reader, value: Any, path: str) -> Building | None:
    o = r.obj(value, path)
    if o is None:
        return None
    bid = r.string(o, "building_id", path, pattern=BUILDING_ID)
    address = r.string(o, "address", path)
    lat = r.number(o, "lat", path, minimum=-90, maximum=90)
    lon = r.number(o, "lon", path, minimum=-180, maximum=180)
    btype = r.enum(o, "building_type", path, BuildingType)
    overhead = r.boolean(o, "overhead_line", path)
    zone = r.string(o, "zone_service_center_id", path, required=False)
    if not ok(bid, address, lat, lon, btype, overhead, zone):
        return None
    return Building(bid, address, lat, lon, btype, overhead, zone)


def parse_access_nodes(
    raw: Any, issues: IssueCollector, path: str = "access_nodes"
) -> tuple[dict[str, AccessNode], dict[str, Building]]:
    r = Reader(issues)
    arr = r.array(raw, path, allow_empty=False)
    if arr is None:
        return {}, {}
    nodes: list[tuple[str, AccessNode]] = []
    buildings: dict[str, Building] = {}
    for i, item in enumerate(arr):
        p = f"{path}[{i}]"
        o = r.obj(item, p)
        if o is None:
            continue
        nid = r.string(o, "access_node_id", p, pattern=NODE_ID)
        ntype = r.enum(o, "node_type", p, NodeType)
        tech = r.enum(o, "technology", p, Technology)
        name = r.string(o, "name", p)
        lat = r.number(o, "lat", p, minimum=-90, maximum=90)
        lon = r.number(o, "lon", p, minimum=-180, maximum=180)
        address = r.string(o, "address", p, required=False)
        blist = r.array(o.get("buildings"), f"{p}.buildings", allow_empty=False) if "buildings" in o else None
        if "buildings" not in o:
            issues.format(f"{p}.buildings", "обязательное поле отсутствует")
        ids: list[str] = []
        for j, braw in enumerate(blist or []):
            bp = f"{p}.buildings[{j}]"
            b = _parse_building(r, braw, bp)
            if b is None:
                continue
            prev = buildings.get(b.building_id)
            if prev is not None and prev != b:
                issues.business(
                    bp, f"дом {b.building_id} встречается в нескольких узлах с разными атрибутами"
                )
            buildings.setdefault(b.building_id, b)
            if b.building_id not in ids:
                ids.append(b.building_id)
        if not ok(nid, ntype, tech, name, lat, lon, address) or blist is None:
            continue
        nodes.append((nid, AccessNode(nid, ntype, tech, name, lat, lon, tuple(ids), address)))
    return _unique(nodes, "узла доступа", path, issues), buildings


def parse_work_types(
    raw: Any, issues: IssueCollector, path: str = "work_types"
) -> dict[WorkTypeCode, WorkType]:
    r = Reader(issues)
    arr = r.array(raw, path, allow_empty=False)
    if arr is None:
        return {}
    items: list[tuple[str, WorkType]] = []
    for i, item in enumerate(arr):
        p = f"{path}[{i}]"
        o = r.obj(item, p)
        if o is None:
            continue
        code = r.enum(o, "code", p, WorkTypeCode)
        name = r.string(o, "name", p)
        profiles = r.enum_set(o, "profiles", p, Profile)
        dur = r.integer(o, "duration_min", p, minimum=1)
        delay = r.integer(o, "max_start_delay_min", p, minimum=0, nullable=True)
        kind = r.enum(o, "deadline_kind", p, DeadlineKind)
        dval = r.integer(o, "deadline_value", p, minimum=1, nullable=True)
        rval = r.integer(o, "repeat_deadline_value", p, minimum=1, nullable=True)
        pgp = r.boolean(o, "pgp_relevant", p)
        for key in ("max_start_delay_min", "deadline_value", "repeat_deadline_value"):
            if key not in o:
                issues.format(f"{p}.{key}", "обязательное поле отсутствует (допускается null)")
        if not ok(code, name, profiles, dur, delay, kind, dval, rval, pgp):
            continue
        if kind is not DeadlineKind.SLOT and dval is None:
            issues.business(f"{p}.deadline_value", f"для deadline_kind={kind} значение обязательно")
            continue
        wt = WorkType(
            code,
            name,
            profiles,
            minutes(dur),
            None if delay is None else minutes(delay),
            kind,
            dval,
            rval,
            pgp,
        )
        items.append((code, wt))
    result = _unique(items, "типа работ", path, issues)
    missing = [c.value for c in WorkTypeCode if c not in result]
    if result and missing:
        issues.warn(path, f"в справочнике нет типов работ: {missing}")
    return result


def read_work_types_csv(text: str) -> list[dict[str, Any]]:
    """`work_types.csv` (RFC 4180, `,`, UTF-8 без BOM) → объекты схемы `WorkType`.

    Списки — через `|`, пустое значение — `null`. Числа и boolean приводятся по схеме.
    """
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    ints = {"duration_min", "max_start_delay_min", "deadline_value", "repeat_deadline_value"}
    rows: list[dict[str, Any]] = []
    for row in reader:
        out: dict[str, Any] = {}
        for key, value in row.items():
            if key is None:
                continue
            key = key.strip()
            value = (value or "").strip()
            if value == "":
                out[key] = None
            elif key == "profiles":
                out[key] = [v.strip() for v in value.split("|") if v.strip()]
            elif key in ints:
                out[key] = int(value) if value.lstrip("-").isdigit() else value
            elif key == "pgp_relevant":
                low = value.lower()
                out[key] = True if low == "true" else False if low == "false" else value
            else:
                out[key] = value
        rows.append(out)
    return rows


def check_reference(ref: ReferenceData, issues: IssueCollector) -> None:
    """Ссылочная целостность и согласованность справочников."""
    for i, (sid, e) in enumerate(ref.employees.items()):
        p = f"staff[{i}]"
        if e.service_center_id not in ref.service_centers:
            issues.business(f"{p}.service_center_id", f"СЦ {e.service_center_id} сотрудника {sid} не найден")
        s = e.shift
        if s.start >= s.end:
            issues.business(f"{p}.shift", "начало смены не раньше её окончания")
        if s.break_window.start > s.break_window.end:
            issues.business(f"{p}.shift.break_window", "from позже to")
        if not (s.start <= s.break_window.start and s.break_window.end <= s.end):
            issues.business(f"{p}.shift.break_window", "окно перерыва выходит за пределы смены")
        if s.break_duration >= s.end - s.start:
            issues.business(f"{p}.shift.break_duration_min", "перерыв не короче смены")
        if not e.equipment_capacity.covers(e.equipment_start):
            issues.business(f"{p}.equipment_capacity", "equipment_capacity меньше equipment_start")
    for i, (bid, b) in enumerate(ref.buildings.items()):
        if b.zone_service_center_id and b.zone_service_center_id not in ref.service_centers:
            issues.warn(f"buildings[{i}]", f"зона дома {bid}: СЦ {b.zone_service_center_id} не найден")


# ------------------------------------------------------------------ наряды


def parse_order(
    raw: Any, path: str, issues: IssueCollector, tb: TimeBase
) -> tuple[Order, datetime, datetime] | None:
    """Наряд → (Order, registered_at, latest_start как datetime) или None при ошибке формата."""
    r = Reader(issues)
    o = r.obj(raw, path)
    if o is None:
        return None
    oid = r.string(o, "order_id", path, pattern=ORDER_ID)
    wt = r.enum(o, "work_type_code", path, WorkTypeCode)
    tech = r.enum(o, "technology", path, Technology)
    bid = r.string(o, "building_id", path, pattern=BUILDING_ID)
    address = r.string(o, "address", path)
    lat = r.number(o, "lat", path, minimum=-90, maximum=90)
    lon = r.number(o, "lon", path, minimum=-180, maximum=180)
    btype = r.enum(o, "building_type", path, BuildingType)
    height = r.boolean(o, "requires_height", path)
    node = r.string(o, "access_node_id", path, pattern=NODE_ID)
    reg = r.datetime(o, "registered_at", path)
    slot = _window(r, o["slot"], f"{path}.slot", tb) if "slot" in o else MISSING
    if "slot" not in o:
        issues.format(f"{path}.slot", "обязательное поле отсутствует")
    latest = r.datetime(o, "latest_start", path)
    deadline = r.datetime(o, "deadline", path)
    eq = (
        parse_equipment(r, o["required_equipment"], f"{path}.required_equipment")
        if "required_equipment" in o
        else MISSING
    )
    if "required_equipment" not in o:
        issues.format(f"{path}.required_equipment", "обязательное поле отсутствует")
    repeat = r.boolean(o, "is_repeat", path)
    prev = r.string(o, "previous_order_id", path, required=False)
    if not ok(
        oid, wt, tech, bid, address, lat, lon, btype, height, node, reg, slot, latest, deadline, eq, repeat
    ):
        return None
    order = Order(
        order_id=oid,
        work_type_code=wt,
        technology=tech,
        building_id=bid,
        address=address,
        lat=lat,
        lon=lon,
        building_type=btype,
        requires_height=height,
        access_node_id=node,
        registered_at=tb.to_seconds(reg),
        slot=slot,
        latest_start=tb.to_seconds(latest),
        deadline=tb.to_seconds(deadline),
        required_equipment=eq,
        is_repeat=repeat,
        previous_order_id=prev or None,
    )
    return order, reg, latest


def check_order(
    order: Order,
    registered_at: datetime,
    latest_start: datetime,
    path: str,
    ref: ReferenceData,
    tb: TimeBase,
    issues: IssueCollector,
    calendar: ProductionCalendar = DEFAULT_CALENDAR,
) -> bool:
    """Бизнес-проверки наряда. Возвращает False, если наряд нельзя использовать."""
    good = True
    wt = ref.work_types.get(order.work_type_code)
    if wt is None:
        issues.business(
            f"{path}.work_type_code", f"тип работ {order.work_type_code} отсутствует в справочнике"
        )
        good = False
    building = ref.buildings.get(order.building_id)
    if building is None:
        issues.business(f"{path}.building_id", f"дом {order.building_id} отсутствует в реестре узлов доступа")
        good = False
    node = ref.access_nodes.get(order.access_node_id)
    if node is None:
        issues.business(f"{path}.access_node_id", f"узел доступа {order.access_node_id} не найден")
        good = False
    s = order.slot
    if s.end <= s.start:
        issues.business(f"{path}.slot", "окончание слота не позже начала")
        good = False
    elif s.end - s.start != SLOT_LENGTH_SEC:
        issues.warn(f"{path}.slot", f"длительность слота {(s.end - s.start) // 60} мин, ожидалось 120")
    if not (0 <= s.start < 86_400):
        issues.business(f"{path}.slot.start", f"слот не относится к дню {tb.day.isoformat()}")
        good = False
    if not (s.start <= order.latest_start <= s.end):
        issues.business(f"{path}.latest_start", "latest_start вне согласованного слота")
        good = False
    if not good or wt is None or building is None or node is None:
        return False

    expected_latest = s.end if wt.max_start_delay is None else min(s.end, s.start + wt.max_start_delay)
    if expected_latest != order.latest_start:
        issues.warn(
            f"{path}.latest_start",
            f"latest_start {tb.format(order.latest_start)} не равен min(slot.end, slot.start + max_start_delay) "
            f"= {tb.format(expected_latest)}; используется значение из входа",
        )
    if order.latest_start + wt.duration > order.deadline:
        issues.warn(f"{path}.deadline", "latest_start + норматив позже контрольного срока")
    expected_deadline = compute_deadline(wt, registered_at, latest_start, order.is_repeat, calendar)
    if tb.to_seconds(expected_deadline) != order.deadline:
        issues.warn(
            f"{path}.deadline",
            f"срок {tb.format(order.deadline)} не совпадает с расчётным {expected_deadline.isoformat()}",
        )
    if order.registered_at > s.start:
        issues.warn(f"{path}.registered_at", "наряд зарегистрирован позже начала слота")
    if order.building_id not in node.building_ids:
        issues.warn(f"{path}.building_id", f"дом не входит в список домов узла {node.access_node_id}")
    if node.technology is not order.technology:
        issues.warn(
            f"{path}.technology", f"технология наряда {order.technology} ≠ технологии узла {node.technology}"
        )
    if building.building_type is not order.building_type:
        issues.warn(f"{path}.building_type", "тип дома в наряде не совпадает с реестром")
    if order.requires_height and not building.overhead_line:
        issues.warn(f"{path}.requires_height", "работы на высоте для дома без ВЛС")
    return True


def slot_date(raw_order: Mapping[str, Any]) -> date | None:
    """День наряда — местная дата `slot.start` (схема `OrdersFile`)."""
    try:
        return datetime.fromisoformat(str(raw_order["slot"]["start"]).replace("Z", "+00:00")).date()
    except (KeyError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------ события


def parse_event(raw: Any, path: str, issues: IssueCollector, tb: TimeBase) -> AnyEvent | None:
    r = Reader(issues)
    o = r.obj(raw, path)
    if o is None:
        return None
    eid = r.string(o, "event_id", path)
    etype = r.enum(o, "type", path, EventType)
    ts = r.datetime(o, "timestamp", path)
    if not ok(eid, etype, ts):
        return None
    t = tb.to_seconds(ts)
    if etype is EventType.NEW_ORDER:
        parsed = parse_order(o["order"], f"{path}.order", issues, tb) if "order" in o else None
        if "order" not in o:
            issues.format(f"{path}.order", "обязательное поле отсутствует")
        return NewOrderEvent(eid, t, parsed[0]) if parsed else None
    if etype is EventType.ORDER_CANCELLED:
        oid, reason = r.string(o, "order_id", path), r.enum(o, "reason", path, CancelReason)
        return OrderCancelledEvent(eid, t, oid, reason) if ok(oid, reason) else None
    if etype is EventType.VISIT_DELAYED:
        sid, oid, end = (
            r.string(o, "staff_id", path),
            r.string(o, "order_id", path),
            r.datetime(o, "expected_end", path),
        )
        return VisitDelayedEvent(eid, t, sid, oid, tb.to_seconds(end)) if ok(sid, oid, end) else None
    if etype is EventType.ORDER_TRANSFERRED:
        sid, oid = r.string(o, "staff_id", path), r.string(o, "order_id", path)
        end, to = r.datetime(o, "actual_end", path), r.enum(o, "transferred_to", path, TransferTarget)
        return (
            OrderTransferredEvent(eid, t, sid, oid, tb.to_seconds(end), to) if ok(sid, oid, end, to) else None
        )
    if etype is EventType.STAFF_UNAVAILABLE:
        sid, reason = r.string(o, "staff_id", path), r.enum(o, "reason", path, UnavailableReason)
        until = r.datetime(o, "until", path, required=False)
        if not ok(sid, reason, until):
            return None
        return StaffUnavailableEvent(eid, t, sid, reason, None if until is None else tb.to_seconds(until))
    return TimeTickEvent(eid, t)


def parse_trigger(raw: Any, path: str, issues: IssueCollector) -> ScenarioTrigger | None:
    r = Reader(issues)
    o = r.obj(raw, path)
    if o is None:
        return None
    tid, oid = r.string(o, "trigger_id", path), r.string(o, "order_id", path)
    ttype = r.enum(o, "type", path, EventType)
    after = r.integer(o, "fire_after_start_min", path, minimum=0)
    extra = r.integer(o, "extra_duration_min", path, required=False, minimum=1, nullable=True)
    to = None
    if o.get("transferred_to") is not None:
        to = r.enum(o, "transferred_to", path, TransferTarget)
    if not ok(tid, oid, ttype, after, extra, to):
        return None
    if ttype not in (EventType.VISIT_DELAYED, EventType.ORDER_TRANSFERRED):
        issues.format(f"{path}.type", "триггер может быть только VISIT_DELAYED или ORDER_TRANSFERRED")
        return None
    if ttype is EventType.VISIT_DELAYED and extra is None:
        issues.business(f"{path}.extra_duration_min", "для VISIT_DELAYED нужен extra_duration_min")
        return None
    return ScenarioTrigger(tid, ttype, oid, minutes(after), None if extra is None else minutes(extra), to)
