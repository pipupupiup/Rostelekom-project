"""СИНТЕТИЧЕСКИЙ генератор данных в формате организаторов (схемы `api_spec.yaml`).

Назначение — разработка, тесты и замеры производительности, пока нет файлов организаторов.
Результаты на этих данных **не являются** результатами на данных кейса. Масштаб — как в ТЗ:
5 СЦ, 40 сотрудников (28 × 3ЛТП, 12 × инсталляция), ~140 узлов, ~1260 домов, 350–450 нарядов.

Матрица времени в пути здесь синтетическая (расстояние по сфере × коэффициент извилистости,
скорость 25 км/ч, +2 мин на парковку, детерминированная асимметрия до 8 %). Это тестовые данные,
а не подмена дорожной сети: в работе сервиса используется матрица организаторов (D2).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from ado.data.calendar import DEFAULT_CALENDAR
from ado.domain.timebase import EKB_TZ

CENTER = (56.8380, 60.5970)
SCS = [
    ("SC-01", 56.8941, 60.6105),
    ("SC-02", 56.7900, 60.6300),
    ("SC-03", 56.8380, 60.5200),
    ("SC-04", 56.8300, 60.7000),
    ("SC-05", 56.9000, 60.5300),
]
TECH_NODE = {"PON": "OLT", "FTTB": "SWITCH", "XDSL": "DSLAM", "DOCSIS": "CMTS"}
WORK_TYPES = [
    # code, name, profiles, duration, delay, kind, value, repeat, pgp
    ("INST_PON_MKD", "Инсталляция ШПД/IPTV, МКД, PON", ["INSTALL"], 90, 10, "WORKING_DAYS", 3, None, False),
    ("INST_FTTB_MKD", "Инсталляция ШПД/IPTV, МКД, FTTB", ["INSTALL"], 60, 10, "WORKING_DAYS", 3, None, False),
    (
        "INST_PRIVATE_VLS",
        "Инсталляция, частный сектор, с ВЛС",
        ["INSTALL"],
        180,
        10,
        "WORKING_DAYS",
        5,
        None,
        False,
    ),
    (
        "ADD_SERVICE",
        "Доподключение услуги (IPTV, Wi-Fi-роутер)",
        ["INSTALL"],
        40,
        10,
        "SLOT",
        None,
        None,
        False,
    ),
    (
        "SURVEY_PRIVATE",
        "Обследование (частный сектор)",
        ["INSTALL", "LTP3"],
        45,
        None,
        "WORKING_DAYS",
        2,
        None,
        False,
    ),
    ("REPAIR", "Устранение неисправности (обращение абонента)", ["LTP3"], 60, None, "HOURS", 24, 12, True),
    (
        "DEGRADATION",
        "Устранение деградации, выявленной системой мониторинга качества",
        ["LTP3"],
        45,
        None,
        "HOURS",
        48,
        None,
        True,
    ),
    ("DISMANTLE", "Демонтаж АО", ["LTP3"], 30, None, "WORKING_DAYS", 5, None, False),
]
MIX = [
    ("REPAIR", 0.47),
    ("DEGRADATION", 0.14),
    ("DISMANTLE", 0.05),
    ("SURVEY_PRIVATE", 0.03),
    ("INST_PON_MKD", 0.13),
    ("INST_FTTB_MKD", 0.09),
    ("INST_PRIVATE_VLS", 0.03),
    ("ADD_SERVICE", 0.06),
]


@dataclass(slots=True)
class SyntheticDay:
    reference: dict[str, Any]
    matrix: dict[str, Any]
    day_input: dict[str, Any]
    late_orders: list[dict[str, Any]]
    pgp_incidents: list[list[str]]


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def synthetic_matrix(
    points: dict[str, tuple[float, float]], matrix_id: str = "synthetic-v1"
) -> dict[str, Any]:
    ids = sorted(points)
    durs, dists = [], []
    for i, a in enumerate(ids):
        drow, mrow = [], []
        for j, b in enumerate(ids):
            if i == j or points[a] == points[b]:
                drow.append(0)
                mrow.append(0)
                continue
            km = _haversine_km(points[a], points[b]) * 1.35
            asym = 1.0 + ((i * 7919 + j * 104729) % 9) / 100
            drow.append(int(km / 25 * 3600 * asym) + 120)
            mrow.append(int(km * 1000 * asym))
        durs.append(drow)
        dists.append(mrow)
    return {
        "matrix_id": matrix_id,
        "points": ids,
        "durations_sec": durs,
        "distances_m": dists,
        "hourly_factors": [{"hour": 8, "factor": 1.35}, {"hour": 17, "factor": 1.3}],
    }


def generate_day(
    day: date = date(2026, 10, 26),
    n_orders: int = 400,
    seed: int = 7,
    n_nodes: int = 140,
    buildings_per_node: int = 9,
    pgp_incidents: int = 2,
) -> SyntheticDay:
    rnd = random.Random(seed)
    scs = [
        {
            "service_center_id": sid,
            "name": f"СЦ {sid}",
            "address": f"г. Екатеринбург, СЦ {sid}",
            "lat": lat,
            "lon": lon,
            "opening_hours": {"open": "08:00", "close": "21:00"},
            "restock_duration_min": 15,
        }
        for sid, lat, lon in SCS
    ]

    staff = []
    for i in range(40):
        sid = f"ST-{i + 1:03d}"
        installer = i >= 28
        late = i % 3 == 2
        techs = ["PON", "FTTB"] + (["XDSL"] if i % 2 == 0 else []) + (["DOCSIS"] if i % 5 == 0 else [])
        staff.append(
            {
                "staff_id": sid,
                "display_name": ("Инсталлятор " if installer else "Монтажник ") + sid[3:],
                "service_center_id": SCS[i % 5][0],
                "profiles": ["INSTALL"] if installer else ["LTP3"],
                "technologies": techs,
                "height_permit": installer and i % 2 == 0,
                "shift": {
                    "start": "12:00" if late else "09:00",
                    "end": "21:00" if late else "18:00",
                    "break_duration_min": 60,
                    "break_window": {
                        "from": "15:00" if late else "12:00",
                        "to": "18:00" if late else "15:00",
                    },
                },
                "equipment_start": {"ONT": 3, "ROUTER": 3, "STB": 2},
                "equipment_capacity": {"ONT": 6, "ROUTER": 6, "STB": 4},
                "experience_years": round(1 + (i * 37 % 90) / 10, 1),
            }
        )

    nodes, buildings = [], {}
    bcount = 0
    for n in range(n_nodes):
        tech = rnd.choices(["PON", "FTTB", "XDSL", "DOCSIS"], [0.55, 0.3, 0.1, 0.05])[0]
        nlat = CENTER[0] + rnd.uniform(-0.09, 0.09)
        nlon = CENTER[1] + rnd.uniform(-0.17, 0.17)
        blist = []
        private = rnd.random() < 0.15
        for _ in range(buildings_per_node):
            bcount += 1
            bid = f"B-{bcount:05d}"
            b = {
                "building_id": bid,
                "address": f"г. Екатеринбург, дом {bid}",
                "lat": round(nlat + rnd.uniform(-0.006, 0.006), 6),
                "lon": round(nlon + rnd.uniform(-0.01, 0.01), 6),
                "building_type": "PRIVATE" if private else "MKD",
                "overhead_line": private,
                "zone_service_center_id": min(SCS, key=lambda s: _haversine_km((s[1], s[2]), (nlat, nlon)))[
                    0
                ],
            }
            buildings[bid] = (b, tech, f"AN-{n + 1:04d}")
            blist.append(b)
        nodes.append(
            {
                "access_node_id": f"AN-{n + 1:04d}",
                "node_type": TECH_NODE[tech],
                "technology": tech,
                "name": f"{TECH_NODE[tech]} узел {n + 1}",
                "address": "г. Екатеринбург",
                "lat": round(nlat, 6),
                "lon": round(nlon, 6),
                "buildings": blist,
            }
        )

    work_types = [
        {
            "code": c,
            "name": nm,
            "profiles": p,
            "duration_min": d,
            "max_start_delay_min": dl,
            "deadline_kind": k,
            "deadline_value": v,
            "repeat_deadline_value": r,
            "pgp_relevant": g,
        }
        for c, nm, p, d, dl, k, v, r, g in WORK_TYPES
    ]
    wt_by = {w["code"]: w for w in work_types}
    reference = {"service_centers": scs, "staff": staff, "access_nodes": nodes, "work_types": work_types}

    def tz(d: date, h: int, m: int = 0) -> datetime:
        return datetime.combine(d, time(h, m), tzinfo=EKB_TZ)

    def deadline(code: str, reg: datetime, latest: datetime, repeat: bool) -> datetime:
        w = wt_by[code]
        if w["deadline_kind"] == "SLOT":
            return latest + timedelta(minutes=w["duration_min"])
        val = w["repeat_deadline_value"] if repeat and w["repeat_deadline_value"] else w["deadline_value"]
        if w["deadline_kind"] == "HOURS":
            return reg + timedelta(hours=val)
        return DEFAULT_CALENDAR.add_working_days(reg, val)

    def make_order(
        idx: int, code: str, bid: str, reg: datetime, slot_h: int, repeat: bool
    ) -> dict[str, Any] | None:
        b, tech, node = buildings[bid]
        w = wt_by[code]
        s = tz(day, slot_h)
        latest = (
            s + timedelta(minutes=w["max_start_delay_min"])
            if w["max_start_delay_min"]
            else s + timedelta(hours=2)
        )
        need_until = latest + timedelta(minutes=w["duration_min"])
        if w["deadline_kind"] == "HOURS":  # гарантия генератора организаторов: latest + норматив ≤ deadline
            val = w["repeat_deadline_value"] if repeat and w["repeat_deadline_value"] else w["deadline_value"]
            reg = max(reg, need_until - timedelta(hours=val))
            reg = min(reg, s - timedelta(minutes=30))
        dl = deadline(code, reg, latest, repeat)
        if dl < need_until:
            return None
        eq = {"ONT": 0, "ROUTER": 0, "STB": 0}
        if code == "INST_PON_MKD":
            eq = {"ONT": 1, "ROUTER": 1, "STB": rnd.choice([0, 1])}
        elif code in ("INST_FTTB_MKD", "INST_PRIVATE_VLS"):
            eq = {"ONT": 0, "ROUTER": 1, "STB": rnd.choice([0, 1])}
        elif code == "ADD_SERVICE":
            eq = rnd.choice([{"ONT": 0, "ROUTER": 0, "STB": 1}, {"ONT": 0, "ROUTER": 1, "STB": 0}])
        elif code == "REPAIR" and rnd.random() < 0.3:
            eq = {"ONT": 1, "ROUTER": 0, "STB": 0} if tech == "PON" else {"ONT": 0, "ROUTER": 1, "STB": 0}
        return {
            "order_id": f"ORD-{day.strftime('%Y%m%d')}-{idx:04d}",
            "work_type_code": code,
            "technology": tech,
            "building_id": bid,
            "address": b["address"],
            "lat": b["lat"],
            "lon": b["lon"],
            "building_type": b["building_type"],
            "requires_height": code == "INST_PRIVATE_VLS",
            "access_node_id": node,
            "registered_at": _iso(reg),
            "slot": {"start": _iso(s), "end": _iso(s + timedelta(hours=2))},
            "latest_start": _iso(latest),
            "deadline": _iso(dl),
            "required_equipment": eq,
            "is_repeat": repeat,
            "previous_order_id": None,
        }

    private_b = [bid for bid, (b, _, _) in buildings.items() if b["overhead_line"]]
    mkd_b = [bid for bid, (b, _, _) in buildings.items() if not b["overhead_line"]]
    orders: list[dict[str, Any]] = []
    relevant_times: dict[str, list[datetime]] = {}
    codes, weights = zip(*MIX, strict=True)
    idx = 0
    while len(orders) < n_orders - 3 * pgp_incidents:
        code = rnd.choices(codes, weights)[0]
        bid = rnd.choice(private_b if code in ("INST_PRIVATE_VLS", "SURVEY_PRIVATE") and private_b else mkd_b)
        tech = buildings[bid][1]
        if code == "INST_PON_MKD" and tech != "PON":
            continue
        if code == "INST_FTTB_MKD" and tech != "FTTB":
            continue
        idx += 1
        install = wt_by[code]["profiles"] == ["INSTALL"]
        slot_h = rnd.choice([9, 11, 13, 15, 17, 19] if install else [9, 9, 11, 11, 13, 13, 15, 15, 17, 19])
        if install:
            reg = tz(day, 10) - timedelta(days=rnd.randint(1, 4), minutes=rnd.randint(0, 400))
        else:
            reg = tz(day, 8, 30) - timedelta(minutes=rnd.randint(-240, 900))
            reg = min(reg, tz(day, slot_h) - timedelta(minutes=30))
        repeat = code == "REPAIR" and rnd.random() < 0.1
        made = make_order(idx, code, bid, reg, slot_h, repeat)
        if made is None:
            continue
        if code in ("REPAIR", "DEGRADATION"):
            # гарантия организаторов: случайных совпадений не больше 2 нарядов с узла за 60 минут
            node = made["access_node_id"]
            t = datetime.fromisoformat(made["registered_at"])
            near = [x for x in relevant_times.get(node, []) if abs((x - t).total_seconds()) <= 3600]
            if len(near) >= 2:
                continue
            relevant_times.setdefault(node, []).append(t)
        orders.append(made)

    incidents = []
    for k in range(pgp_incidents):
        base = tz(day, 7, 40) + timedelta(minutes=k * 150)
        j = (k * 37 + 11) % len(nodes)
        while any(
            abs((x - base).total_seconds()) <= 2 * 3600
            for x in relevant_times.get(nodes[j]["access_node_id"], [])
        ):
            j = (j + 1) % len(nodes)  # узел без случайных обращений рядом — эталон инцидента однозначен
        node = nodes[j]
        ids = []
        for j in range(3):
            idx += 1
            bid = node["buildings"][j % len(node["buildings"])]["building_id"]
            o = make_order(idx, "REPAIR", bid, base + timedelta(minutes=15 * j), 11 + 2 * k, False)
            assert o is not None
            orders.append(o)
            ids.append(o["order_id"])
        incidents.append(ids)

    now = tz(day, 8, 30)
    early = [o for o in orders if datetime.fromisoformat(o["registered_at"]) <= now]
    late = sorted(
        (o for o in orders if datetime.fromisoformat(o["registered_at"]) > now),
        key=lambda o: (o["registered_at"], o["order_id"]),
    )
    points = {sid: (lat, lon) for sid, lat, lon in SCS}
    points.update({bid: (b["lat"], b["lon"]) for bid, (b, _, _) in buildings.items()})
    day_input = {"date": day.isoformat(), "now": _iso(now), "orders": early, "absent_staff_ids": []}
    return SyntheticDay(reference, synthetic_matrix(points), day_input, late, incidents)


def generate_history(reference: dict, days: int = 12, per_day: int = 250, seed: int = 7) -> list[dict]:
    """СИНТЕТИЧЕСКАЯ история выполнения нарядов (поле `fact`, как в `orders_train.json`) для
    демонстрации прогноза длительности (D39). Скорость сотрудника зависит от стажа
    (`1.12 − 0.035·min(стаж, 8)` от норматива: новички медленнее, опытные быстрее), разброс —
    логнормальный (σ = 0.15)."""
    import math
    import random

    rnd = random.Random(seed)
    wts = reference["work_types"]
    staff = reference["staff"]
    speed = {s["staff_id"]: 1.12 - 0.035 * min(float(s.get("experience_years") or 0), 8.0) for s in staff}
    out: list[dict] = []
    n = 0
    for d in range(days):
        for _ in range(per_day):
            wt = rnd.choice(wts)
            profs = (
                set(wt["profiles"])
                if isinstance(wt["profiles"], list)
                else set(str(wt["profiles"]).split("|"))
            )
            cand = [s for s in staff if profs & set(s["profiles"])]
            if not cand:
                continue
            st = rnd.choice(cand)
            n += 1
            norm = int(wt["duration_min"])
            outcome = "COMPLETED" if rnd.random() < 0.9 else rnd.choice(["CANCELLED", "RESCHEDULED"])
            actual = (
                round(norm * speed[st["staff_id"]] * math.exp(rnd.gauss(0, 0.15)))
                if outcome == "COMPLETED"
                else None
            )
            out.append(
                {
                    "order_id": f"HIST-{d:02d}-{n:05d}",
                    "work_type_code": wt["code"],
                    "fact": {
                        "outcome": outcome,
                        "staff_id": st["staff_id"] if outcome == "COMPLETED" else None,
                        "actual_start": None,
                        "actual_duration_min": actual,
                        "pgp_incident_id": None,
                    },
                }
            )
    return out
