from __future__ import annotations

import json
from datetime import datetime

import pytest

from ado.data.errors import BusinessValidationError, InputFormatError
from ado.data.prepare import (
    prepare_data,
    prepare_day,
    prepare_day_report,
    prepare_event,
    prepare_reference,
    prepare_scenario,
)
from ado.domain.enums import EventType, WorkTypeCode
from ado.domain.equipment import EquipmentInventory
from ado.domain.events import NewOrderEvent, StaffUnavailableEvent, VisitDelayedEvent
from tests import factories as F


@pytest.fixture
def ref():
    return prepare_reference(F.reference())


def test_valid_day(ref) -> None:
    report = prepare_day_report(F.day(), ref)
    day = report.value
    assert day.now == 8 * 3600 + 30 * 60
    o = day.orders["ORD-20261026-0001"]
    assert o.work_type_code is WorkTypeCode.REPAIR
    assert o.slot.start == 9 * 3600 and o.latest_start == 11 * 3600
    assert o.registered_at == -(5 * 3600 + 20 * 60)  # прошлый день → отрицательное время
    assert o.required_equipment == EquipmentInventory(1, 0, 0)
    inst = day.orders["ORD-20261026-0042"]
    assert inst.latest_start == inst.slot.start + 600
    assert not [w for w in report.warnings if "deadline" in w.field], report.warnings


def test_spec_day_example(spec: dict) -> None:
    ref = prepare_reference(spec["components"]["examples"]["ReferenceDataExample"]["value"])
    raw = spec["components"]["examples"]["DayInputExample"]["value"]
    with pytest.raises(BusinessValidationError, match="ST-017"):
        prepare_day(raw, ref)  # отсутствующий сотрудник неизвестен справочнику примера
    raw = {**raw, "absent_staff_ids": []}
    assert len(prepare_day(raw, ref).orders) == 2


def test_empty_orders_allowed(ref) -> None:
    assert prepare_day(F.day(orders=[]), ref).orders == {}


def test_missing_order_field_is_format_error(ref) -> None:
    o = F.order()
    del o["slot"]
    with pytest.raises(InputFormatError) as e:
        prepare_day(F.day(orders=[o]), ref)
    assert e.value.issues[0].field == "orders[0].slot"


def test_naive_datetime_rejected(ref) -> None:
    o = F.order(registered="2026-10-25T18:40:00")
    with pytest.raises(InputFormatError, match="смещение"):
        prepare_day(F.day(orders=[o]), ref)


def test_slot_end_before_start(ref) -> None:
    o = F.order(
        slot=("2026-10-26T11:00:00+05:00", "2026-10-26T09:00:00+05:00"), latest="2026-10-26T11:00:00+05:00"
    )
    with pytest.raises(BusinessValidationError, match="слота"):
        prepare_day(F.day(orders=[o]), ref)


def test_latest_start_outside_slot(ref) -> None:
    o = F.order(latest="2026-10-26T12:00:00+05:00")
    with pytest.raises(BusinessValidationError, match="latest_start"):
        prepare_day(F.day(orders=[o]), ref)


def test_slot_of_other_day(ref) -> None:
    o = F.order(
        slot=("2026-10-27T09:00:00+05:00", "2026-10-27T11:00:00+05:00"),
        latest="2026-10-27T11:00:00+05:00",
        deadline="2026-10-27T18:40:00+05:00",
    )
    with pytest.raises(BusinessValidationError, match="не относится к дню"):
        prepare_day(F.day(orders=[o]), ref)


@pytest.mark.parametrize(
    ("field", "value", "msg"),
    [("building_id", "B-99999", "дом"), ("access_node_id", "AN-9999", "узел")],
)
def test_unknown_references(ref, field, value, msg) -> None:
    o = F.order()
    o[field] = value
    with pytest.raises(BusinessValidationError, match=msg):
        prepare_day(F.day(orders=[o]), ref)


def test_unknown_absent_staff(ref) -> None:
    with pytest.raises(BusinessValidationError, match="ST-777"):
        prepare_day(F.day(absent=["ST-777"]), ref)


def test_duplicate_order(ref) -> None:
    with pytest.raises(BusinessValidationError, match="дубликат"):
        prepare_day(F.day(orders=[F.order(), F.order()]), ref)


def test_inconsistent_deadline_is_warning_and_input_wins(ref) -> None:
    o = F.order(deadline="2026-10-26T20:00:00+05:00")
    report = prepare_day_report(F.day(orders=[o]), ref)
    assert report.value.orders[o["order_id"]].deadline == 20 * 3600
    assert any(w.field == "orders[0].deadline" for w in report.warnings)


def test_other_timezone_offset_normalised(ref) -> None:
    o = F.order(
        slot=("2026-10-26T04:00:00+00:00", "2026-10-26T06:00:00+00:00"), latest="2026-10-26T06:00:00+00:00"
    )
    assert prepare_day(F.day(orders=[o]), ref).orders[o["order_id"]].slot.start == 9 * 3600


def test_reload_day_no_duplication(ref) -> None:
    a = prepare_day(F.day(), ref)
    b = prepare_day(F.day(), ref)
    assert a == b and len(b.orders) == 2


# ------------------------------------------------------------------ события и сценарий


def test_spec_event_examples(spec: dict, ref) -> None:
    day = prepare_day(F.day(), ref)
    examples = spec["components"]["examples"]
    names = [
        "NewOrderEventExample",
        "OrderCancelledEventExample",
        "VisitDelayedEventExample",
        "OrderTransferredEventExample",
        "StaffUnavailableEventExample",
        "TimeTickEventExample",
    ]
    events = [prepare_event(examples[n]["value"], day) for n in names]
    assert [e.type for e in events] == list(EventType)
    assert isinstance(events[0], NewOrderEvent) and events[0].order.is_repeat
    assert isinstance(events[2], VisitDelayedEvent) and events[2].expected_end == 10 * 3600 + 45 * 60
    assert isinstance(events[4], StaffUnavailableEvent) and events[4].until is None


def test_event_bad_type(ref) -> None:
    day = prepare_day(F.day(), ref)
    with pytest.raises(InputFormatError):
        prepare_event({"event_id": "E1", "type": "EXPLOSION", "timestamp": "2026-10-26T10:00:00+05:00"}, day)


def test_scenario_out_of_order() -> None:
    raw = {
        "date": "2026-10-26",
        "now": "2026-10-26T08:30:00+05:00",
        "events": [
            {"event_id": "E2", "type": "TIME_TICK", "timestamp": "2026-10-26T10:00:00+05:00"},
            {"event_id": "E1", "type": "TIME_TICK", "timestamp": "2026-10-26T09:00:00+05:00"},
        ],
        "triggers": [
            {
                "trigger_id": "T1",
                "type": "VISIT_DELAYED",
                "order_id": "ORD-20261026-0001",
                "fire_after_start_min": 30,
                "extra_duration_min": 40,
            }
        ],
    }
    with pytest.raises(BusinessValidationError, match="неубывания"):
        prepare_scenario(raw)
    raw["events"].reverse()
    sc = prepare_scenario(raw).value
    assert sc.triggers[0].extra_duration == 40 * 60 and sc.triggers[0].fire_after_start == 1800


# ------------------------------------------------------------------ каталог


def test_prepare_data_directory(tmp_path) -> None:
    ref = F.reference()
    for key in ("service_centers", "staff", "access_nodes"):
        (tmp_path / f"{key}.json").write_text(json.dumps(ref[key], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "work_types.csv").write_text(F.WORK_TYPES_CSV, encoding="utf-8")
    late = F.order(
        "ORD-20261026-0003",
        registered="2026-10-26T10:00:00+05:00",
        slot=("2026-10-26T13:00:00+05:00", "2026-10-26T15:00:00+05:00"),
        deadline="2026-10-27T10:00:00+05:00",
    )
    next_day = F.order(
        "ORD-20261027-0001",
        registered="2026-10-26T12:00:00+05:00",
        slot=("2026-10-27T09:00:00+05:00", "2026-10-27T11:00:00+05:00"),
        deadline="2026-10-27T12:00:00+05:00",
    )
    orders = [F.order(), F.install_order(), late, next_day]
    (tmp_path / "orders_train.json").write_text(json.dumps(orders), encoding="utf-8")
    ds = prepare_data(tmp_path)
    assert sorted(ds.days) == ["2026-10-26", "2026-10-27"]
    body, late_orders = ds.days["2026-10-26"].day_input()
    assert [o["order_id"] for o in body["orders"]] == ["ORD-20261026-0001", "ORD-20261026-0042"]
    assert [o["order_id"] for o in late_orders] == ["ORD-20261026-0003"]
    assert datetime.fromisoformat(body["now"]).hour == 8
    assert ds.scenario is None and ds.travel_matrix_raw is None
    assert len(prepare_day(body, ds.reference).orders) == 2


def test_prepare_data_missing_files(tmp_path) -> None:
    with pytest.raises(BusinessValidationError, match="нет файлов"):
        prepare_data(tmp_path)
