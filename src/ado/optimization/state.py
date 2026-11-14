"""Рабочее состояние плана для оптимизатора (общая основа ADO-06…ADO-10, ADO-13).

`RouteState` — маршрут одного сотрудника: последовательность элементов (`VisitItem`/`RestockItem`),
замороженный префикс, параметры черновика и **текущее допустимое расписание** из симулятора.
Поверх расписания строятся кеши для O(1)-предпроверки вставки:

* `chain` — остановки, соответствующие элементам, плюс `DEPOT_END` (перерыв исключён — его
  размещает симулятор);
* `push[c]` — на сколько можно задержать прибытие в остановку `c`, не нарушив её окно и все
  последующие (классический forward time slack: `push_c = wait_c + min(lat_c − start_c, push_{c+1})`);
* `inv_gap[p]` — остаток АО перед элементом `p`; `inv_floor[p]` — остаток перед ближайшим
  пополнением после `p` (минимум на отрезке, т. к. остаток до пополнения только убывает).

Предпроверка отсекает заведомо невозможные позиции; окончательное слово всегда за
`simulate_route()` (D6), поэтому приближения предпроверки не могут дать недопустимый маршрут.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ado.domain.contracts import TravelTimeProvider
from ado.domain.enums import RouteStatus, StopType
from ado.domain.equipment import EquipmentInventory
from ado.domain.models import (
    Alert,
    CancelledOrder,
    DayInstance,
    Employee,
    Order,
    PgpGroup,
    Plan,
    PlanItem,
    RestockItem,
    Route,
    RouteDraft,
    RouteStop,
    SimulationResult,
    TimeWindow,
    UnassignedReason,
    VisitItem,
)
from ado.domain.timebase import Seconds
from ado.optimization.simulator import simulate_route

INF = 10**12


@dataclass(frozen=True, slots=True)
class PositionCheck:
    position: int
    delta_travel: Seconds
    start: Seconds
    needs_restock: bool


@dataclass(slots=True)
class RouteState:
    employee: Employee
    items: list[PlanItem] = field(default_factory=list)
    frozen: tuple[RouteStop, ...] = ()
    earliest_departure: Seconds | None = None
    resume_from_depot_at: Seconds | None = None
    blocked: tuple[TimeWindow, ...] = ()
    not_before: dict[str, Seconds] = field(default_factory=dict)
    end_at_depot: bool = True
    require_break: bool = True
    break_margin: Seconds = 3600
    sim: SimulationResult = field(default_factory=lambda: SimulationResult(True))
    chain: list[RouteStop] = field(default_factory=list)
    push: list[Seconds] = field(default_factory=list)
    inv_gap: list[EquipmentInventory] = field(default_factory=list)
    inv_floor: list[EquipmentInventory] = field(default_factory=list)

    @property
    def staff_id(self) -> str:
        return self.employee.staff_id

    def draft(self, items: list[PlanItem] | tuple[PlanItem, ...] | None = None) -> RouteDraft:
        return RouteDraft(
            self.employee.staff_id,
            tuple(self.items if items is None else items),
            self.frozen,
            self.earliest_departure,
            self.resume_from_depot_at,
            self.blocked,
            tuple(sorted(self.not_before.items())),
            self.end_at_depot,
            self.require_break,
            self.break_margin,
        )

    def simulate(
        self, items: list[PlanItem] | tuple[PlanItem, ...], day: DayInstance, travel: TravelTimeProvider
    ) -> SimulationResult:
        return simulate_route(self.employee, self.draft(items), day, travel)

    def visit_ids(self) -> list[str]:
        return [i.order_id for i in self.items if isinstance(i, VisitItem)]

    # ------------------------------------------------------------ кеши
    def set_schedule(self, items: list[PlanItem], sim: SimulationResult, day: DayInstance) -> None:
        """Принять допустимое расписание и пересчитать кеши предпроверки."""
        if not sim.feasible:
            raise ValueError("в RouteState можно записать только допустимое расписание")
        self.items = list(items)
        self.sim = sim
        self._rebuild(day)

    def _rebuild(self, day: DayInstance) -> None:
        emp = self.employee
        stops = self.sim.stops
        n_frozen = len(self.frozen)
        chain: list[RouteStop] = []
        for s in stops[n_frozen:]:
            if s.type in (StopType.VISIT, StopType.RESTOCK, StopType.DEPOT_END):
                chain.append(s)
        # остаток АО перед каждым элементом
        inv = emp.equipment_start
        for s in self.frozen:
            if s.type is StopType.VISIT and s.order_id in day.orders:
                inv = inv - day.orders[s.order_id].required_equipment
            elif s.type is StopType.RESTOCK:
                inv = emp.equipment_capacity
        gaps: list[EquipmentInventory] = []
        for item in self.items:
            gaps.append(inv)
            if isinstance(item, VisitItem):
                inv = inv - day.orders[item.order_id].required_equipment
            else:
                inv = emp.equipment_capacity
        gaps.append(inv)
        # floor[p] — остаток непосредственно перед ближайшим пополнением на позиции ≥ p (или в конце)
        floors = [inv] * (len(self.items) + 1)
        cur = inv
        for p in range(len(self.items) - 1, -1, -1):
            if isinstance(self.items[p], RestockItem):
                cur = gaps[p]
            floors[p] = cur
        # forward slack
        push = [0] * len(chain)
        nxt = INF
        ref = day.reference
        for c in range(len(chain) - 1, -1, -1):
            s = chain[c]
            if s.type is StopType.VISIT:
                lat = day.orders[s.order_id].latest_start
            elif s.type is StopType.RESTOCK:
                sc = ref.service_centers[s.location_id]
                lat = sc.opening.end - sc.restock_duration
            else:
                lat = emp.shift.end
            room = min(lat - s.start, nxt)
            push[c] = (s.start - s.arrival) + room
            nxt = push[c]
        self.chain, self.push, self.inv_gap, self.inv_floor = chain, push, gaps, floors

    # ------------------------------------------------------------ предпроверка
    def origin(self) -> tuple[str, Seconds]:
        """Откуда и когда может начаться движение к первому новому элементу."""
        if self.frozen:
            last = self.frozen[-1]
            t = last.end
            loc = last.location_id
        else:
            t = self.employee.shift.start
            loc = self.employee.service_center_id
        if self.earliest_departure is not None:
            t = max(t, self.earliest_departure)
        if not self.frozen and self.resume_from_depot_at is not None:
            t = max(t, self.resume_from_depot_at)
        return loc, t

    def precheck(
        self, order: Order, duration: Seconds, day: DayInstance, travel: TravelTimeProvider
    ) -> list[PositionCheck]:
        """Позиции, прошедшие O(1)-проверки времени и АО, по возрастанию прироста дороги."""
        emp = self.employee
        if not self.chain and self.items:
            return []
        b = order.building_id
        lo = max(order.slot.start, order.registered_at, self.not_before.get(order.order_id, -INF))
        hi = order.latest_start
        need = order.required_equipment
        home = emp.service_center_id
        o_loc, o_t = self.origin()
        n = len(self.items)
        out: list[PositionCheck] = []
        for p in range(n + 1):
            if p == 0:
                prev_loc, prev_end = o_loc, o_t
            else:
                ps = self.chain[p - 1]
                prev_loc, prev_end = ps.location_id, ps.end
            if prev_end > hi:
                break  # дальше по маршруту только позже
            t_in = travel.travel_sec(prev_loc, b)
            if t_in is None:
                continue
            arr = prev_end + t_in
            if p == 0 and self.resume_from_depot_at is not None:
                back = travel.travel_sec(home, b)
                if back is None:
                    continue
                arr = max(arr, self.resume_from_depot_at + back)
            start = max(arr, lo)
            if start > hi:
                continue
            end = start + duration
            if end > emp.shift.end:
                continue
            if p < len(self.chain):
                nxt = self.chain[p]
                next_loc = nxt.location_id
            elif self.end_at_depot:
                nxt, next_loc = None, home
            else:
                nxt, next_loc = None, None
            t_out = travel.travel_sec(b, next_loc) if next_loc is not None else 0
            if t_out is None:
                continue
            old = 0
            if nxt is not None:
                delay = end + t_out - nxt.arrival
                if delay > self.push[p]:
                    continue
                old = travel.travel_sec(prev_loc, next_loc) or 0
            elif next_loc is not None and end + t_out > emp.shift.end:
                # Пустой маршрут: поездки DEPOT_START→DEPOT_END не было.
                continue
            needs_restock = not (self.inv_gap[p].covers(need) and self.inv_floor[p].covers(need))
            if needs_restock and not emp.equipment_capacity.covers(need):
                continue
            out.append(PositionCheck(p, t_in + t_out - old, start, needs_restock))
        out.sort(key=lambda c: (c.needs_restock, c.delta_travel, c.position))
        return out


def build_route_state(
    employee: Employee,
    items: list[PlanItem],
    day: DayInstance,
    travel: TravelTimeProvider,
    **draft_kw: object,
) -> RouteState:
    st = RouteState(employee, list(items), **draft_kw)  # type: ignore[arg-type]
    sim = st.simulate(items, day, travel)
    if not sim.feasible:
        raise ValueError(f"маршрут {employee.staff_id} недопустим: {sim.violation}")
    st.set_schedule(list(items), sim, day)
    return st


@dataclass(slots=True)
class WorkingPlan:
    """Изменяемый план дня, с которым работают оптимизаторы. Публикуется через `to_plan()`."""

    day: DayInstance
    travel: TravelTimeProvider
    routes: dict[str, RouteState]
    unassigned: dict[str, UnassignedReason] = field(default_factory=dict)
    pgp_groups: list[PgpGroup] = field(default_factory=list)
    cancelled: dict[str, CancelledOrder] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    explanations: dict[str, str] = field(default_factory=dict)
    unavailable: dict[str, tuple[Seconds, Seconds | None]] = field(default_factory=dict)
    eligible: dict[str, list[str]] = field(default_factory=dict)
    """order_id → допущенные сотрудники (ADO-04); заполняет ADO-09, обновляет ADO-13."""
    priority: dict[str, float] = field(default_factory=dict)

    def assigned(self) -> dict[str, str]:
        """Все визиты плана, включая начатые (замороженные): для учёта H9."""
        out = {oid: sid for sid, r in self.routes.items() for oid in r.visit_ids()}
        for sid, r in self.routes.items():
            for s in r.frozen:
                if s.type is StopType.VISIT and s.order_id:
                    out[s.order_id] = sid
        return out

    def movable(self) -> dict[str, str]:
        """Только визиты, которые ещё можно переставлять (не начатые)."""
        return {oid: sid for sid, r in self.routes.items() for oid in r.visit_ids()}

    def copy(self) -> WorkingPlan:
        routes = {}
        for sid, r in self.routes.items():
            nr = RouteState(
                r.employee,
                list(r.items),
                frozen=r.frozen,
                earliest_departure=r.earliest_departure,
                resume_from_depot_at=r.resume_from_depot_at,
                blocked=r.blocked,
                not_before=dict(r.not_before),
                end_at_depot=r.end_at_depot,
                require_break=r.require_break,
                break_margin=r.break_margin,
                sim=r.sim,
                chain=r.chain,
                push=r.push,
                inv_gap=r.inv_gap,
                inv_floor=r.inv_floor,
            )
            routes[sid] = nr
        return WorkingPlan(
            self.day,
            self.travel,
            routes,
            dict(self.unassigned),
            [
                PgpGroup(g.group_id, g.access_node_id, list(g.order_ids), g.detected_at, g.action, g.comment)
                for g in self.pgp_groups
            ],
            dict(self.cancelled),
            list(self.alerts),
            dict(self.explanations),
            dict(self.unavailable),
            {k: list(v) for k, v in self.eligible.items()},
            dict(self.priority),
        )

    def to_plan(
        self,
        version: int = 1,
        as_of: Seconds | None = None,
        based_on_event_id: str | None = None,
        computation_ms: int | None = None,
    ) -> Plan:
        routes: dict[str, Route] = {}
        for sid in sorted(self.routes):
            r = self.routes[sid]
            stops = []
            for s in r.sim.stops:
                if s.type is StopType.VISIT and s.order_id in self.explanations and s.explanation is None:
                    s = replace(s, explanation=self.explanations[s.order_id])
                stops.append(s)
            un = self.unavailable.get(sid)
            routes[sid] = Route(
                sid,
                r.employee.service_center_id,
                stops,
                RouteStatus.UNAVAILABLE if un else RouteStatus.ACTIVE,
                un[0] if un else None,
                un[1] if un else None,
            )
        return Plan(
            self.day.date,
            version,
            self.day.now if as_of is None else as_of,
            routes,
            dict(self.unassigned),
            [
                PgpGroup(g.group_id, g.access_node_id, list(g.order_ids), g.detected_at, g.action, g.comment)
                for g in self.pgp_groups
            ],
            dict(self.cancelled),
            list(self.alerts),
            based_on_event_id,
            computation_ms,
        )
