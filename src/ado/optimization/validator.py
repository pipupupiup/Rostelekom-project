"""Независимый валидатор плана H1–H10 (часть ADO-11).

Проверяет **готовое** расписание так же, как валидатор организаторов: по единой матрице, без
пересчёта и без использования симулятора. Это сознательное дублирование логики (уточнение D6):
симулятор — единственный *конструктор* расписаний, валидатор — независимый *оракул*, который
ловит ошибки симулятора и интеграции. Он вызывается перед публикацией версии плана, в тестах и
в бенчмарке, но не во внутренних циклах оптимизации.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from ado.domain.contracts import TravelTimeProvider
from ado.domain.enums import ConstraintCode as C
from ado.domain.enums import RouteStatus, StopType
from ado.domain.models import DayInstance, Plan, Route
from ado.domain.timebase import Seconds


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: C
    message: str
    staff_id: str | None = None
    order_id: str | None = None
    seq: int | None = None

    def __str__(self) -> str:
        where = "/".join(x for x in (self.staff_id, self.order_id) if x)
        return (
            f"{self.code} {where} #{self.seq}: {self.message}"
            if self.seq
            else f"{self.code} {where}: {self.message}"
        )


def _route_issues(
    route: Route,
    day: DayInstance,
    travel: TravelTimeProvider,
    end_overrides: Mapping[str, Seconds],
    not_before: Mapping[str, Seconds],
) -> list[ValidationIssue]:
    out: list[ValidationIssue] = []
    ref = day.reference
    emp = ref.employees.get(route.staff_id)
    sid = route.staff_id
    if emp is None:
        return [ValidationIssue(C.STRUCTURE, "сотрудник не найден", sid)]
    stops = route.stops
    if not stops:
        return out

    def add(code: C, msg: str, i: int, oid: str | None = None) -> None:
        out.append(ValidationIssue(code, msg, sid, oid, i + 1))

    shift = emp.shift
    home = emp.service_center_id
    if route.service_center_id != home:
        add(C.H8_SERVICE_CENTER, "service_center_id маршрута не совпадает с СЦ сотрудника", 0)
    if stops[0].type is not StopType.DEPOT_START:
        add(C.STRUCTURE, "первая остановка не DEPOT_START", 0)
    if stops[-1].type is not StopType.DEPOT_END and route.status is not RouteStatus.UNAVAILABLE:
        add(C.STRUCTURE, "последняя остановка не DEPOT_END", len(stops) - 1)
    inv = emp.equipment_start
    visits = 0
    breaks = []
    for i, s in enumerate(stops):
        oid = s.order_id
        if s.start < shift.start or s.end > shift.end:
            add(C.H2_SHIFT, "остановка вне смены", i, oid)
        if s.start < s.arrival or s.end < s.start:
            add(C.H6_TRAVEL, "нарушен порядок arrival ≤ start ≤ end", i, oid)
        if i > 0:
            prev = stops[i - 1]
            try:
                tt = travel.travel_sec(prev.location_id, s.location_id)
            except KeyError:
                tt = None
            if tt is None:
                add(C.H6_TRAVEL, f"нет дорожной связи {prev.location_id} → {s.location_id}", i, oid)
            elif s.arrival < prev.end + tt:
                add(C.H6_TRAVEL, f"прибытие раньше возможного на {prev.end + tt - s.arrival} с", i, oid)
        if route.unavailable_from is not None:
            until = route.unavailable_until if route.unavailable_until is not None else shift.end + 1
            if route.unavailable_from <= s.start < until and s.start >= route.unavailable_from:
                add(C.H10_FREEZE, "остановка начинается в период недоступности", i, oid)
        if s.type in (StopType.DEPOT_START, StopType.DEPOT_END):
            if not (s.arrival == s.start == s.end):
                add(C.STRUCTURE, "у DEPOT_* должно быть arrival = start = end", i)
            if s.location_id != home:
                add(C.H8_SERVICE_CENTER, "DEPOT_* не в СЦ сотрудника", i)
            if s.type is StopType.DEPOT_START and i != 0:
                add(C.STRUCTURE, "DEPOT_START не первая", i)
            if s.type is StopType.DEPOT_END and i != len(stops) - 1:
                add(C.STRUCTURE, "DEPOT_END не последняя", i)
        elif s.type is StopType.VISIT:
            visits += 1
            o = day.orders.get(oid or "")
            if o is None:
                add(C.H9_ACCOUNTING, "визит по неизвестному наряду", i, oid)
                continue
            wt = ref.work_types[o.work_type_code]
            if s.location_id != o.building_id:
                add(C.STRUCTURE, "location_id визита ≠ building_id наряда", i, oid)
            if (
                not (emp.profiles & wt.profiles)
                or o.technology not in emp.technologies
                or (o.requires_height and not emp.height_permit)
            ):
                add(C.H1_QUALIFICATION, "нет допуска", i, oid)
            if not (o.slot.start <= s.start <= o.latest_start):
                add(C.H4_SLOT, "начало визита вне [slot.start, latest_start]", i, oid)
            expected_end = end_overrides.get(o.order_id, s.start + wt.duration)
            if s.end != expected_end:
                add(C.H5_DURATION, f"длительность {(s.end - s.start) // 60} мин ≠ нормативу", i, oid)
            if o.order_id in not_before and s.start < not_before[o.order_id]:
                add(C.H10_FREEZE, "визит по NEW_ORDER раньше события", i, oid)
            inv = inv - o.required_equipment
            if not inv.is_non_negative():
                add(C.H7_EQUIPMENT, "остаток АО стал отрицательным", i, oid)
        elif s.type is StopType.RESTOCK:
            sc = ref.service_centers.get(s.location_id)
            if sc is None:
                add(C.H8_SERVICE_CENTER, "RESTOCK не в СЦ", i)
                continue
            if not (sc.opening.start <= s.start and s.end <= sc.opening.end):
                add(C.H8_SERVICE_CENTER, "СЦ закрыт на интервале пополнения", i)
            if s.end - s.start < sc.restock_duration:
                add(C.H8_SERVICE_CENTER, "пополнение короче restock_duration_min", i)
            inv = emp.equipment_capacity
        elif s.type is StopType.BREAK:
            breaks.append(i)
            if i == 0 or s.location_id != stops[i - 1].location_id:
                add(C.H3_BREAK, "перерыв не на месте предыдущей остановки", i)
            if not shift.break_window.contains(s.start):
                add(C.H3_BREAK, "начало перерыва вне окна", i)
            if s.end - s.start < shift.break_duration:
                add(C.H3_BREAK, "перерыв короче нормы", i)
    if visits and len(breaks) != 1 and not (route.status is RouteStatus.UNAVAILABLE and not breaks):  # A9
        out.append(ValidationIssue(C.H3_BREAK, f"перерывов {len(breaks)}, нужен ровно один", sid))
    return out


def validate_plan(
    plan: Plan,
    day: DayInstance,
    travel: TravelTimeProvider,
    *,
    previous: Plan | None = None,
    end_overrides: Mapping[str, Seconds] | None = None,
    not_before: Mapping[str, Seconds] | None = None,
) -> list[ValidationIssue]:
    """Все нарушения H1–H10 версии плана. `previous` — для проверки заморозки (H10)."""
    end_overrides = end_overrides or {}
    not_before = not_before or {}
    issues: list[ValidationIssue] = []
    active = {sid for sid in day.reference.employees if sid not in day.absent_staff_ids}
    missing_routes = sorted(active - set(plan.routes))
    if missing_routes:
        issues.append(ValidationIssue(C.STRUCTURE, f"нет маршрутов для {missing_routes}"))
    for sid in sorted(plan.routes):
        route = plan.routes[sid]
        if sid in day.absent_staff_ids and route.stops:
            issues.append(ValidationIssue(C.STRUCTURE, "маршрут у отсутствующего сотрудника", sid))
        issues.extend(_route_issues(route, day, travel, end_overrides, not_before))

    # H9 — ровно одно место для каждого наряда
    places: Counter[str] = Counter()
    for route in plan.routes.values():
        places.update(s.order_id for s in route.stops if s.type is StopType.VISIT and s.order_id)
    places.update(list(plan.unassigned))
    for g in plan.pgp_groups:
        places.update(g.order_ids)
    places.update(list(plan.cancelled))
    for oid in sorted(day.orders):
        n = places.get(oid, 0)
        if n != 1:
            issues.append(ValidationIssue(C.H9_ACCOUNTING, f"наряд учтён {n} раз", order_id=oid))
    for oid in sorted(set(places) - set(day.orders)):
        issues.append(ValidationIssue(C.H9_ACCOUNTING, "неизвестный наряд в плане", order_id=oid))

    # H10 — заморозка начатых остановок
    if previous is not None:
        now = plan.as_of
        for sid, prev_route in previous.routes.items():
            started = [s for s in prev_route.stops if s.start < now]
            cur = plan.routes.get(sid)
            cur_stops = cur.stops if cur else []
            for i, s in enumerate(started):
                c = cur_stops[i] if i < len(cur_stops) else None
                same = c is not None and (c.type, c.location_id, c.order_id, c.arrival, c.start) == (
                    s.type,
                    s.location_id,
                    s.order_id,
                    s.arrival,
                    s.start,
                )
                if not same:
                    issues.append(
                        ValidationIssue(C.H10_FREEZE, "изменена начатая остановка", sid, s.order_id, i + 1)
                    )
                elif c.end != s.end and (s.order_id or "") not in end_overrides:
                    issues.append(
                        ValidationIssue(
                            C.H10_FREEZE, "изменён end начатой остановки без события", sid, s.order_id, i + 1
                        )
                    )
    return issues
