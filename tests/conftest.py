"""Общие фикстуры.

ВНИМАНИЕ: объекты ниже — СИНТЕТИЧЕСКИЕ тестовые данные (значения взяты из примеров
`spec/api_spec.yaml`). Это не данные организаторов и не результат работы алгоритма.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from ado.domain.enums import BuildingType, Profile, Technology, WorkTypeCode
from ado.domain.equipment import EquipmentInventory
from ado.domain.models import Employee, Order, Shift, TimeWindow
from ado.domain.timebase import TimeBase, parse_local_time

SPEC_PATH = Path(__file__).resolve().parents[1] / "spec" / "api_spec.yaml"


@pytest.fixture(scope="session")
def spec() -> dict:
    with SPEC_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def tb() -> TimeBase:
    return TimeBase(date(2026, 10, 26))


@pytest.fixture
def synthetic_employee() -> Employee:
    return Employee(
        staff_id="ST-001",
        service_center_id="SC-01",
        profiles=frozenset({Profile.LTP3}),
        technologies=frozenset({Technology.PON, Technology.FTTB, Technology.XDSL}),
        height_permit=False,
        shift=Shift(
            start=parse_local_time("09:00"),
            end=parse_local_time("18:00"),
            break_duration=3600,
            break_window=TimeWindow(parse_local_time("12:00"), parse_local_time("15:00")),
        ),
        equipment_start=EquipmentInventory(3, 4, 2),
        equipment_capacity=EquipmentInventory(6, 6, 4),
    )


@pytest.fixture
def synthetic_order() -> Order:
    return Order(
        order_id="ORD-20261026-0001",
        work_type_code=WorkTypeCode.REPAIR,
        technology=Technology.PON,
        building_id="B-00412",
        address="г. Екатеринбург, ул. Учителей, 18",
        lat=56.8712,
        lon=60.6531,
        building_type=BuildingType.MKD,
        requires_height=False,
        access_node_id="AN-0031",
        registered_at=-(5 * 3600 + 20 * 60),
        slot=TimeWindow(9 * 3600, 11 * 3600),
        latest_start=11 * 3600,
        deadline=18 * 3600 + 40 * 60,
        required_equipment=EquipmentInventory(1, 0, 0),
        is_repeat=False,
    )
