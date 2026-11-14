from __future__ import annotations

import pytest

from ado.data.errors import BusinessValidationError, InputFormatError
from ado.data.loaders import read_work_types_csv
from ado.data.prepare import prepare_reference, prepare_reference_report
from ado.domain.enums import DeadlineKind, Profile, WorkTypeCode
from ado.domain.equipment import EquipmentInventory
from tests import factories as F


def test_valid_reference_maps_to_domain() -> None:
    ref = prepare_reference(F.reference())
    assert set(ref.service_centers) == {"SC-01", "SC-02"}
    sc = ref.service_centers["SC-01"]
    assert sc.opening.start == 8 * 3600 and sc.opening.end == 21 * 3600
    assert sc.restock_duration == 15 * 60
    e = ref.employees["ST-029"]
    assert e.profiles == frozenset({Profile.INSTALL}) and e.height_permit
    assert e.shift.start == 12 * 3600 and e.shift.break_window.start == 15 * 3600
    assert e.shift.break_duration == 3600
    assert e.equipment_start == EquipmentInventory(3, 4, 2)
    assert set(ref.buildings) == {"B-00412", "B-00415", "B-00877", "B-01000"}
    assert ref.access_nodes["AN-0031"].building_ids == ("B-00412", "B-00415")
    assert len(ref.work_types) == 8
    rep = ref.work_types[WorkTypeCode.REPAIR]
    assert rep.duration == 3600 and rep.max_start_delay is None and rep.repeat_deadline_value == 12
    assert ref.work_types[WorkTypeCode.INST_PON_MKD].max_start_delay == 600
    assert ref.work_types[WorkTypeCode.SURVEY_PRIVATE].profiles == frozenset({Profile.INSTALL, Profile.LTP3})


def test_spec_reference_example_is_accepted(spec: dict) -> None:
    example = spec["components"]["examples"]["ReferenceDataExample"]["value"]
    report = prepare_reference_report(example)
    assert set(report.value.employees) == {"ST-001", "ST-029"}
    # в примере только 2 типа работ — это предупреждение, а не ошибка
    assert any("нет типов работ" in w.message for w in report.warnings)


@pytest.mark.parametrize("key", ["service_centers", "staff", "access_nodes", "work_types"])
def test_empty_reference_lists_are_format_errors(key: str) -> None:
    with pytest.raises(InputFormatError) as e:
        prepare_reference(F.reference(**{key: []}))
    assert e.value.issues[0].field == key


def test_missing_required_fields_report_paths() -> None:
    raw = F.reference()
    del raw["staff"][0]["height_permit"]
    del raw["service_centers"][1]["lat"]
    del raw["access_nodes"][0]["buildings"][1]["overhead_line"]
    with pytest.raises(InputFormatError) as e:
        prepare_reference(raw)
    fields = {i.field for i in e.value.issues}
    assert {
        "staff[0].height_permit",
        "service_centers[1].lat",
        "access_nodes[0].buildings[1].overhead_line",
    } <= fields


@pytest.mark.parametrize(("lat", "lon"), [(91, 60), (-91, 60), (56, 181), (56, -181), ("56", 60), (None, 60)])
def test_invalid_coordinates(lat, lon) -> None:
    raw = F.reference()
    raw["service_centers"][0]["lat"], raw["service_centers"][0]["lon"] = lat, lon
    with pytest.raises(InputFormatError):
        prepare_reference(raw)


def test_wrong_enum_and_id_pattern() -> None:
    raw = F.reference()
    raw["staff"][0]["technologies"] = ["PON", "GPON"]
    raw["staff"][1]["staff_id"] = "ST-29"
    with pytest.raises(InputFormatError) as e:
        prepare_reference(raw)
    msgs = " ".join(str(i) for i in e.value.issues)
    assert "GPON" in msgs and "ST-29" in msgs


def test_negative_equipment_and_unknown_kind() -> None:
    raw = F.reference()
    raw["staff"][0]["equipment_start"] = {"ONT": -1, "ROUTER": 0, "STB": 0}
    raw["staff"][1]["equipment_capacity"] = {"ONT": 1, "ROUTER": 1, "STB": 1, "MODEM": 1}
    with pytest.raises(InputFormatError) as e:
        prepare_reference(raw)
    assert len(e.value.issues) == 2


def test_unknown_service_center_is_business_error() -> None:
    raw = F.reference()
    raw["staff"][0]["service_center_id"] = "SC-99"
    with pytest.raises(BusinessValidationError) as e:
        prepare_reference(raw)
    assert e.value.issues[0].field == "staff[0].service_center_id"


def test_capacity_below_start_and_bad_shift() -> None:
    raw = F.reference()
    raw["staff"][0]["equipment_capacity"] = F.eq(1, 1, 1)
    raw["staff"][1]["shift"]["break_window"] = {"from": "10:00", "to": "22:00"}
    with pytest.raises(BusinessValidationError) as e:
        prepare_reference(raw)
    fields = {i.field for i in e.value.issues}
    assert {"staff[0].equipment_capacity", "staff[1].shift.break_window"} <= fields


def test_duplicate_ids_are_rejected_not_overwritten() -> None:
    raw = F.reference()
    raw["staff"].append(F.staff("ST-001"))
    with pytest.raises(BusinessValidationError, match="дубликат"):
        prepare_reference(raw)


def test_building_in_two_nodes_must_be_identical() -> None:
    raw = F.reference()
    raw["access_nodes"][1]["buildings"].append(F.building("B-00412", 56.8712, 60.6531))
    assert "B-00412" in prepare_reference(raw).buildings  # одинаковые атрибуты — допустимо
    raw["access_nodes"][1]["buildings"][-1]["overhead_line"] = True
    with pytest.raises(BusinessValidationError, match="разными атрибутами"):
        prepare_reference(raw)


def test_reload_is_idempotent_and_independent() -> None:
    raw = F.reference()
    a, b = prepare_reference(raw), prepare_reference(raw)
    assert a == b and a is not b
    assert a.employees is not b.employees


def test_work_types_csv_parsing() -> None:
    rows = read_work_types_csv("﻿" + F.WORK_TYPES_CSV)
    survey = next(r for r in rows if r["code"] == "SURVEY_PRIVATE")
    assert survey["profiles"] == ["INSTALL", "LTP3"]
    assert survey["max_start_delay_min"] is None
    assert survey["pgp_relevant"] is False
    add = next(r for r in rows if r["code"] == "ADD_SERVICE")
    assert add["deadline_kind"] == "SLOT" and add["deadline_value"] is None


def test_non_slot_deadline_requires_value() -> None:
    raw = F.reference()
    raw["work_types"][5]["deadline_value"] = None  # REPAIR
    with pytest.raises(BusinessValidationError, match="deadline_value"):
        prepare_reference(raw)


def test_deadline_kind_enum() -> None:
    ref = prepare_reference(F.reference())
    assert ref.work_types[WorkTypeCode.ADD_SERVICE].deadline_kind is DeadlineKind.SLOT
