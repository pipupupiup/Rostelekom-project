"""ADO-11 — единая оценка и сравнение планов.

Определения (внутренние, документированы в docs/ADO_SPEC.md §5 и D7; это не официальная
формула организаторов):

* **в оценке** — наряды дня, кроме отменённых и переданных по ПГП (переданные не считаются
  выполненными — ПГП оценивается отдельно);
* **успех** — визит начат в `[slot.start, latest_start]`, завершён не позже `deadline`, на наряде
  нет нарушений H1–H10; вес повторного обращения — `repeat_weight` (2);
* **переносы** (A1) — нераспределённые наряды с `suggested_action = RESCHEDULE`;
* **время в пути** — пересчёт по матрице по всем парам соседних остановок;
* **загрузка** — `(работы + дорога) / (смена − перерыв)` для каждого сотрудника на смене;
  CV = σ/μ (генеральная σ); при μ = 0 CV = 0;
* **сравнение** — лексикографическое: допустимость → взвешенные успехи → −переносы → −дорога
  (с допуском `travel_tolerance_sec`) → −CV (допуск `cv_tolerance`). Разные единицы не смешиваются.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass

from ado.domain.contracts import TravelTimeProvider
from ado.domain.enums import StopType, UnassignedAction
from ado.domain.models import DayInstance, Plan, PlanMetrics
from ado.optimization.state import WorkingPlan
from ado.optimization.validator import ValidationIssue, validate_plan


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    repeat_weight: int = 2
    travel_tolerance_sec: int = 0
    cv_tolerance: float = 1e-9
    include_idle_staff: bool = True
    """Учитывать сотрудников без визитов в CV загрузки (их загрузка = 0)."""


def evaluate_plan(
    plan: Plan,
    day: DayInstance,
    travel: TravelTimeProvider,
    config: EvaluationConfig = EvaluationConfig(),
    issues: list[ValidationIssue] | None = None,
) -> PlanMetrics:
    if issues is None:
        issues = validate_plan(plan, day, travel)
    bad_orders = {i.order_id for i in issues if i.order_id}
    route_level_bad = {i.staff_id for i in issues if i.order_id is None and i.staff_id}

    pgp = {oid for g in plan.pgp_groups for oid in g.order_ids}
    cancelled = set(plan.cancelled)
    in_scope = [o for oid, o in day.orders.items() if oid not in pgp and oid not in cancelled]

    def w(o) -> int:
        return config.repeat_weight if o.is_repeat else 1

    weighted_total = sum(w(o) for o in in_scope)
    slot_ok = deadline_ok = success = weighted = visits = 0
    travel_total = dist_total = 0
    util: dict[str, float] = {}
    for sid, route in plan.routes.items():
        emp = day.reference.employees.get(sid)
        work = rtravel = 0
        stops = route.stops
        for i, s in enumerate(stops):
            if i > 0:
                tt = travel.travel_sec(stops[i - 1].location_id, s.location_id) or 0
                dd = travel.distance_m(stops[i - 1].location_id, s.location_id) or 0
                rtravel += tt
                dist_total += dd
            if s.type is not StopType.VISIT or s.order_id not in day.orders:
                continue
            visits += 1
            o = day.orders[s.order_id]
            work += s.end - s.start
            in_slot = o.slot.start <= s.start <= o.latest_start
            in_deadline = s.end <= o.deadline
            slot_ok += in_slot
            deadline_ok += in_deadline
            if in_slot and in_deadline and o.order_id not in bad_orders and sid not in route_level_bad:
                success += 1
                weighted += w(o)
        travel_total += rtravel
        if emp is not None and sid not in day.absent_staff_ids and (stops or config.include_idle_staff):
            avail = (emp.shift.end - emp.shift.start) - emp.shift.break_duration
            util[sid] = (work + rtravel) / avail if avail > 0 else 0.0
    values = list(util.values())
    mean = statistics.fmean(values) if values else 0.0
    cv = statistics.pstdev(values) / mean if values and mean > 0 else 0.0
    reasons = Counter(r.code.value for r in plan.unassigned.values())
    reschedules = sum(
        1 for r in plan.unassigned.values() if r.suggested_action is UnassignedAction.RESCHEDULE
    )
    return PlanMetrics(
        weighted_success=weighted,
        success_count=success,
        orders_total=len(in_scope),
        reschedules=reschedules,
        travel_sec_total=travel_total,
        utilization=util,
        utilization_cv=cv,
        hard_violations=len(issues),
        weighted_total=weighted_total,
        slot_ok_count=slot_ok,
        deadline_ok_count=deadline_ok,
        visits_planned=visits,
        unassigned_count=len(plan.unassigned),
        unassigned_by_reason=dict(reasons),
        pgp_orders=len(pgp),
        cancelled=len(cancelled),
        distance_m_total=dist_total,
    )


def compare_plans(a: PlanMetrics, b: PlanMetrics, config: EvaluationConfig = EvaluationConfig()) -> int:
    """1 — `a` лучше, −1 — хуже, 0 — равноценны. Недопустимый план никогда не лучше допустимого."""
    if a.hard_feasible != b.hard_feasible:
        return 1 if a.hard_feasible else -1
    if a.hard_violations != b.hard_violations:
        return 1 if a.hard_violations < b.hard_violations else -1
    if a.weighted_success != b.weighted_success:
        return 1 if a.weighted_success > b.weighted_success else -1
    if a.reschedules != b.reschedules:
        return 1 if a.reschedules < b.reschedules else -1
    if abs(a.travel_sec_total - b.travel_sec_total) > config.travel_tolerance_sec:
        return 1 if a.travel_sec_total < b.travel_sec_total else -1
    if abs(a.utilization_cv - b.utilization_cv) > config.cv_tolerance:
        return 1 if a.utilization_cv < b.utilization_cv else -1
    return 0


def is_better(a: PlanMetrics, b: PlanMetrics, config: EvaluationConfig = EvaluationConfig()) -> bool:
    return compare_plans(a, b, config) > 0


class PlanEvaluator:
    """Реализация протокола `PlanEvaluator`."""

    def __init__(self, config: EvaluationConfig = EvaluationConfig()) -> None:
        self.config = config

    def evaluate(self, plan: Plan, day: DayInstance, travel: TravelTimeProvider) -> PlanMetrics:
        return evaluate_plan(plan, day, travel, self.config)


def working_metrics(working: WorkingPlan, config: EvaluationConfig = EvaluationConfig()) -> PlanMetrics:
    """Быстрая оценка рабочего плана для внутренних циклов (ADO-10, ADO-13).

    Те же определения, что у `evaluate_plan`, но по расписаниям симулятора, без валидатора:
    в `RouteState` попадают только допустимые расписания, поэтому `hard_violations = 0`.
    Итоговая версия плана всё равно проходит `evaluate_plan` + валидатор.
    """
    day = working.day
    pgp = {oid for g in working.pgp_groups for oid in g.order_ids}
    in_scope = [o for oid, o in day.orders.items() if oid not in pgp and oid not in working.cancelled]
    rw = config.repeat_weight
    weighted_total = sum(rw if o.is_repeat else 1 for o in in_scope)
    weighted = success = visits = travel_total = dist = 0
    util: dict[str, float] = {}
    for sid, r in working.routes.items():
        sim = r.sim
        misses = set(sim.deadline_misses)
        for s in sim.stops:
            if s.type is StopType.VISIT and s.order_id in day.orders:
                visits += 1
                if s.order_id not in misses:
                    o = day.orders[s.order_id]
                    success += 1
                    weighted += rw if o.is_repeat else 1
        travel_total += sim.travel_sec
        dist += sim.distance_m
        e = r.employee
        avail = (e.shift.end - e.shift.start) - e.shift.break_duration
        util[sid] = (sim.work_sec + sim.travel_sec) / avail if avail > 0 else 0.0
    values = list(util.values())
    mean = statistics.fmean(values) if values else 0.0
    cv = statistics.pstdev(values) / mean if values and mean > 0 else 0.0
    assigned = {oid for r in working.routes.values() for oid in r.visit_ids()}
    missing = [o for o in in_scope if o.order_id not in assigned]
    reschedules = sum(
        1
        for o in missing
        if (r := working.unassigned.get(o.order_id)) is None
        or r.suggested_action is UnassignedAction.RESCHEDULE
    )
    return PlanMetrics(
        weighted_success=weighted,
        success_count=success,
        orders_total=len(in_scope),
        reschedules=reschedules,
        travel_sec_total=travel_total,
        utilization=util,
        utilization_cv=cv,
        hard_violations=0,
        weighted_total=weighted_total,
        visits_planned=visits,
        unassigned_count=len(missing),
        pgp_orders=len(pgp),
        cancelled=len(working.cancelled),
        distance_m_total=dist,
    )


def scalar_objective(m: PlanMetrics) -> float:
    """Скаляризация с сохранением порядка целей — только для критерия приёмки в ALNS (SA).

    Шаг успеха (10⁷) больше любого реального суммарного пути в секундах (< 10⁶), шаг переноса
    (10⁶) — тоже; CV × 10³ < 1 с пути. Публикуемый лучший план выбирается `compare_plans`.
    """
    return m.weighted_success * 1e7 - m.reschedules * 1e6 - m.travel_sec_total - m.utilization_cv * 1e3
