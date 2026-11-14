"""СИНТЕТИЧЕСКИЕ доменные объекты для unit-тестов алгоритмов (время — секунды от полуночи).

Матрица задаётся явно словарём; точки, не указанные в `trips`, получают `default` секунд.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Mapping

from ado.data.prepare import prepare_reference
from ado.domain.enums import BuildingType, Profile, Technology, WorkTypeCode
from ado.domain.equipment import EquipmentInventory
from ado.domain.models import (
    Building,
    DayInstance,
    Employee,
    Order,
    ReferenceData,
    ServiceCenter,
    Shift,
    TimeWindow,
)
from ado.routing.matrix import UNREACHABLE, MatrixTravelProvider, TravelMatrix
from tests import factories as F

H = 3600
M = 60
_BASE_WT = prepare_reference(F.reference()).work_types


def hm(h: int, m: int = 0) -> int:
    return h * H + m * M


def employee(
    sid: str = "ST-001",
    sc: str = "SC-01",
    profiles: Iterable[Profile] = (Profile.LTP3,),
    techs: Iterable[Technology] = (Technology.PON, Technology.FTTB, Technology.XDSL, Technology.DOCSIS),
    height: bool = False,
    shift: tuple[int, int] = (hm(9), hm(18)),
    break_window: tuple[int, int] | None = None,
    break_min: int = 60,
    start: EquipmentInventory = EquipmentInventory(3, 3, 3),
    capacity: EquipmentInventory = EquipmentInventory(6, 6, 6),
) -> Employee:
    bw = break_window or (shift[0] + 3 * H, shift[0] + 6 * H)
    return Employee(
        sid,
        sc,
        frozenset(profiles),
        frozenset(techs),
        height,
        Shift(shift[0], shift[1], break_min * M, TimeWindow(*bw)),
        start,
        capacity,
    )


def installer(sid: str = "ST-101", **kw) -> Employee:
    kw.setdefault("profiles", (Profile.INSTALL,))
    return employee(sid, **kw)


def order(
    oid: str = "O1",
    wt: WorkTypeCode = WorkTypeCode.REPAIR,
    building: str = "B-00001",
    slot: tuple[int, int] = (hm(9), hm(11)),
    latest: int | None = None,
    tech: Technology = Technology.PON,
    equipment: EquipmentInventory = EquipmentInventory(0, 0, 0),
    repeat: bool = False,
    height: bool = False,
    node: str | None = None,
    registered: int = -hm(5),
    deadline: int | None = None,
) -> Order:
    wtype = _BASE_WT[wt]
    if node is None:  # по умолчанию у каждого наряда свой узел — чтобы не образовывать ПГП
        node = f"AN-{zlib.crc32(oid.encode()) % 10000:04d}"
    if latest is None:
        latest = slot[1] if wtype.max_start_delay is None else min(slot[1], slot[0] + wtype.max_start_delay)
    if deadline is None:
        deadline = latest + wtype.duration + 6 * H
    return Order(
        oid,
        wt,
        tech,
        building,
        "адрес",
        56.8,
        60.6,
        BuildingType.MKD,
        height,
        node,
        registered,
        TimeWindow(*slot),
        latest,
        deadline,
        equipment,
        repeat,
    )


def reference(
    employees: Iterable[Employee],
    buildings: Iterable[str] = (),
    scs: Mapping[str, tuple[int, int]] | None = None,
    restock_min: int = 15,
) -> ReferenceData:
    scs = scs or {"SC-01": (hm(8), hm(21))}
    return ReferenceData(
        service_centers={
            sid: ServiceCenter(sid, sid, "адрес", 56.8, 60.6, TimeWindow(*hours), restock_min * M)
            for sid, hours in scs.items()
        },
        employees={e.staff_id: e for e in employees},
        access_nodes={},
        buildings={b: Building(b, "адрес", 56.8, 60.6, BuildingType.MKD, False) for b in buildings},
        work_types=dict(_BASE_WT),
    )


def day(
    ref: ReferenceData, orders: Iterable[Order], now: int = hm(8, 30), absent: Iterable[str] = ()
) -> DayInstance:
    return DayInstance("2026-10-26", now, {o.order_id: o for o in orders}, frozenset(absent), ref)


def matrix(
    points: Iterable[str], trips: Mapping[tuple[str, str], int | None] | None = None, default: int = 20 * M
) -> MatrixTravelProvider:
    pts = list(dict.fromkeys(points))
    trips = trips or {}
    durs = []
    for a in pts:
        row = []
        for b in pts:
            if a == b:
                row.append(0)
            else:
                v = trips.get((a, b), default)
                row.append(UNREACHABLE if v is None else v)
        durs.append(row)
    dists = [[0 if v == UNREACHABLE else v * 10 for v in row] for row in durs]
    for i, row in enumerate(durs):
        for j, v in enumerate(row):
            if v == UNREACHABLE:
                dists[i][j] = UNREACHABLE
    return MatrixTravelProvider(TravelMatrix("test", pts, durs, dists))
