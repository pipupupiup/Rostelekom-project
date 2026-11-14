"""ADO-09 — первоначальное построение плана: Regret-k Insertion.

`build_daily_plan(day, travel)`:
1. ПГП (ADO-03) → наряды групп исключаются из распределения, создаются алерты `PGP_DETECTED`;
2. допуски (ADO-04) с учётом дороги из/в СЦ → наряды без кандидатов сразу получают причину;
3. приоритеты (ADO-05) — для ранжирования при равных сожалениях;
4. `regret_insertion()` — общий для ADO-09, ремонта ALNS (ADO-10) и перепланирования (ADO-13):
   для каждого наряда лучшая вставка в маршрут каждого допущенного сотрудника (ADO-08), затем
   цикл выбора наряда с максимальным

       score = weight · (regret_k + priority_weight_sec · priority),
       regret_k = Σ_{j=2..k} (c_j − c_1),   c_j — стоимость j-го лучшего маршрута (прирост дороги, с);
       отсутствующие альтернативы — c_1 + missing_penalty_sec,

   то есть раньше назначаются ценные наряды, у которых потеря лучшего варианта дороже всего
   (в том числе единственный подходящий специалист). Равенство — по `(−c_1, order_id)`.
   После вставки пересчитываются варианты **только** для изменённого маршрута;
5. опционально `improver` (ADO-10) получает план и остаток бюджета;
6. хвост → причины (диагностика на пустом маршруте) и алерты `UNASSIGNED`.

Детерминированность: перечисления в отсортированном порядке, случайности нет.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from ado.data.calendar import DEFAULT_CALENDAR
from ado.domain.contracts import TravelTimeProvider
from ado.domain.enums import AlertAction, AlertStatus, AlertType, UnassignedAction, UnassignedReasonCode
from ado.domain.models import (
    Alert,
    DayInstance,
    InsertionOption,
    Order,
    Plan,
    PlanMetrics,
    SimulationResult,
    TimeWindow,
    UnassignedReason,
    VisitItem,
)
from ado.optimization.budget import TimeBudget
from ado.optimization.eligibility import AvailabilityState, EligibilityIndex
from ado.optimization.evaluation import EvaluationConfig, evaluate_plan
from ado.optimization.insertion import InsertionConfig, InsertionEngine
from ado.optimization.pgp import PgpConfig, PgpDetector
from ado.optimization.priority import PriorityConfig, prioritize_orders
from ado.optimization.state import RouteState, WorkingPlan
from ado.optimization.validator import ValidationIssue, validate_plan

Improver = Callable[[WorkingPlan, TimeBudget], WorkingPlan]


@dataclass(frozen=True, slots=True)
class InitialConfig:
    regret_k: int = 3
    missing_penalty_sec: int = 4 * 3600
    priority_weight_sec: float = 600.0
    insertion: InsertionConfig = field(default_factory=InsertionConfig)
    pgp: PgpConfig = field(default_factory=PgpConfig)
    priority: PriorityConfig = field(default_factory=PriorityConfig)
    evaluation: EvaluationConfig = field(default_factory=EvaluationConfig)
    seed: int = 0


@dataclass(slots=True)
class InitialPlanResult:
    plan: Plan
    working: WorkingPlan
    metrics: PlanMetrics
    issues: list[ValidationIssue]
    stats: dict[str, float | int]


@dataclass(slots=True)
class RegretOutcome:
    assigned: list[str]
    pending: list[str]
    iterations: int
    stopped_by_budget: bool


def new_alert_id(working: WorkingPlan) -> str:
    return f"ALR-{working.day.date.replace('-', '')}-{len(working.alerts) + 1:04d}"


def proposed_next_day_slot(order: Order, day: DayInstance) -> TimeWindow:
    """Тот же слот в ближайший рабочий день — в секундах от полуночи текущего дня (A8)."""
    d = date.fromisoformat(day.date)
    nxt = DEFAULT_CALENDAR.next_working_day(d)
    shift = (nxt - d).days * 86_400
    return TimeWindow(order.slot.start + shift, order.slot.end + shift)


def empty_routes(day: DayInstance) -> dict[str, RouteState]:
    routes = {}
    for e in sorted(day.active_employees, key=lambda e: e.staff_id):
        st = RouteState(e, earliest_departure=day.now)
        st.set_schedule([], SimulationResult(True), day)
        routes[e.staff_id] = st
    return routes


def diagnose_unassigned(
    order: Order, staff_ids: list[str], working: WorkingPlan, since: int
) -> UnassignedReason:
    """Причина для наряда с кандидатами, но без допустимой вставки."""
    day, travel = working.day, working.travel
    hints = []
    for sid in staff_ids:
        r = working.routes.get(sid)
        if r is None:
            continue
        alone = RouteState(
            r.employee,
            frozen=r.frozen,
            earliest_departure=r.earliest_departure,
            resume_from_depot_at=r.resume_from_depot_at,
            blocked=r.blocked,
            not_before=dict(r.not_before),
            end_at_depot=r.end_at_depot,
            require_break=r.require_break,
        )
        sim = alone.simulate([VisitItem(order.order_id)], day, travel)
        if sim.feasible:
            return UnassignedReason(
                order.order_id,
                UnassignedReasonCode.NO_SHIFT_TIME,
                f"У {len(staff_ids)} подходящих сотрудников не хватает времени смены: маршруты заполнены",
                UnassignedAction.RESCHEDULE,
                proposed_next_day_slot(order, day),
                since,
            )
        if sim.violation and sim.violation.unassigned_hint:
            hints.append(sim.violation)
    if hints:
        v = hints[0]
        code = v.unassigned_hint or UnassignedReasonCode.OTHER
        return UnassignedReason(
            order.order_id,
            code,
            f"Ни один подходящий сотрудник не может выполнить: {v.message}",
            UnassignedAction.RESCHEDULE,
            proposed_next_day_slot(order, day),
            since,
        )
    return UnassignedReason(
        order.order_id,
        UnassignedReasonCode.OTHER,
        "Допустимая вставка не найдена",
        UnassignedAction.ESCALATE,
        None,
        since,
    )


def explain_option(opt: InsertionOption, alternatives: int, order: Order) -> str:
    visit = next(s for s in opt.simulation.stops if s.order_id == order.order_id)
    slack = (order.latest_start - visit.start) // 60
    parts = [f"мин. прирост пути {opt.delta_travel_sec // 60:+d} мин"]
    parts.append(
        f"других допустимых исполнителей на момент назначения: {alternatives}"
        if alternatives
        else "на момент назначения других допустимых исполнителей не было"
    )
    parts.append(f"запас до последнего начала {slack} мин")
    if opt.with_restock:
        parts.append(f"с пополнением АО в {opt.restock_service_center_id}")
    return "; ".join(parts)


def regret_insertion(
    working: WorkingPlan,
    order_ids: Iterable[str],
    engine: InsertionEngine,
    config: InitialConfig,
    budget: TimeBudget | None = None,
    routes_filter: set[str] | None = None,
    only_routes: dict[str, set[str]] | None = None,
) -> RegretOutcome:
    """Вставить наряды методом regret-k в маршруты допущенных сотрудников.

    `routes_filter` ограничивает маршруты для всех нарядов; `only_routes[order_id]` — для отдельного
    наряда (ALNS: нераспределённому наряду имеет смысл пробовать только изменённые маршруты — в
    остальных вставка уже не нашлась). После каждой вставки маршрут пересчитывается для всех.
    `working.eligible` и `working.priority` должны быть заполнены.
    """
    day = working.day
    ids = sorted(set(order_ids), key=lambda x: (-working.priority.get(x, 0.0), x))
    options: dict[str, dict[str, InsertionOption]] = {}
    for oid in ids:
        o = day.orders[oid]
        opts = {}
        allowed = only_routes.get(oid) if only_routes is not None else None
        for sid in working.eligible.get(oid, ()):
            if routes_filter is not None and sid not in routes_filter:
                continue
            if allowed is not None and sid not in allowed:
                continue  # маршрут не менялся с прошлой неудачной попытки — вставки там нет
            route = working.routes.get(sid)
            if route is None:
                continue
            opt = engine.best_for_route(o, route)
            if opt is not None:
                opts[sid] = opt
        options[oid] = opts
    k = config.regret_k
    pending = set(ids)
    assigned: list[str] = []
    stopped = False
    while pending:
        if budget is not None and budget.expired():
            stopped = True
            break
        best_key = None
        best_oid = None
        for oid in pending:
            opts = options[oid]
            if not opts:
                continue
            costs = sorted(opt.score for opt in opts.values())
            c1 = costs[0]
            regret = sum(
                (costs[j] if j < len(costs) else c1 + config.missing_penalty_sec) - c1 for j in range(1, k)
            )
            score = day.orders[oid].weight * (
                regret + config.priority_weight_sec * working.priority.get(oid, 0.0)
            )
            key = (score, -c1, oid)
            if best_key is None or key > best_key:
                best_key, best_oid = key, oid
        if best_oid is None:
            break
        oid = best_oid
        opts = options[oid]
        chosen = min(opts.values(), key=lambda x: (x.score, x.staff_id))
        route = working.routes[chosen.staff_id]
        route.set_schedule(list(chosen.items), chosen.simulation, day)
        working.explanations[oid] = explain_option(chosen, len(opts) - 1, day.orders[oid])
        working.unassigned.pop(oid, None)
        pending.discard(oid)
        assigned.append(oid)
        sid = chosen.staff_id
        for other in pending:
            if sid in options[other] or sid in working.eligible.get(other, ()):
                opt = engine.best_for_route(day.orders[other], route)
                if opt is None:
                    options[other].pop(sid, None)
                else:
                    options[other][sid] = opt
    return RegretOutcome(assigned, sorted(pending), len(assigned), stopped)


def compute_eligibility(
    working: WorkingPlan, order_ids: Iterable[str], config: InitialConfig
) -> dict[str, UnassignedReason]:
    """Заполнить `working.eligible`/`priority`; вернуть причины для нарядов без кандидатов."""
    day = working.day
    idx = EligibilityIndex(day.reference, working.travel)
    unavailable = {
        sid: (TimeWindow(fr, until if until is not None else 10**9),)
        for sid, (fr, until) in working.unavailable.items()
    }
    avail = AvailabilityState(absent=day.absent_staff_ids, unavailable=unavailable)
    active = [e for e in day.active_employees if e.staff_id in working.routes]
    reasons: dict[str, UnassignedReason] = {}
    orders = [day.orders[oid] for oid in sorted(set(order_ids))]
    for o in orders:
        res = idx.find_eligible_staff(o, active, avail)
        working.eligible[o.order_id] = res.staff_ids
        if not res.eligible:
            reason = idx.explain(o, res, since=day.now)
            assert reason is not None
            if reason.suggested_action is UnassignedAction.RESCHEDULE and reason.proposed_slot is None:
                reason = UnassignedReason(
                    reason.order_id,
                    reason.code,
                    reason.text,
                    reason.suggested_action,
                    proposed_next_day_slot(o, day),
                    reason.since,
                )
            reasons[o.order_id] = reason
    scored = prioritize_orders(
        [o for o in orders if working.eligible[o.order_id]],
        working.eligible,
        day.now,
        day.reference,
        config.priority,
    )
    working.priority.update({s.order_id: s.value for s in scored})
    return reasons


def add_unassigned_alerts(working: WorkingPlan, order_ids: Iterable[str], at: int) -> list[Alert]:
    out = []
    for oid in sorted(order_ids):
        r = working.unassigned[oid]
        a = Alert(
            new_alert_id(working),
            at,
            AlertType.UNASSIGNED,
            AlertStatus.ACTIVE,
            AlertAction.RESCHEDULE if r.suggested_action is UnassignedAction.RESCHEDULE else AlertAction.NONE,
            f"{oid}: {r.text}",
            order_id=oid,
            proposed_slot=r.proposed_slot,
        )
        working.alerts.append(a)
        out.append(a)
    return out


def build_daily_plan(
    day: DayInstance,
    travel: TravelTimeProvider,
    config: InitialConfig = InitialConfig(),
    budget: TimeBudget | None = None,
    improver: Improver | None = None,
) -> InitialPlanResult:
    t0 = time.perf_counter()
    budget = budget or TimeBudget(300, reserve=15)
    ref = day.reference
    working = WorkingPlan(day, travel, empty_routes(day))

    # 1. ПГП
    pgp = PgpDetector(config.pgp).detect(day.orders.values(), [], frozenset(), day.now, ref, day=day.date)
    working.pgp_groups = pgp.groups
    for g in pgp.groups:
        working.alerts.append(
            Alert(
                new_alert_id(working),
                day.now,
                AlertType.PGP_DETECTED,
                AlertStatus.ACTIVE,
                AlertAction.TRANSFER_TO_STATION,
                f"ПГП на {g.access_node_id}: {n_orders_ru(len(g.order_ids))} переданы в станционное подразделение",
                group_id=g.group_id,
                applied=True,
            )
        )
    order_ids = [oid for oid in sorted(day.orders) if oid not in pgp.grouped_order_ids]
    t_pgp = time.perf_counter()

    # 2–3. допуски и приоритеты
    working.unassigned.update(compute_eligibility(working, order_ids, config))
    t_elig = time.perf_counter()

    # 4. regret
    engine = InsertionEngine(day, travel, config.insertion)
    candidates = [oid for oid in order_ids if working.eligible.get(oid)]
    outcome = regret_insertion(working, candidates, engine, config, budget)
    t_regret = time.perf_counter()

    # 5. улучшение
    improve_stats: dict[str, float | int] = {}
    if improver is not None and not budget.expired():
        working = improver(working, budget)
        improve_stats = dict(getattr(improver, "last_stats", {}) or {})
    t_improve = time.perf_counter()

    # 6. причины и алерты
    assigned_now = working.assigned()
    for oid in candidates:
        if oid in assigned_now:
            working.unassigned.pop(oid, None)
            continue
        if oid in working.unassigned and oid not in outcome.pending:
            continue
        reason = diagnose_unassigned(day.orders[oid], working.eligible[oid], working, day.now)
        if outcome.stopped_by_budget and oid in outcome.pending:
            reason = UnassignedReason(
                oid,
                UnassignedReasonCode.OTHER,
                "Исчерпан бюджет времени расчёта до рассмотрения наряда",
                UnassignedAction.ESCALATE,
                reason.proposed_slot,
                reason.since,
            )
        working.unassigned[oid] = reason
    add_unassigned_alerts(working, working.unassigned, day.now)
    t_reasons = time.perf_counter()

    plan = working.to_plan(1, day.now, None)
    issues = validate_plan(plan, day, travel)
    metrics = evaluate_plan(plan, day, travel, config.evaluation, issues)
    t_end = time.perf_counter()
    stats: dict[str, float | int] = dict(
        pgp_sec=t_pgp - t0,
        eligibility_sec=t_elig - t_pgp,
        regret_sec=t_regret - t_elig,
        improve_sec=t_improve - t_regret,
        reasons_sec=t_reasons - t_improve,
        validate_sec=t_end - t_reasons,
        total_sec=t_end - t0,
        iterations=outcome.iterations,
        simulations=engine.stats.simulations,
        prechecks=engine.stats.prechecks,
        stopped_by_budget=int(outcome.stopped_by_budget),
    )
    stats.update({f"improve_{k}": v for k, v in improve_stats.items()})
    plan.computation_ms = int((t_end - t0) * 1000)
    return InitialPlanResult(plan, working, metrics, issues, stats)


def n_orders_ru(n: int) -> str:
    """«3 наряда», «5 нарядов», «21 наряд»."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} наряд"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} наряда"
    return f"{n} нарядов"
