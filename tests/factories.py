"""СИНТЕТИЧЕСКИЕ тестовые данные в формате организаторов (raw JSON по схемам api_spec.yaml).

Не являются данными организаторов. Нормативы и сроки — из таблицы ТЗ (п. 4).
"""

from __future__ import annotations

import copy
from typing import Any

WORK_TYPES_CSV = """code,name,profiles,duration_min,max_start_delay_min,deadline_kind,deadline_value,repeat_deadline_value,pgp_relevant
INST_PON_MKD,"Инсталляция ШПД/IPTV, МКД, PON",INSTALL,90,10,WORKING_DAYS,3,,false
INST_FTTB_MKD,"Инсталляция ШПД/IPTV, МКД, FTTB",INSTALL,60,10,WORKING_DAYS,3,,false
INST_PRIVATE_VLS,"Инсталляция, частный сектор, с ВЛС",INSTALL,180,10,WORKING_DAYS,5,,false
ADD_SERVICE,Доподключение услуги (IPTV/Wi-Fi-роутер),INSTALL,40,10,SLOT,,,false
SURVEY_PRIVATE,Обследование (частный сектор),INSTALL|LTP3,45,,WORKING_DAYS,2,,false
REPAIR,Устранение неисправности (обращение абонента),LTP3,60,,HOURS,24,12,true
DEGRADATION,"Устранение деградации, выявленной системой мониторинга качества",LTP3,45,,HOURS,48,,true
DISMANTLE,Демонтаж АО,LTP3,30,,WORKING_DAYS,5,,false
"""


def eq(ont: int = 0, router: int = 0, stb: int = 0) -> dict[str, int]:
    return {"ONT": ont, "ROUTER": router, "STB": stb}


def service_center(sid: str = "SC-01", lat: float = 56.8941, lon: float = 60.6105) -> dict[str, Any]:
    return {
        "service_center_id": sid,
        "name": f"СЦ {sid}",
        "address": "г. Екатеринбург, ул. Победы, 30",
        "lat": lat,
        "lon": lon,
        "opening_hours": {"open": "08:00", "close": "21:00"},
        "restock_duration_min": 15,
    }


def staff(
    sid: str = "ST-001",
    sc: str = "SC-01",
    profiles: list[str] | None = None,
    technologies: list[str] | None = None,
    height: bool = False,
    shift: tuple[str, str, str, str] = ("09:00", "18:00", "12:00", "15:00"),
    start: dict[str, int] | None = None,
    capacity: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "staff_id": sid,
        "display_name": f"Сотрудник {sid}",
        "service_center_id": sc,
        "profiles": profiles or ["LTP3"],
        "technologies": technologies or ["PON", "FTTB", "XDSL"],
        "height_permit": height,
        "shift": {
            "start": shift[0],
            "end": shift[1],
            "break_duration_min": 60,
            "break_window": {"from": shift[2], "to": shift[3]},
        },
        "equipment_start": start or eq(3, 4, 2),
        "equipment_capacity": capacity or eq(6, 6, 4),
    }


def building(bid: str, lat: float, lon: float, btype: str = "MKD", overhead: bool = False) -> dict[str, Any]:
    return {
        "building_id": bid,
        "address": f"г. Екатеринбург, дом {bid}",
        "lat": lat,
        "lon": lon,
        "building_type": btype,
        "overhead_line": overhead,
        "zone_service_center_id": "SC-01",
    }


def access_node(nid: str, tech: str, buildings: list[dict[str, Any]]) -> dict[str, Any]:
    ntype = {"PON": "OLT", "FTTB": "SWITCH", "XDSL": "DSLAM", "DOCSIS": "CMTS"}[tech]
    return {
        "access_node_id": nid,
        "node_type": ntype,
        "technology": tech,
        "name": f"{ntype} {nid}",
        "lat": 56.87,
        "lon": 60.65,
        "buildings": buildings,
    }


def work_types_rows() -> list[dict[str, Any]]:
    from ado.data.loaders import read_work_types_csv

    return read_work_types_csv(WORK_TYPES_CSV)


def reference(**overrides: Any) -> dict[str, Any]:
    ref = {
        "service_centers": [service_center("SC-01"), service_center("SC-02", 56.80, 60.60)],
        "staff": [
            staff("ST-001"),
            staff(
                "ST-029",
                profiles=["INSTALL"],
                technologies=["PON", "FTTB"],
                height=True,
                shift=("12:00", "21:00", "15:00", "18:00"),
            ),
        ],
        "access_nodes": [
            access_node(
                "AN-0031",
                "PON",
                [building("B-00412", 56.8712, 60.6531), building("B-00415", 56.8655, 60.6493)],
            ),
            access_node("AN-0102", "FTTB", [building("B-00877", 56.9052, 60.5874)]),
            access_node("AN-0200", "PON", [building("B-01000", 56.95, 60.70, "PRIVATE", True)]),
        ],
        "work_types": work_types_rows(),
    }
    ref.update(overrides)
    return copy.deepcopy(ref)


def order(
    oid: str = "ORD-20261026-0001",
    wt: str = "REPAIR",
    tech: str = "PON",
    bid: str = "B-00412",
    node: str = "AN-0031",
    registered: str = "2026-10-25T18:40:00+05:00",
    slot: tuple[str, str] = ("2026-10-26T09:00:00+05:00", "2026-10-26T11:00:00+05:00"),
    latest: str | None = None,
    deadline: str = "2026-10-26T18:40:00+05:00",
    equipment: dict[str, int] | None = None,
    repeat: bool = False,
    btype: str = "MKD",
    height: bool = False,
) -> dict[str, Any]:
    return {
        "order_id": oid,
        "work_type_code": wt,
        "technology": tech,
        "building_id": bid,
        "address": f"г. Екатеринбург, дом {bid}",
        "lat": 56.8712,
        "lon": 60.6531,
        "building_type": btype,
        "requires_height": height,
        "access_node_id": node,
        "registered_at": registered,
        "slot": {"start": slot[0], "end": slot[1]},
        "latest_start": latest or slot[1],
        "deadline": deadline,
        "required_equipment": equipment or eq(1, 0, 0),
        "is_repeat": repeat,
        "previous_order_id": None,
        "fact": {"outcome": "COMPLETED"},  # поле обучающего набора — должно игнорироваться
    }


def install_order(oid: str = "ORD-20261026-0042") -> dict[str, Any]:
    return order(
        oid,
        "INST_PON_MKD",
        bid="B-00415",
        registered="2026-10-22T14:12:00+05:00",
        slot=("2026-10-26T13:00:00+05:00", "2026-10-26T15:00:00+05:00"),
        latest="2026-10-26T13:10:00+05:00",
        deadline="2026-10-27T14:12:00+05:00",
        equipment=eq(1, 1, 1),
    )


def day(orders: list[dict[str, Any]] | None = None, absent: list[str] | None = None) -> dict[str, Any]:
    return {
        "date": "2026-10-26",
        "now": "2026-10-26T08:30:00+05:00",
        "orders": [order(), install_order()] if orders is None else orders,
        "absent_staff_ids": absent or [],
    }
