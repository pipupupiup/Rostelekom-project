"""ADO-07 — симуляция маршрута: единственный конструктор расписания и проверка H1–H8.

`simulate_route(employee, draft, day, travel)` последовательно рассчитывает прибытие, ожидание,
начало и окончание каждой остановки и возвращает `SimulationResult` с расписанием либо с
конкретной причиной недопустимости (`Violation` с кодом H* и подсказкой `UnassignedReasonCode`).
Вход не изменяется; сетевых запросов и БД нет — только `TravelTimeProvider` (O(1)).

Правила (п. 3–4 спецификации):
* старт в своём СЦ: `DEPOT_START` ≥ начала смены; выезд сдвигается на время ожидания у первого
  визита (сотрудник не стоит у подъезда без дела, дорога не меняется);
* визит: `start = max(arrival, slot.start, not_before)`, `start ≤ latest_start` (H4; для
  инсталляции `latest_start = slot.start + 10 мин`), `end = start + норматив` (H5);
* окончание после конца слота допустимо — важно только начало (H4);
* АО списывается в момент начала визита, остаток ≥ 0 (H7); `RESTOCK` — в СЦ, открытом на всём
  интервале, длительность = `restock_duration_min`, рюкзак доводится до `equipment_capacity` (H8);
* перерыв (H3) размещается симулятором: ровно один при наличии визитов, на месте предыдущей
  остановки, начало в окне. Жадное правило: перерыв берётся перед очередной остановкой, если
  (а) это бесплатно — он укладывается в ожидание и не сдвигает начало, или (б) иначе он уже не
  поместится (окончание следующей работы позже конца окна). Если жадное размещение привело к
  нарушению, перебираются другие позиции перерыва (только при неудаче, чтобы не замедлять
  основной путь);
* возвращение в свой СЦ не позже конца смены (H2);
* контрольный срок не является H-ограничением: визиты с `end > deadline` перечисляются в
  `deadline_misses` и учитываются метриками (ADO-11).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ado.domain.contracts import TravelTimeProvider
from ado.domain.enums import ConstraintCode as C
from ado.domain.enums import StopType
from ado.domain.enums import UnassignedReasonCode as R
from ado.domain.equipment import EquipmentInventory
from ado.domain.models import (
    DayInstance,
    Employee,
    RestockItem,
    RouteDraft,
    RouteStop,
    SimulationResult,
    TimeWindow,
    Violation,
    VisitItem,
)
from ado.domain.timebase import Seconds

NEG_INF = -(10**9)


@dataclass(slots=True)
class _Outcome:
    result: SimulationResult
    fail_index: int | None = None
    """Индекс элемента `items`, на котором маршрут стал недопустимым (len(items) — хвост)."""
    break_pos: int | None = None
    """Перед каким элементом поставлен перерыв (None — в префиксе или не ставился)."""


def _fail(v: Violation, stops: list[RouteStop], k: int | None, bpos: int | None) -> _Outcome:
    return _Outcome(SimulationResult(False, tuple(stops), (v,)), k, bpos)


def _blocked_at(t: Seconds, blocked: tuple[TimeWindow, ...]) -> bool:
    if not blocked:
        return False
    return any(w.start <= t < w.end for w in blocked)


def _run(
    emp: Employee,
    draft: RouteDraft,
    day: DayInstance,
    travel: TravelTimeProvider,
    forced_break: int | None,
) -> _Outcome:
    ref = day.reference
    orders = day.orders
    shift = emp.shift
    bw = shift.break_window
    bdur = shift.break_duration
    home = emp.service_center_id
    items = draft.items
    not_before = dict(draft.not_before)

    stops: list[RouteStop] = []
    inv = emp.equipment_start
    break_taken = False
    has_visit = False
    travel_total = dist_total = work_total = wait_total = 0
    misses: list[str] = []
    # загруженность дорог (доп. задание): время в пути × коэффициент часа отправления, ≥ 1 (D38)
    factor_at = getattr(travel, "factor_at", None)

    def eff(base: int, at: int) -> int:
        return base if factor_at is None or not base else math.ceil(base * factor_at(at))

    if draft.staff_id != emp.staff_id:
        return _fail(
            Violation(C.STRUCTURE, f"черновик {draft.staff_id} ≠ сотрудник {emp.staff_id}"), [], 0, None
        )

    # ---- замороженный префикс: восстановить состояние
    for i, s in enumerate(draft.frozen_prefix):
        stops.append(s)
        if s.type is StopType.VISIT:
            o = orders.get(s.order_id or "")
            if o is None:
                return _fail(
                    Violation(C.STRUCTURE, f"наряд {s.order_id} префикса не найден", i), stops, 0, None
                )
            inv = inv - o.required_equipment
            has_visit = True
            work_total += s.end - s.start
        elif s.type is StopType.RESTOCK:
            inv = emp.equipment_capacity
        elif s.type is StopType.BREAK:
            break_taken = True
        travel_total += s.travel_sec
        dist_total += s.distance_m
    if draft.frozen_prefix:
        last = draft.frozen_prefix[-1]
        if last.type is StopType.DEPOT_END:
            if items:
                return _fail(Violation(C.STRUCTURE, "маршрут уже завершён в СЦ"), stops, 0, None)
            return _Outcome(SimulationResult(True, tuple(stops), (), travel_total, dist_total, work_total, 0))
        loc, t = last.location_id, last.end
        depot_start_idx = None
    else:
        if not items:
            return _Outcome(SimulationResult(True, ()))  # пустой маршрут (A2)
        loc = home
        t = shift.start
        if draft.earliest_departure is not None:
            t = max(t, draft.earliest_departure)
        if draft.resume_from_depot_at is not None:
            t = max(t, draft.resume_from_depot_at)  # ещё не выехал: выезд после возвращения доступности
        if _blocked_at(t, draft.blocked):
            return _fail(
                Violation(C.STRUCTURE, "выезд в период недоступности", unassigned_hint=R.STAFF_UNAVAILABLE),
                stops,
                0,
                None,
            )
        stops.append(RouteStop(StopType.DEPOT_START, home, t, t, t, equipment_after=inv))
        depot_start_idx = 0
    if draft.earliest_departure is not None:
        t = max(t, draft.earliest_departure)
    resume_at = draft.resume_from_depot_at
    break_pos: int | None = None

    def place_break(at: Seconds, k: int) -> None:
        nonlocal t, break_taken, break_pos
        bs = max(at, bw.start)
        stops.append(RouteStop(StopType.BREAK, loc, at, bs, bs + bdur, travel_sec=0, equipment_after=inv))
        t = bs + bdur
        break_taken = True
        break_pos = k

    # ---- элементы маршрута
    for k, item in enumerate(items):
        if isinstance(item, VisitItem):
            o = orders.get(item.order_id)
            if o is None:
                return _fail(Violation(C.STRUCTURE, f"наряд {item.order_id} не найден"), stops, k, break_pos)
            wt = ref.work_types[o.work_type_code]
            if not (emp.profiles & wt.profiles):
                return _fail(
                    Violation(
                        C.H1_QUALIFICATION,
                        f"профиль не допускает {wt.code}",
                        order_id=o.order_id,
                        unassigned_hint=R.NO_QUALIFIED_STAFF,
                    ),
                    stops,
                    k,
                    break_pos,
                )
            if o.technology not in emp.technologies:
                return _fail(
                    Violation(
                        C.H1_QUALIFICATION,
                        f"нет допуска к технологии {o.technology}",
                        order_id=o.order_id,
                        unassigned_hint=R.NO_QUALIFIED_STAFF,
                    ),
                    stops,
                    k,
                    break_pos,
                )
            if o.requires_height and not emp.height_permit:
                return _fail(
                    Violation(
                        C.H1_QUALIFICATION,
                        "нет допуска к работам на высоте",
                        order_id=o.order_id,
                        unassigned_hint=R.NO_QUALIFIED_STAFF,
                    ),
                    stops,
                    k,
                    break_pos,
                )
            target, dur = o.building_id, wt.duration
            lo = max(o.slot.start, o.registered_at, not_before.get(o.order_id, NEG_INF))
            hi = o.latest_start
            stype = StopType.VISIT
            oid: str | None = o.order_id
        elif isinstance(item, RestockItem):
            sc = ref.service_centers.get(item.service_center_id)
            if sc is None:
                return _fail(
                    Violation(C.H8_SERVICE_CENTER, f"СЦ {item.service_center_id} не найден"),
                    stops,
                    k,
                    break_pos,
                )
            target, dur = sc.service_center_id, sc.restock_duration
            lo, hi = sc.opening.start, sc.opening.end - sc.restock_duration
            stype, oid, o = StopType.RESTOCK, None, None
        else:  # pragma: no cover — защита от неверного типа
            return _fail(Violation(C.STRUCTURE, f"неизвестный элемент {item!r}"), stops, k, break_pos)

        try:
            tt = travel.travel_sec(loc, target)
            dd = travel.distance_m(loc, target)
        except KeyError as e:
            return _fail(
                Violation(C.H6_TRAVEL, f"точки {e} нет в матрице", order_id=oid, unassigned_hint=R.OTHER),
                stops,
                k,
                break_pos,
            )
        if tt is None:
            return _fail(
                Violation(
                    C.H6_TRAVEL,
                    f"нет дорожной связи {loc} → {target}",
                    order_id=oid,
                    unassigned_hint=R.SLOT_UNREACHABLE,
                ),
                stops,
                k,
                break_pos,
            )

        base_tt = tt
        tt = eff(base_tt, t)
        # перерыв перед элементом
        if draft.require_break and not break_taken and (forced_break is None or forced_break == k):
            bs = max(t, bw.start)
            if forced_break == k:
                if bs > bw.end:
                    return _fail(
                        Violation(
                            C.H3_BREAK, "перерыв не помещается в окно", unassigned_hint=R.NO_SHIFT_TIME
                        ),
                        stops,
                        k,
                        break_pos,
                    )
                place_break(t, k)
            elif bs <= bw.end and (has_visit or stype is StopType.VISIT):
                s_nb = max(t + tt, lo)
                s_b = max(bs + bdur + tt, lo)
                robust = draft.break_margin > 0 and s_nb + dur + draft.break_margin > bw.end and s_b <= hi
                if s_b <= s_nb or s_nb + dur > bw.end or robust:
                    place_break(t, k)
            elif bs > bw.end and has_visit:
                return _fail(
                    Violation(C.H3_BREAK, "окно перерыва пропущено", unassigned_hint=R.NO_SHIFT_TIME),
                    stops,
                    k,
                    break_pos,
                )

        tt = eff(base_tt, t)  # отправление могло сдвинуться перерывом
        arrival = t + tt
        if resume_at is not None:
            try:
                back = travel.travel_sec(home, target)
            except KeyError:
                back = None
            if back is not None:
                back = eff(back, resume_at)
            if back is None:
                return _fail(
                    Violation(
                        C.H6_TRAVEL,
                        f"нет дорожной связи {home} → {target}",
                        order_id=oid,
                        unassigned_hint=R.SLOT_UNREACHABLE,
                    ),
                    stops,
                    k,
                    break_pos,
                )
            arrival = max(arrival, resume_at + back)
            resume_at = None
        start = max(arrival, lo)
        if _blocked_at(start, draft.blocked):
            return _fail(
                Violation(
                    C.STRUCTURE,
                    "остановка в период недоступности сотрудника",
                    order_id=oid,
                    unassigned_hint=R.STAFF_UNAVAILABLE,
                ),
                stops,
                k,
                break_pos,
            )
        if start > hi:
            if stype is StopType.VISIT:
                late = (start - hi + 59) // 60
                v = Violation(
                    C.H4_SLOT,
                    f"начало визита позже latest_start на {late} мин",
                    order_id=oid,
                    unassigned_hint=R.SLOT_UNREACHABLE,
                )
            else:
                v = Violation(
                    C.H8_SERVICE_CENTER, f"СЦ {target} закрыт для пополнения", unassigned_hint=R.NO_EQUIPMENT
                )
            return _fail(v, stops, k, break_pos)
        end = start + dur
        if end > shift.end:
            return _fail(
                Violation(
                    C.H2_SHIFT, "работы выходят за конец смены", order_id=oid, unassigned_hint=R.NO_SHIFT_TIME
                ),
                stops,
                k,
                break_pos,
            )
        if o is not None:
            need = o.required_equipment
            if not inv.covers(need):
                return _fail(
                    Violation(
                        C.H7_EQUIPMENT,
                        "в рюкзаке не хватает АО",
                        order_id=oid,
                        unassigned_hint=R.NO_EQUIPMENT,
                    ),
                    stops,
                    k,
                    break_pos,
                )
            inv = inv - need
            has_visit = True
            work_total += dur
            if end > o.deadline:
                misses.append(o.order_id)
        else:
            inv = emp.equipment_capacity
        stops.append(RouteStop(stype, target, arrival, start, end, oid, tt, dd or 0, inv))
        travel_total += tt
        dist_total += dd or 0
        wait_total += start - arrival
        loc, t = target, end

    # ---- хвост: перерыв и возвращение
    n = len(items)
    if has_visit and not break_taken and draft.require_break:
        bs = max(t, bw.start)
        if (forced_break is None or forced_break == n) and bs <= bw.end:
            place_break(t, n)
        else:
            return _fail(
                Violation(C.H3_BREAK, "нет места для обеденного перерыва", unassigned_hint=R.NO_SHIFT_TIME),
                stops,
                n,
                break_pos,
            )
    if draft.end_at_depot:
        try:
            tt = travel.travel_sec(loc, home)
            dd = travel.distance_m(loc, home)
        except KeyError as e:
            return _fail(Violation(C.H6_TRAVEL, f"точки {e} нет в матрице"), stops, n, break_pos)
        if tt is None:
            return _fail(
                Violation(C.H6_TRAVEL, f"нет дорожной связи {loc} → {home}", unassigned_hint=R.NO_SHIFT_TIME),
                stops,
                n,
                break_pos,
            )
        tt = eff(tt, t)
        arr = t + tt
        if resume_at is not None:
            arr = max(arr, resume_at)
        if arr > shift.end:
            late = (arr - shift.end + 59) // 60
            return _fail(
                Violation(
                    C.H2_SHIFT,
                    f"возвращение в СЦ позже конца смены на {late} мин",
                    unassigned_hint=R.NO_SHIFT_TIME,
                ),
                stops,
                n,
                break_pos,
            )
        stops.append(RouteStop(StopType.DEPOT_END, home, arr, arr, arr, None, tt, dd or 0, inv))
        travel_total += tt
        dist_total += dd or 0

    # ---- выезд без ожидания у первого объекта
    if depot_start_idx is not None and len(stops) > 1 and stops[1].type in (StopType.VISIT, StopType.RESTOCK):
        first = stops[1]
        slack = first.start - first.arrival
        if slack > 0:
            d0 = stops[0]
            new_t = d0.start + slack
            stops[0] = RouteStop(
                StopType.DEPOT_START, d0.location_id, new_t, new_t, new_t, equipment_after=d0.equipment_after
            )
            stops[1] = RouteStop(
                first.type,
                first.location_id,
                first.start,
                first.start,
                first.end,
                first.order_id,
                first.travel_sec,
                first.distance_m,
                first.equipment_after,
            )
            wait_total -= slack

    res = SimulationResult(
        True, tuple(stops), (), travel_total, dist_total, work_total, wait_total, tuple(misses)
    )
    return _Outcome(res, None, break_pos)


def simulate_route(
    employee: Employee, draft: RouteDraft, day: DayInstance, travel: TravelTimeProvider
) -> SimulationResult:
    out = _run(employee, draft, day, travel, None)
    if out.result.feasible:
        return out.result
    v = out.result.violations[0]
    n = len(draft.items)
    k = out.fail_index if out.fail_index is not None else n
    candidates: list[int] = []
    if v.code is C.H3_BREAK:
        candidates = list(range(min(k, n), -1, -1))  # перерыв раньше
    elif (
        v.code in (C.H4_SLOT, C.H2_SHIFT, C.H8_SERVICE_CENTER)
        and out.break_pos is not None
        and out.break_pos <= k
    ):
        candidates = list(range(k + 1, n + 1))  # перерыв позже точки отказа
    for p in candidates:
        alt = _run(employee, draft, day, travel, p)
        if alt.result.feasible:
            return alt.result
    return out.result


class RouteSimulator:
    """Реализация протокола `ado.domain.contracts.RouteSimulator`."""

    def simulate(
        self, employee: Employee, draft: RouteDraft, day: DayInstance, travel: TravelTimeProvider
    ) -> SimulationResult:
        return simulate_route(employee, draft, day, travel)


def inventory_after(stops: tuple[RouteStop, ...], start: EquipmentInventory) -> EquipmentInventory:
    """Остаток АО после последней остановки (по `equipment_after`)."""
    for s in reversed(stops):
        if s.equipment_after is not None:
            return s.equipment_after
    return start
