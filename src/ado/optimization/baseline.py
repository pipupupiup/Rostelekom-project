"""Baseline для сравнения (ADO-09 / ADO-17): последовательное назначение.

Наряды по `(slot.start, order_id)`; каждый — первому по `staff_id` допущенному сотруднику, в
маршрут которого он встаёт **в конец** без нарушений (проверка — тем же симулятором). Нет
перебора позиций, сожаления и улучшения; ПГП и причины — как в основном алгоритме, чтобы
сравнение касалось только назначения.
"""

from __future__ import annotations

import time

from ado.domain.contracts import TravelTimeProvider
from ado.domain.models import DayInstance, VisitItem
from ado.optimization.eligibility import AvailabilityState, EligibilityIndex
from ado.optimization.evaluation import evaluate_plan
from ado.optimization.initial import InitialPlanResult, diagnose_unassigned, empty_routes
from ado.optimization.pgp import PgpConfig, PgpDetector
from ado.optimization.state import WorkingPlan
from ado.optimization.validator import validate_plan


def build_baseline_plan(
    day: DayInstance, travel: TravelTimeProvider, pgp_config: PgpConfig | None = None
) -> InitialPlanResult:
    t0 = time.perf_counter()
    ref = day.reference
    working = WorkingPlan(day, travel, empty_routes(day))
    pgp = PgpDetector(pgp_config).detect(day.orders.values(), [], frozenset(), day.now, ref, day=day.date)
    working.pgp_groups = pgp.groups
    idx = EligibilityIndex(ref, travel)
    avail = AvailabilityState(absent=day.absent_staff_ids)
    sims = 0
    orders = sorted(
        (o for oid, o in day.orders.items() if oid not in pgp.grouped_order_ids),
        key=lambda o: (o.slot.start, o.order_id),
    )
    for o in orders:
        res = idx.find_eligible_staff(o, day.active_employees, avail)
        if not res.eligible:
            working.unassigned[o.order_id] = idx.explain(o, res, since=day.now)  # type: ignore[assignment]
            continue
        placed = False
        for sid in res.staff_ids:
            route = working.routes[sid]
            items = [*route.items, VisitItem(o.order_id)]
            sim = route.simulate(items, day, travel)
            sims += 1
            if sim.feasible:
                route.set_schedule(items, sim, day)
                placed = True
                break
        if not placed:
            working.unassigned[o.order_id] = diagnose_unassigned(o, res.staff_ids, working, day.now)
    plan = working.to_plan(1, day.now)
    issues = validate_plan(plan, day, travel)
    metrics = evaluate_plan(plan, day, travel, issues=issues)
    total = time.perf_counter() - t0
    plan.computation_ms = int(total * 1000)
    return InitialPlanResult(plan, working, metrics, issues, {"total_sec": total, "simulations": sims})
