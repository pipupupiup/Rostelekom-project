"""Сценарный анализ «что, если» (доп. задание, D40).

Берётся начало текущего дня (наряды, известные к `day.now`, справочники, матрица) и строится план
для базы и для изменённых условий одним и тем же быстрым алгоритмом (regret-3 + Local Search, без
ALNS, бюджет `time_limit_sec` на прогон) — сравнение «яблоки с яблоками». Активный план и история не
меняются: каждый прогон — отдельная сессия в памяти.

* `fewer_staff` — на смену не вышли `n` сотрудников (выбор случайный, `samples` повторов с разными
  seed; в ответе — среднее и каждый прогон с перечнем отсутствующих);
* `backpack` — какой запас АО нужен, чтобы не заезжать в СЦ: по маршрутам базового плана
  суммируется требуемое АО (без пополнений), рекомендация — наибольшая потребность по сотрудникам
  (не больше вместимости рюкзака), для справки — P90; затем план с таким стартовым запасом — сколько осталось заездов в СЦ;
* `traffic` — тот же день с учётом `hourly_factors` (загруженность дорог по часам, D38).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, replace
from typing import Any

from ado.domain.enums import StopType
from ado.domain.equipment import EquipmentInventory
from ado.domain.models import DayInstance
from ado.events.session import DaySession, ReoptConfig
from ado.optimization.evaluation import evaluate_plan
from ado.optimization.improvement import ImproveConfig
from ado.routing.matrix import MatrixTravelProvider, TrafficTravelProvider, TravelMatrix

KINDS = ("fewer_staff", "backpack", "traffic")


@dataclass(frozen=True, slots=True)
class ScenarioInput:
    day: DayInstance
    matrix: TravelMatrix


def start_of_day(day: DayInstance) -> DayInstance:
    """Только наряды, известные на момент построения плана (без поздних NEW_ORDER)."""
    orders = {k: o for k, o in day.orders.items() if o.registered_at <= day.now}
    return replace(day, orders=orders)


def _config(limit: float) -> ReoptConfig:
    return ReoptConfig(
        improve=ImproveConfig(use_alns=False, use_ls=True, time_limit_sec=limit), late_risk_alerts=False
    )


def _run(day: DayInstance, travel, limit: float) -> dict[str, Any]:
    t0 = time.perf_counter()
    s = DaySession(day, travel, _config(limit))
    plan = s.build_plan(time_limit_sec=limit + 5)
    m = evaluate_plan(plan, s.day, MatrixTravelProvider(travel.matrix))
    restocks = sum(1 for r in plan.routes.values() for st in r.stops if st.type is StopType.RESTOCK)
    return {
        "success": m.success_count,
        "orders_total": m.orders_total,
        "weighted_share": round(m.weighted_success_share, 4),
        "unassigned": m.unassigned_count,
        "travel_h": round(m.travel_sec_total / 3600, 1),
        "road_h": round(sum(st.travel_sec for r in plan.routes.values() for st in r.stops) / 3600, 1),
        "utilization_cv": round(m.utilization_cv, 3),
        "restocks": restocks,
        "hard_violations": m.hard_violations,
        "seconds": round(time.perf_counter() - t0, 1),
        "_plan": plan,
        "_day": s.day,
    }


def _public(r: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def run_scenario(inp: ScenarioInput, kind: str, params: dict[str, Any], limit: float = 3.0) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"неизвестный сценарий {kind!r}; доступны: {', '.join(KINDS)}")
    day = start_of_day(inp.day)
    base_travel = MatrixTravelProvider(inp.matrix)
    base = _run(day, base_travel, limit)
    out: dict[str, Any] = {"kind": kind, "orders": len(day.orders), "base": _public(base)}

    if kind == "fewer_staff":
        n = int(params.get("n", 3))
        samples = max(1, min(int(params.get("samples", 3)), 5))
        active = sorted(sid for sid in day.reference.employees if sid not in day.absent_staff_ids)
        if not 1 <= n < len(active):
            raise ValueError(f"n должно быть от 1 до {len(active) - 1}")
        runs = []
        for k in range(samples):
            off = sorted(random.Random(1000 + k).sample(active, n))
            d = replace(day, absent_staff_ids=frozenset(day.absent_staff_ids | set(off)))
            r = _public(_run(d, base_travel, limit))
            r["absent"] = off
            runs.append(r)
        avg = {
            key: round(sum(r[key] for r in runs) / len(runs), 4 if key == "weighted_share" else 1)
            for key in ("success", "weighted_share", "unassigned", "travel_h", "road_h", "restocks")
        }
        out.update(
            n=n,
            runs=runs,
            scenario=avg,
            delta={k: round(avg[k] - base[k], 4 if k == "weighted_share" else 1) for k in avg},
            conclusion=(
                f"Без {n} сотрудников в среднем выполняется {avg['success']:.0f} нарядов вместо {base['success']} "
                f"({avg['success'] - base['success']:+.0f}), нераспределённых {avg['unassigned']:.0f} "
                f"({avg['unassigned'] - base['unassigned']:+.0f})."
            ),
        )

    elif kind == "backpack":
        plan, bday = base["_plan"], base["_day"]
        need: dict[str, dict[str, int]] = {}
        for sid, r in plan.routes.items():
            tot = EquipmentInventory()
            for st in r.stops:
                if st.type is StopType.VISIT and st.order_id:
                    tot = tot + bday.orders[st.order_id].required_equipment
            if any(st.type is StopType.VISIT for st in r.stops):
                need[sid] = tot.to_api()
        emps = bday.reference.employees
        cap = {
            k: min(e.equipment_capacity.to_api()[k] for e in emps.values()) for k in ("ONT", "ROUTER", "STB")
        }
        cur = {k: max(e.equipment_start.to_api()[k] for e in emps.values()) for k in ("ONT", "ROUTER", "STB")}
        rec: dict[str, int] = {}
        for k in ("ONT", "ROUTER", "STB"):
            vals = sorted(v[k] for v in need.values()) or [0]
            rec[k] = min(cap[k], max(vals))  # чтобы не заезжать в СЦ, хватить должно всем
        short = sum(1 for v in need.values() if any(v[k] > cur[k] for k in v))
        inv = EquipmentInventory.from_api(rec)
        emps2 = {sid: replace(e, equipment_start=inv) for sid, e in emps.items()}
        d2 = replace(day, reference=replace(day.reference, employees=emps2))
        r2 = _public(_run(d2, base_travel, limit))
        out.update(
            current_start=cur,
            capacity=cap,
            recommended_start=rec,
            routes_needing_more_than_current=short,
            routes=len(need),
            p90_need={
                k: sorted(v[k] for v in need.values())[round(0.9 * (len(need) - 1))] if need else 0
                for k in ("ONT", "ROUTER", "STB")
            },
            scenario=r2,
            conclusion=(
                f"Сейчас в плане заездов в СЦ: {base['restocks']}. С запасом ONT {rec['ONT']} / роутер {rec['ROUTER']} "
                f"/ приставка {rec['STB']} (наибольшая потребность за смену, не больше вместимости рюкзака) — "
                f"{r2['restocks']}."
            ),
        )

    else:  # traffic
        if not inp.matrix.hourly_factors:
            raise ValueError("в матрице нет hourly_factors — сравнивать не с чем")
        r2 = _public(_run(day, TrafficTravelProvider(inp.matrix), limit))
        out.update(
            factors={str(h): f for h, f in sorted(inp.matrix.hourly_factors.items())},
            scenario=r2,
            conclusion=(
                f"С учётом пробок выполняется {r2['success']} нарядов вместо {base['success']}, "
                f"суммарно в дороге {r2['road_h']} ч вместо {base['road_h']} ч (с коэффициентами часа отправления)."
            ),
        )
    return out


__all__ = ["KINDS", "ScenarioInput", "run_scenario", "start_of_day"]
