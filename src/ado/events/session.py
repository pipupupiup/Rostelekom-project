"""ADO-13 — динамическое перепланирование: сессия одного рабочего дня.

`DaySession` хранит актуальное состояние дня (D5): рабочий план, номер версии, журнал событий,
фактические окончания визитов и историю опубликованных версий. Публичные операции:

* `build_plan(time_limit_sec)` — первоначальный план (ADO-09 + ADO-10), версия 1;
* `apply(event)` — одно событие, ответ ≤ 10 с (бюджет `event_budget_sec` с резервом);
* `current_plan()`.

Обработка события (п. 3 спецификации, задание ADO-13):
1. идемпотентность (`event_id` уже принят → прежний результат, `duplicate = True`), порядок
   (`timestamp` раньше последнего → `EventOutOfOrder`, 409), ссылки и согласованность с
   состоянием (`EventRejected`, 422) — при ошибке состояние не меняется;
2. модельное время := `timestamp`; **заморозка**: остановки с `start < t` переходят в
   `RouteState.frozen` и больше не меняются (кроме `end` по событию задержки/передачи);
3. применение события (новый наряд, отмена, задержка, передача, недоступность, ход времени);
4. ремонт затронутых маршрутов по уровням: (1) пересчёт и перестановка внутри маршрута;
   (2–4) вытесненные наряды — regret-вставка по всем допущенным сотрудникам (поиск сам
   расширяется от ближайших к остальным, ADO-06/08);
5. минимальное вмешательство (D20): после ремонта — только вставка нераспределённых нарядов и
   перестановки внутри изменённых маршрутов; межмаршрутных переносов «ради дороги» нет — каждое
   переназначение должно сопровождаться своевременным предупреждением;
6. `LATE_RISK` по каждому переназначенному/снятому наряду (с прогнозом без вмешательства, ADO-12),
   `UNASSIGNED` — по новым нераспределённым, `PGP_DETECTED` — по новым группам;
7. независимая проверка (H1–H10 с предыдущей версией). При нарушении — безопасный режим: в
   проблемных маршрутах остаются только замороженные остановки, наряды получают причины;
8. `plan_version + 1` (даже если план не изменился).

Каждое изменение маршрута проходит симулятор (D6); выполненная часть не меняется.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from ado.domain.contracts import TravelTimeProvider
from ado.domain.enums import AlertAction, AlertStatus, AlertType, StopType
from ado.domain.events import (
    AnyEvent,
    NewOrderEvent,
    OrderCancelledEvent,
    OrderTransferredEvent,
    StaffUnavailableEvent,
    TimeTickEvent,
    VisitDelayedEvent,
)
from ado.domain.models import (
    Alert,
    CancelledOrder,
    DayInstance,
    Plan,
    PlanItem,
    RestockItem,
    RouteStop,
    SimulationResult,
    TimeWindow,
    VisitItem,
)
from ado.domain.timebase import Seconds
from ado.optimization.breaks import BreakPlan, stagger_breaks
from ado.optimization.budget import TimeBudget
from ado.optimization.improvement import (
    ImproveConfig,
    ImproveStats,
    LocalSearch,
    RouteImprover,
    remove_orders,
)
from ado.optimization.initial import (
    InitialConfig,
    add_unassigned_alerts,
    build_daily_plan,
    compute_eligibility,
    diagnose_unassigned,
    explain_option,
    n_orders_ru,
    new_alert_id,
    proposed_next_day_slot,
    regret_insertion,
)
from ado.optimization.insertion import InsertionEngine
from ado.optimization.pgp import PgpDetector
from ado.optimization.risk import RiskConfig, forecast_route
from ado.optimization.state import RouteState, WorkingPlan
from ado.optimization.validator import ValidationIssue, validate_plan


class SessionError(Exception):
    status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, code: str | None = None, field: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.field = field


class PlanNotFound(SessionError):
    status = 404
    code = "PLAN_NOT_FOUND"


class EventOutOfOrder(SessionError):
    status = 409
    code = "EVENT_OUT_OF_ORDER"


class PlanAlreadyReplanned(SessionError):
    status = 409
    code = "PLAN_ALREADY_REPLANNED"


class EventRejected(SessionError):
    status = 422
    code = "STATE_CONFLICT"


@dataclass(frozen=True, slots=True)
class ReoptConfig:
    event_budget_sec: float = 10.0
    event_reserve_sec: float = 2.0
    initial: InitialConfig = field(default_factory=InitialConfig)
    improve: ImproveConfig = field(default_factory=ImproveConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    plan_reserve_sec: float = 20.0
    # варианты для сравнения в ADO-17 (по умолчанию — полный ADO)
    initial_algorithm: str = "regret"
    """`regret` (ADO-09 + ADO-10) или `baseline` (последовательное назначение)."""
    dynamic: str = "full"
    """`full` (ADO-13) или `naive`: вытесненные наряды снимаются без перевставки и улучшения."""
    late_risk_alerts: bool = True
    stagger_breaks: bool = True
    """D36: развести обеды смен внутри входных окон, чтобы во время перерыва одной работала другая."""


@dataclass(slots=True)
class EventOutcome:
    event_id: str
    accepted: bool
    duplicate: bool
    plan_version: int
    as_of: Seconds
    processing_ms: int
    changed_staff_ids: list[str]
    new_alerts: list[Alert]
    message: str
    issues: list[ValidationIssue] = field(default_factory=list)
    stages_ms: dict[str, int] = field(default_factory=dict)
    """Время по этапам (ADO-14): freeze, apply_event, reinsert, improve, reconcile_alerts, validate."""


def _items_of(stops: tuple[RouteStop, ...]) -> list[PlanItem]:
    out: list[PlanItem] = []
    for s in stops:
        if s.type is StopType.VISIT and s.order_id:
            out.append(VisitItem(s.order_id))
        elif s.type is StopType.RESTOCK:
            out.append(RestockItem(s.location_id))
    return out


class DaySession:
    def __init__(
        self, day: DayInstance, travel: TravelTimeProvider, config: ReoptConfig | None = None
    ) -> None:
        self.config = config or ReoptConfig()
        self.break_plan: BreakPlan | None = None
        if self.config.stagger_breaks:
            employees, self.break_plan = stagger_breaks(day.reference.employees)
            if self.break_plan.changed:
                day = replace(day, reference=replace(day.reference, employees=employees))
        self.day = day
        self.travel = travel
        self.working: WorkingPlan | None = None
        self.plan: Plan | None = None
        self.version = 0
        self.now: Seconds = day.now
        self.results: dict[str, EventOutcome] = {}
        self.end_overrides: dict[str, Seconds] = {}
        self.not_before: dict[str, Seconds] = {}
        self.history: list[Plan] = []
        self.snapshots: list[tuple[Plan, dict[str, Seconds], dict[str, Seconds], frozenset[str]]] = []
        """(версия, фактические окончания, not_before, известные наряды) — для проверки каждой версии."""
        self.last_stats: dict[str, float | int] = {}

    # ------------------------------------------------------------ план
    def build_plan(self, time_limit_sec: float = 300.0) -> Plan:
        if self.results:
            raise PlanAlreadyReplanned(
                "план уже перепланировался по событиям; повторное построение недоступно"
            )
        limit = max(1.0, min(time_limit_sec, 300.0))
        reserve = min(self.config.plan_reserve_sec, limit * 0.2)
        budget = TimeBudget(limit, reserve=reserve)
        alns_time = max(0.0, min(self.config.improve.time_limit_sec, limit - reserve - 10))
        improver = RouteImprover(replace(self.config.improve, time_limit_sec=alns_time), self.config.initial)
        if self.config.initial_algorithm == "baseline":
            from ado.optimization.baseline import build_baseline_plan

            res = build_baseline_plan(self.day, self.travel, self.config.initial.pgp)
        else:
            res = build_daily_plan(self.day, self.travel, self.config.initial, budget, improver)
        self.working = res.working
        self.version = 1
        self.plan = res.plan
        self.history = [res.plan]
        self.snapshots = [(res.plan, {}, {}, frozenset(self.day.orders))]
        self.last_stats = res.stats
        return res.plan

    def current_plan(self) -> Plan:
        if self.plan is None:
            raise PlanNotFound(f"План на {self.day.date} ещё не построен")
        return self.plan

    # ------------------------------------------------------------ события
    def apply(self, event: AnyEvent) -> EventOutcome:
        t0 = time.perf_counter()
        if event.event_id in self.results:
            prev = self.results[event.event_id]
            return EventOutcome(
                prev.event_id, True, True, prev.plan_version, prev.as_of, 0, [], [], "повтор события"
            )
        if self.plan is None or self.working is None:
            raise PlanNotFound(f"План на {self.day.date} ещё не построен")
        if event.timestamp < self.now:
            raise EventOutOfOrder(
                f"timestamp события раньше последнего принятого момента ({event.timestamp} < {self.now})"
            )
        self._check(event)
        # атомарность: при любом исключении состояние сессии возвращается к исходному
        saved = (dict(self.day.orders), dict(self.end_overrides), dict(self.not_before))
        try:
            return self._apply(event, t0)
        except Exception:
            self.day.orders.clear()
            self.day.orders.update(saved[0])
            self.end_overrides, self.not_before = saved[1], saved[2]
            raise

    def _apply(self, event: AnyEvent, t0: float) -> EventOutcome:
        stages: dict[str, int] = {}
        last = [t0]

        def mark(name: str) -> None:
            t = time.perf_counter()
            stages[name] = stages.get(name, 0) + int((t - last[0]) * 1000)
            last[0] = t

        budget = TimeBudget(self.config.event_budget_sec, reserve=self.config.event_reserve_sec)
        day = self.day
        prev_plan = self.plan
        w = self.working.copy()
        now = event.timestamp
        before = w.assigned()
        before_stops = {
            oid: s for r in prev_plan.routes.values() for s in r.stops if s.order_id for oid in [s.order_id]
        }
        force_freeze = (
            event.order_id if isinstance(event, VisitDelayedEvent | OrderTransferredEvent) else None
        )
        self._freeze(w, now, force_freeze)
        mark("freeze")
        engine = InsertionEngine(day, self.travel, self.config.initial.insertion)
        changed: set[str] = set()
        displaced: list[str] = []
        new_alerts: list[Alert] = []
        msg: list[str] = []
        risk_forecast: dict[str, tuple[Seconds | None, int | None]] = {}

        if isinstance(event, NewOrderEvent):
            self._on_new_order(w, event, engine, changed, new_alerts, msg)
        elif isinstance(event, OrderCancelledEvent):
            self._on_cancel(w, event, changed, msg)
        elif isinstance(event, VisitDelayedEvent | OrderTransferredEvent):
            end = event.expected_end if isinstance(event, VisitDelayedEvent) else event.actual_end
            self._override_end(w, event.staff_id, event.order_id, end)
            risk_forecast = self._forecast(w, now)
            displaced += self._repair_route(w, event.staff_id, engine)
            changed.add(event.staff_id)
            msg.append(
                (
                    "задержка"
                    if isinstance(event, VisitDelayedEvent)
                    else "передача в профильное подразделение"
                )
                + f" {event.order_id}: окончание {end // 3600:02d}:{end % 3600 // 60:02d}"
            )
        elif isinstance(event, StaffUnavailableEvent):
            displaced += self._on_unavailable(w, event, changed, msg)
            risk_forecast = {oid: (None, None) for oid in displaced}
        elif isinstance(event, TimeTickEvent):
            msg.append("ход модельного времени")

        mark("apply_event")
        naive = self.config.dynamic == "naive"
        # 4. вытесненные наряды: перевставка по всем допущенным
        if displaced and not naive:
            w.unassigned.update(compute_eligibility(w, displaced, self.config.initial))
            out = regret_insertion(w, displaced, engine, self.config.initial, budget)
            changed.update(w.assigned().get(oid) for oid in out.assigned)
            changed.discard(None)  # type: ignore[arg-type]
        mark("reinsert")
        # 5. минимальное вмешательство: вставка нераспределённых + перестановки в изменённых маршрутах
        if changed and not naive and not budget.expired():
            ls = LocalSearch(engine, self.config.initial, self.config.improve, ImproveStats())
            ls.insert_unassigned(w, budget, changed)
            changed |= ls.anchor_insert(
                w, budget, set(changed)
            )  # D32: новый/перенесённый наряд может стать «якорем»
            sub = WorkingPlan(
                w.day, w.travel, {sid: w.routes[sid] for sid in sorted(changed) if sid in w.routes}
            )
            ls.intra(sub, budget)
        mark("improve")
        # учёт H9: всё, что не размещено, — в unassigned с причиной
        newly_unassigned = self._reconcile(w, now)
        new_alerts += add_unassigned_alerts(w, newly_unassigned, now)
        self._resolve_assigned(w, now)
        if self.config.late_risk_alerts:
            new_alerts += self._late_risk_alerts(w, before, before_stops, risk_forecast, now)

        mark("reconcile_alerts")
        # 7. проверка и безопасный режим
        version = self.version + 1
        plan = w.to_plan(version, now, event.event_id)
        issues = validate_plan(
            plan,
            day,
            self.travel,
            previous=prev_plan,
            end_overrides=self.end_overrides,
            not_before=self.not_before,
        )
        if issues:
            self._safe_mode(w, {i.staff_id for i in issues if i.staff_id}, now)
            new_alerts += add_unassigned_alerts(w, self._reconcile(w, now), now)
            plan = w.to_plan(version, now, event.event_id)
            issues = validate_plan(
                plan,
                day,
                self.travel,
                previous=prev_plan,
                end_overrides=self.end_overrides,
                not_before=self.not_before,
            )
            msg.append("безопасный режим: проблемные маршруты сокращены до выполненной части")
        mark("validate")
        changed_staff = sorted(
            sid
            for sid in plan.routes
            if plan.routes[sid].stops != prev_plan.routes.get(sid, plan.routes[sid]).stops
            or sid not in prev_plan.routes
        )
        ms = int((time.perf_counter() - t0) * 1000)
        plan.computation_ms = ms
        self.working, self.plan, self.version, self.now = w, plan, version, now
        self.history.append(plan)
        self.snapshots.append(
            (plan, dict(self.end_overrides), dict(self.not_before), frozenset(self.day.orders))
        )
        outcome = EventOutcome(
            event.event_id,
            True,
            False,
            version,
            now,
            ms,
            changed_staff,
            new_alerts,
            "; ".join(msg) or "событие учтено",
            issues,
            stages,
        )
        self.results[event.event_id] = outcome
        return outcome

    # ------------------------------------------------------------ проверки
    def _check(self, event: AnyEvent) -> None:
        day = self.day
        assert self.working is not None
        if isinstance(event, NewOrderEvent):
            o = event.order
            if o.order_id in day.orders:
                raise EventRejected(
                    f"Наряд {o.order_id} уже существует в дне {day.date}", "DUPLICATE_ORDER", "order"
                )
            if not self.travel.has_point(o.building_id):
                raise EventRejected(f"Дом {o.building_id} отсутствует в матрице", "UNKNOWN_BUILDING", "order")
            return
        if isinstance(event, OrderCancelledEvent):
            if event.order_id not in day.orders:
                raise EventRejected(
                    f"Наряд {event.order_id} не найден в дне {day.date}", "UNKNOWN_ORDER", "order_id"
                )
            return
        if isinstance(event, VisitDelayedEvent | OrderTransferredEvent | StaffUnavailableEvent):
            if event.staff_id not in day.reference.employees:
                raise EventRejected(f"Сотрудник {event.staff_id} не найден", "UNKNOWN_STAFF", "staff_id")
            if event.staff_id not in self.working.routes:
                raise EventRejected(
                    f"Сотрудник {event.staff_id} не на смене", "STAFF_NOT_ON_SHIFT", "staff_id"
                )
        if isinstance(event, VisitDelayedEvent | OrderTransferredEvent):
            if event.order_id not in day.orders:
                raise EventRejected(
                    f"Наряд {event.order_id} не найден в дне {day.date}", "UNKNOWN_ORDER", "order_id"
                )
            route = self.working.routes[event.staff_id]
            stop = next((s for s in route.sim.stops if s.order_id == event.order_id), None)
            if stop is None:
                raise EventRejected(
                    f"Наряд {event.order_id} не в маршруте {event.staff_id}", field="order_id"
                )
            if stop.start > event.timestamp:
                raise EventRejected(f"Визит {event.order_id} ещё не начат", field="timestamp")
            end = event.expected_end if isinstance(event, VisitDelayedEvent) else event.actual_end
            if end < stop.start:
                raise EventRejected("Окончание работ раньше их начала", field="expected_end")
        if isinstance(event, StaffUnavailableEvent):
            if event.until is not None and event.until <= event.timestamp:
                raise EventRejected("until не позже timestamp", field="until")
            if (
                event.staff_id in self.working.unavailable
                and self.working.unavailable[event.staff_id][1] is None
            ):
                raise EventRejected(f"Сотрудник {event.staff_id} уже выбыл до конца смены", field="staff_id")

    # ------------------------------------------------------------ состояние
    def _freeze(self, w: WorkingPlan, now: Seconds, force_order: str | None = None) -> None:
        for r in w.routes.values():
            stops = r.sim.stops
            k = 0
            while k < len(stops) and stops[k].start < now:
                k += 1
            if force_order is not None:
                idx = next((i for i, s in enumerate(stops) if s.order_id == force_order), None)
                if idx is not None:
                    k = max(k, idx + 1)
            r.frozen = tuple(stops[:k])
            r.earliest_departure = now
            r.set_schedule(_items_of(stops[k:]), r.sim, w.day)

    def _override_end(self, w: WorkingPlan, sid: str, oid: str, end: Seconds) -> None:
        r = w.routes[sid]
        self.end_overrides[oid] = end
        r.frozen = tuple(
            s
            if s.order_id != oid
            else RouteStop(
                s.type,
                s.location_id,
                s.arrival,
                s.start,
                end,
                s.order_id,
                s.travel_sec,
                s.distance_m,
                s.equipment_after,
                s.explanation,
            )
            for s in r.frozen
        )

    def _forecast(self, w: WorkingPlan, now: Seconds) -> dict[str, tuple[Seconds | None, int | None]]:
        """Прогноз без вмешательства по текущей последовательности (с новыми фактическими окончаниями)."""
        out: dict[str, tuple[Seconds | None, int | None]] = {}
        plan = w.to_plan(self.version, now)
        for _sid, route in plan.routes.items():
            stops = list(route.stops)
            for i, s in enumerate(stops):
                if s.order_id in self.end_overrides and s.type is StopType.VISIT:
                    stops[i] = RouteStop(
                        s.type, s.location_id, s.arrival, s.start, self.end_overrides[s.order_id], s.order_id
                    )
            route.stops = stops
            for f in forecast_route(route, self.day, self.travel, now, self.end_overrides):
                late = (f.forecast_start - f.latest_start + 59) // 60
                out[f.order_id] = (f.forecast_start, late if late > 0 else None)
        return out

    def _resim(self, w: WorkingPlan, sid: str, items: list[PlanItem]) -> SimulationResult:
        r = w.routes[sid]
        return r.simulate(items, w.day, w.travel)

    def _repair_route(self, w: WorkingPlan, sid: str, engine: InsertionEngine) -> list[str]:
        """Уровень 1: пересчёт, затем пересборка порядка внутри маршрута. → вытесненные наряды."""
        r = w.routes[sid]
        sim = self._resim(w, sid, r.items)
        if sim.feasible:
            r.set_schedule(list(r.items), sim, w.day)
            return []
        if (
            self.config.dynamic == "naive"
        ):  # без перестановок: снимаем первый недопустимый визит, пока не сойдётся
            items, dropped = list(r.items), []
            while items:
                v = sim.violation
                k = next(
                    (
                        i
                        for i, it in enumerate(items)
                        if isinstance(it, VisitItem) and v and it.order_id == v.order_id
                    ),
                    None,
                )
                if k is None:
                    k = (
                        max(i for i, it in enumerate(items) if isinstance(it, VisitItem))
                        if any(isinstance(it, VisitItem) for it in items)
                        else len(items) - 1
                    )
                it = items.pop(k)
                if isinstance(it, VisitItem):
                    dropped.append(it.order_id)
                sim = self._resim(w, sid, items)
                if sim.feasible:
                    r.set_schedule(items, sim, w.day)
                    return dropped
            base = self._resim(w, sid, [])
            if base.feasible:
                r.set_schedule([], base, w.day)
            else:
                self._force_minimal(w, sid)
            return dropped
        visits = [i.order_id for i in r.items if isinstance(i, VisitItem)]
        tmp = RouteState(
            r.employee,
            [],
            r.frozen,
            r.earliest_departure,
            r.resume_from_depot_at,
            r.blocked,
            dict(r.not_before),
            r.end_at_depot,
            require_break=r.require_break,
        )
        base = tmp.simulate([], w.day, w.travel)
        if not base.feasible:
            self._force_minimal(w, sid)
            return visits
        tmp.set_schedule([], base, w.day)
        order = sorted(
            visits, key=lambda oid: (-w.day.orders[oid].weight, w.day.orders[oid].latest_start, oid)
        )
        displaced = []
        for oid in order:
            opt = engine.best_for_route(w.day.orders[oid], tmp)
            if opt is None:
                displaced.append(oid)
            else:
                tmp.set_schedule(list(opt.items), opt.simulation, w.day)
        r.set_schedule(list(tmp.items), tmp.sim, w.day)
        return displaced

    def _force_minimal(self, w: WorkingPlan, sid: str) -> None:
        """Маршрут из замороженной части не завершается допустимо (событие увело за смену):
        публикуем выполненную часть + возвращение в СЦ как есть."""
        r = w.routes[sid]
        stops = list(r.frozen)
        if r.end_at_depot and stops and stops[-1].type is not StopType.DEPOT_END:
            last = stops[-1]
            home = r.employee.service_center_id
            tt = w.travel.travel_sec(last.location_id, home) or 0
            arr = max(last.end + tt, r.earliest_departure or 0)
            stops.append(
                RouteStop(
                    StopType.DEPOT_END,
                    home,
                    arr,
                    arr,
                    arr,
                    None,
                    tt,
                    w.travel.distance_m(last.location_id, home) or 0,
                )
            )
        r.items = []
        r.sim = SimulationResult(True, tuple(stops))
        r.chain, r.push, r.inv_gap, r.inv_floor = (
            [],
            [],
            [r.employee.equipment_start],
            [r.employee.equipment_start],
        )

    def _on_new_order(self, w, event: NewOrderEvent, engine, changed, new_alerts, msg) -> None:
        day = self.day
        o = event.order
        day.orders[o.order_id] = o
        self.not_before[o.order_id] = event.timestamp
        for r in w.routes.values():
            r.not_before[o.order_id] = event.timestamp
        started = frozenset(s.order_id for r in w.routes.values() for s in r.frozen if s.order_id)
        known = {g.group_id for g in w.pgp_groups}
        det = PgpDetector(self.config.initial.pgp).detect(
            day.orders.values(),
            w.pgp_groups,
            started,
            event.timestamp,
            day.reference,
            day=day.date,
            cancelled_order_ids=frozenset(w.cancelled),
        )
        w.pgp_groups = det.groups
        grouped = det.grouped_order_ids
        moved = [oid for oid in grouped if oid in w.assigned()]
        if moved:
            _, ch = remove_orders(w, set(moved))
            changed.update(ch)
        for oid in grouped:
            w.unassigned.pop(oid, None)
        for g in det.groups:
            if g.group_id not in known:
                a = Alert(
                    new_alert_id(w),
                    event.timestamp,
                    AlertType.PGP_DETECTED,
                    AlertStatus.ACTIVE,
                    AlertAction.TRANSFER_TO_STATION,
                    f"ПГП на {g.access_node_id}: {n_orders_ru(len(g.order_ids))} переданы в станционное подразделение",
                    group_id=g.group_id,
                    applied=True,
                )
                w.alerts.append(a)
                new_alerts.append(a)
                msg.append(f"выявлена ПГП {g.group_id} ({n_orders_ru(len(g.order_ids))})")
        if o.order_id in grouped:
            msg.append(f"{o.order_id} включён в ПГП")
            return
        reasons = compute_eligibility(w, [o.order_id], self.config.initial)
        if o.order_id in reasons:
            w.unassigned[o.order_id] = reasons[o.order_id]
            msg.append(f"{o.order_id}: {reasons[o.order_id].text}")
            return
        opts = engine.find_feasible_insertions(o, w.eligible[o.order_id], w.routes, full=True)
        if opts:
            best = opts[0]
            w.routes[best.staff_id].set_schedule(list(best.items), best.simulation, day)
            w.explanations[o.order_id] = explain_option(best, len(opts) - 1, o)
            changed.add(best.staff_id)
            msg.append(f"{o.order_id} назначен {best.staff_id}")
        else:
            msg.append(f"{o.order_id}: допустимой вставки нет")

    def _on_cancel(
        self, w: WorkingPlan, event: OrderCancelledEvent, changed: set[str], msg: list[str]
    ) -> None:
        oid = event.order_id
        if oid in w.cancelled:
            msg.append(f"{oid} уже отменён")
            return
        if any(s.order_id == oid for r in w.routes.values() for s in r.frozen):
            msg.append(f"визит {oid} уже начат — остаётся в маршруте")
            return
        where = w.assigned().get(oid)
        if where is not None:
            r = w.routes[where]
            items = [i for i in r.items if not (isinstance(i, VisitItem) and i.order_id == oid)]
            sim = r.simulate(items, w.day, w.travel)
            if not sim.feasible:  # удаление почти всегда допустимо; иначе — пересборка
                r.items = items
                self._repair_route(w, where, InsertionEngine(w.day, w.travel))
            else:
                r.set_schedule(items, sim, w.day)
            changed.add(where)
        w.unassigned.pop(oid, None)
        for g in w.pgp_groups:
            if oid in g.order_ids:
                g.order_ids.remove(oid)
        w.cancelled[oid] = CancelledOrder(oid, event.timestamp, event.event_id)
        for a in w.alerts:
            if a.order_id == oid and a.status is AlertStatus.ACTIVE:
                a.status, a.resolved_at = AlertStatus.RESOLVED, event.timestamp
        msg.append(f"{oid} отменён ({event.reason})")

    def _on_unavailable(
        self, w: WorkingPlan, event: StaffUnavailableEvent, changed: set[str], msg: list[str]
    ) -> list[str]:
        sid = event.staff_id
        r = w.routes[sid]
        displaced = [i.order_id for i in r.items if isinstance(i, VisitItem)]
        w.unavailable[sid] = (event.timestamp, event.until)
        r.blocked = (TimeWindow(event.timestamp, event.until if event.until is not None else 10**9),)
        r.require_break = False  # A9: выбывшему по событию перерыв не требуется (вопрос Q3)
        if event.until is None:
            r.end_at_depot = False
        else:
            r.resume_from_depot_at = event.until
        sim = r.simulate([], w.day, w.travel)
        if sim.feasible:
            r.set_schedule([], sim, w.day)
        else:
            self._force_minimal(w, sid)
        changed.add(sid)
        until = (
            "до конца смены"
            if event.until is None
            else f"до {event.until // 3600:02d}:{event.until % 3600 // 60:02d}"
        )
        msg.append(f"{sid} недоступен {until} ({event.reason}); перераспределяется {len(displaced)} нарядов")
        return displaced

    def _reconcile(self, w: WorkingPlan, now: Seconds) -> list[str]:
        """Каждый наряд дня — ровно в одном месте; неразмещённые получают причину."""
        assigned = w.assigned()
        frozen_ids = {s.order_id for r in w.routes.values() for s in r.frozen if s.order_id}
        pgp = {oid for g in w.pgp_groups for oid in g.order_ids}
        new = []
        for oid in sorted(self.day.orders):
            if oid in assigned or oid in frozen_ids or oid in pgp or oid in w.cancelled:
                w.unassigned.pop(oid, None)
                continue
            if oid not in w.unassigned:
                w.unassigned[oid] = diagnose_unassigned(self.day.orders[oid], w.eligible.get(oid, []), w, now)
                new.append(oid)
        return new

    def _resolve_assigned(self, w: WorkingPlan, now: Seconds) -> None:
        assigned = w.assigned()
        for a in w.alerts:
            if a.type is AlertType.UNASSIGNED and a.status is AlertStatus.ACTIVE and a.order_id in assigned:
                a.status, a.resolved_at = AlertStatus.RESOLVED, now

    def _late_risk_alerts(self, w, before, before_stops, forecast, now) -> list[Alert]:
        after = w.assigned()
        frozen_ids = {s.order_id for r in w.routes.values() for s in r.frozen if s.order_id}
        out = []
        pgp_of = {oid: g for g in w.pgp_groups for oid in g.order_ids}
        for oid in sorted(before):
            if oid in frozen_ids or oid in w.cancelled:
                continue
            if oid in pgp_of:  # снят из маршрута в ПГП — тоже предупреждение (передача в станционное)
                g = pgp_of[oid]
                a = Alert(
                    new_alert_id(w),
                    now,
                    AlertType.LATE_RISK,
                    AlertStatus.RESOLVED,
                    AlertAction.TRANSFER_TO_STATION,
                    f"{oid}: снят с {before[oid]} — ПГП {g.group_id}",
                    order_id=oid,
                    group_id=g.group_id,
                    staff_id=before[oid],
                    applied=True,
                    resolved_at=now,
                )
                w.alerts.append(a)
                out.append(a)
                continue
            old, new = before[oid], after.get(oid)
            if new == old:
                continue
            fstart, late = forecast.get(oid, (before_stops[oid].start if oid in before_stops else None, None))
            if new is not None:
                action, staff, slot = AlertAction.REASSIGN, new, None
                text = f"{oid}: {old} не успевает — передан {new}"
            else:
                r = w.unassigned.get(oid)
                slot = (r.proposed_slot if r else None) or proposed_next_day_slot(
                    self.day.orders[oid], self.day
                )
                action, staff = AlertAction.RESCHEDULE, None
                text = f"{oid}: {old} не успевает, замены нет — предложен перенос"
            a = Alert(
                new_alert_id(w),
                now,
                AlertType.LATE_RISK,
                AlertStatus.RESOLVED,
                action,
                text,
                order_id=oid,
                staff_id=old,
                forecast_start=fstart,
                lateness_min=late,
                suggested_staff_id=staff,
                proposed_slot=slot,
                applied=True,
                resolved_at=now,
            )
            w.alerts.append(a)
            out.append(a)
        return out

    def _safe_mode(self, w: WorkingPlan, staff_ids: set[str], now: Seconds) -> None:
        for sid in sorted(staff_ids):
            if sid not in w.routes:
                continue
            r = w.routes[sid]
            sim = r.simulate([], w.day, w.travel)
            if sim.feasible:
                r.set_schedule([], sim, w.day)
            else:
                self._force_minimal(w, sid)
