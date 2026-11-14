"""ADO-07: обязательные сценарии задания + структурные инварианты расписания."""

from __future__ import annotations

import itertools

from ado.domain.enums import ConstraintCode as C
from ado.domain.enums import Profile, StopType, Technology
from ado.domain.enums import UnassignedReasonCode as R
from ado.domain.enums import WorkTypeCode as W
from ado.domain.equipment import EquipmentInventory as Eq
from ado.domain.models import RestockItem, RouteDraft, RouteStop, TimeWindow, VisitItem
from ado.optimization.simulator import simulate_route
from tests import dom as D

hm = D.hm
EMP = D.employee("ST-001", shift=(hm(9), hm(18)), break_window=(hm(12), hm(15)), start=Eq(3, 3, 3))
PTS = ["SC-01", "SC-02", "B-00001", "B-00002", "B-00003"]


def run(orders, items, emp=EMP, trips=None, default=20 * 60, scs=None, **draft_kw):
    ref = D.reference([emp], scs=scs or {"SC-01": (hm(8), hm(21)), "SC-02": (hm(10), hm(12))})
    day = D.day(ref, orders)
    travel = D.matrix(PTS, trips, default)
    draft = RouteDraft(emp.staff_id, tuple(items), **draft_kw)
    res = simulate_route(emp, draft, day, travel)
    if res.feasible:
        assert_schedule_invariants(res, emp, day, travel, len(draft.frozen_prefix))
    return res


def assert_schedule_invariants(res, emp, day, travel, n_frozen=0) -> None:
    stops = res.stops
    if not stops:
        return
    assert stops[0].type is StopType.DEPOT_START and stops[0].start >= emp.shift.start
    assert stops[-1].type is StopType.DEPOT_END and stops[-1].arrival <= emp.shift.end
    breaks = [s for s in stops if s.type is StopType.BREAK]
    if any(s.type is StopType.VISIT for s in stops):
        assert len(breaks) == 1
        b = breaks[0]
        assert emp.shift.break_window.contains(b.start) and b.end - b.start >= emp.shift.break_duration
    for prev, cur in itertools.pairwise(stops):
        if cur.type is StopType.BREAK:
            assert cur.location_id == prev.location_id and cur.travel_sec == 0
        assert cur.arrival >= prev.end + (travel.travel_sec(prev.location_id, cur.location_id) or 0)
        assert cur.start >= cur.arrival and cur.end >= cur.start
    for s in stops[n_frozen:]:
        if s.type is StopType.VISIT:
            o = day.orders[s.order_id]
            assert o.slot.start <= s.start <= o.latest_start
            assert s.end - s.start == day.reference.work_types[o.work_type_code].duration


def visit_stop(res, oid):
    return next(s for s in res.stops if s.order_id == oid)


# 1. Прибытие точно в начале слота
def test_arrival_exactly_at_slot_start() -> None:
    o = D.order("A", slot=(hm(9, 20), hm(11, 20)))
    res = run([o], [VisitItem("A")])
    s = visit_stop(res, "A")
    assert res.feasible and s.arrival == s.start == hm(9, 20)
    assert res.stops[0].start == hm(9)


def test_departure_shifted_instead_of_waiting() -> None:
    res = run([D.order("A", slot=(hm(10), hm(12)))], [VisitItem("A")])
    assert res.stops[0].start == hm(9, 40) and visit_stop(res, "A").arrival == hm(10)
    assert res.wait_sec == 0


# 2. Инсталляция: +10 мин допустимо, +11 — нет
def test_installation_10_and_11_minutes_late() -> None:
    inst = D.installer("ST-101", shift=(hm(8, 30), hm(17, 30)), break_window=(hm(12), hm(15)))
    o = D.order("I", W.INST_PON_MKD, slot=(hm(9), hm(11)), equipment=Eq(1, 1, 0))
    assert o.latest_start == hm(9, 10)
    ok = run([o], [VisitItem("I")], emp=inst, default=40 * 60)  # 8:30 + 40 = 9:10
    assert ok.feasible and visit_stop(ok, "I").start == hm(9, 10)
    late = run([o], [VisitItem("I")], emp=inst, default=41 * 60)  # 9:11
    assert not late.feasible
    v = late.violation
    assert v.code is C.H4_SLOT and v.unassigned_hint is R.SLOT_UNREACHABLE and v.order_id == "I"
    assert "1 мин" in v.message


# 3. Раннее прибытие и ожидание
def test_early_arrival_waits_for_slot() -> None:
    a = D.order("A", slot=(hm(9, 20), hm(11, 20)))
    b = D.order("B", building="B-00002", slot=(hm(13), hm(15)))
    res = run([a, b], [VisitItem("A"), VisitItem("B")])
    sb = visit_stop(res, "B")
    assert res.feasible and sb.arrival == hm(10, 40) and sb.start == hm(13)
    assert res.wait_sec == hm(2, 20)


# 4. Работа заканчивается после окончания слота, но начинается вовремя
def test_work_may_end_after_slot() -> None:
    a = D.order("A", slot=(hm(9), hm(10)))
    b = D.order("B", building="B-00002", slot=(hm(9), hm(10, 40)))
    res = run([a, b], [VisitItem("A"), VisitItem("B")])
    sb = visit_stop(res, "B")
    assert res.feasible and sb.start == hm(10, 40) and sb.end == hm(11, 40) > b.slot.end


# 5. Смена заканчивается до возвращения в СЦ
def test_shift_ends_before_return() -> None:
    o = D.order("A", slot=(hm(16, 30), hm(17, 30)))
    ok = run([o], [VisitItem("A")])
    assert ok.feasible and ok.stops[-1].arrival == hm(17, 50)
    late = run([D.order("A", slot=(hm(16, 50), hm(17, 30)))], [VisitItem("A")])
    assert not late.feasible and late.violation.code is C.H2_SHIFT
    assert late.violation.unassigned_hint is R.NO_SHIFT_TIME and "10 мин" in late.violation.message


# 6. Недостаток оборудования
def test_not_enough_equipment() -> None:
    res = run([D.order("A", equipment=Eq(4, 0, 0))], [VisitItem("A")])
    assert not res.feasible and res.violation.code is C.H7_EQUIPMENT
    assert res.violation.unassigned_hint is R.NO_EQUIPMENT


def test_equipment_tracked_per_type_across_visits() -> None:
    a = D.order("A", equipment=Eq(2, 1, 0))
    b = D.order("B", building="B-00002", slot=(hm(11), hm(13)), equipment=Eq(1, 3, 0))
    res = run([a, b], [VisitItem("A"), VisitItem("B")])
    assert not res.feasible and res.violation.order_id == "B"  # ROUTER: 3 − 1 = 2 < 3


# 7. Пополнение в СЦ (и часы работы СЦ)
def test_restock_refills_to_capacity() -> None:
    o = D.order("A", slot=(hm(10), hm(12)), equipment=Eq(5, 0, 0))
    res = run([o], [RestockItem("SC-01"), VisitItem("A")])
    assert res.feasible
    r = next(s for s in res.stops if s.type is StopType.RESTOCK)
    assert r.end - r.start == 15 * 60 and r.equipment_after == Eq(6, 6, 6)
    assert visit_stop(res, "A").equipment_after == Eq(1, 6, 6)


def test_restock_waits_for_opening_and_fails_when_closed() -> None:
    o = D.order("A", slot=(hm(10), hm(13)), equipment=Eq(5, 0, 0))
    res = run([o], [RestockItem("SC-02"), VisitItem("A")])  # SC-02 открыт 10:00–12:00
    r = next(s for s in res.stops if s.type is StopType.RESTOCK)
    assert res.feasible and r.start == hm(10)
    late = D.order("A", slot=(hm(11, 50), hm(13)), equipment=Eq(5, 0, 0))
    b = D.order("B", slot=(hm(11), hm(11, 10)))
    res2 = run([late, b], [VisitItem("B"), RestockItem("SC-02"), VisitItem("A")])  # к SC-02 в 12:20
    assert not res2.feasible and res2.violation.code is C.H8_SERVICE_CENTER


# 8. Несовместимая технология / 9. нет допуска к высоте
def test_incompatible_technology() -> None:
    emp = D.employee("ST-001", techs=(Technology.PON,))
    res = run([D.order("A", tech=Technology.XDSL)], [VisitItem("A")], emp=emp)
    assert not res.feasible and res.violation.code is C.H1_QUALIFICATION
    assert res.violation.unassigned_hint is R.NO_QUALIFIED_STAFF


def test_no_height_permit_and_wrong_profile() -> None:
    inst = D.installer("ST-101", height=False)
    vls = D.order("V", W.INST_PRIVATE_VLS, slot=(hm(10), hm(12)), height=True)
    res = run([vls], [VisitItem("V")], emp=inst)
    assert not res.feasible and "высоте" in res.violation.message
    res2 = run([D.order("R")], [VisitItem("R")], emp=D.installer("ST-102"))
    assert not res2.feasible and "профиль" in res2.violation.message


# 10. Обед и пересечение с работой
def test_break_never_overlaps_work_and_starts_in_window() -> None:
    a = D.order("A", slot=(hm(11), hm(11)))  # строго 11:00–12:00
    b = D.order("B", building="B-00002", slot=(hm(13), hm(15)))
    res = run([a, b], [VisitItem("A"), VisitItem("B")])
    br = next(s for s in res.stops if s.type is StopType.BREAK)
    # перерыв до B задержал бы его на 20 мин — симулятор переносит перерыв после B (14:00, окно до 15:00)
    assert res.feasible and visit_stop(res, "B").start == hm(13)
    assert br.start == hm(14) and br.location_id == "B-00002"


def test_break_in_waiting_time_is_free() -> None:
    a = D.order("A", slot=(hm(9, 20), hm(9, 20)))
    b = D.order("B", building="B-00002", slot=(hm(14), hm(16)))
    res = run([a, b], [VisitItem("A"), VisitItem("B")])
    br = next(s for s in res.stops if s.type is StopType.BREAK)
    assert br.start == hm(12) and visit_stop(res, "B").start == hm(14)


def test_break_cannot_fit_between_tight_visits() -> None:
    emp = D.employee("ST-001", break_window=(hm(12), hm(12, 30)))
    a = D.order("A", slot=(hm(11), hm(13)), latest=hm(11))
    b = D.order("B", building="B-00002", slot=(hm(12, 20), hm(14)), latest=hm(12, 20))
    res = run([a, b], [VisitItem("A"), VisitItem("B")], emp=emp)
    assert not res.feasible and res.violation.code in (C.H3_BREAK, C.H4_SLOT)


def test_break_fallback_moves_break_before_late_first_visit() -> None:
    emp = D.employee("ST-001", shift=(hm(12), hm(21)), break_window=(hm(15), hm(18)))
    o = D.order("A", slot=(hm(18, 30), hm(20)))  # визит после окна перерыва → перерыв до него
    res = run([o], [VisitItem("A")], emp=emp)
    br = next(s for s in res.stops if s.type is StopType.BREAK)
    assert res.feasible and br.location_id == "SC-01" and br.start == hm(15)


# 11. Недостижимая дорожная связь
def test_unreachable_road() -> None:
    res = run([D.order("A")], [VisitItem("A")], trips={("SC-01", "B-00001"): None})
    assert not res.feasible and res.violation.code is C.H6_TRAVEL
    back = run([D.order("A")], [VisitItem("A")], trips={("B-00001", "SC-01"): None})
    assert not back.feasible and back.violation.code is C.H6_TRAVEL


def test_point_missing_in_matrix() -> None:
    res = run([D.order("A", building="B-09999")], [VisitItem("A")])
    assert not res.feasible and "нет в матрице" in res.violation.message


# 12. Пустой маршрут
def test_empty_route() -> None:
    res = run([], [])
    assert res.feasible and res.stops == () and res.travel_sec == 0


# 13. Замороженная выполненная часть
def test_frozen_prefix_kept_and_continued() -> None:
    a = D.order("A", slot=(hm(9, 20), hm(11, 20)), equipment=Eq(2, 0, 0))
    b = D.order("B", building="B-00002", slot=(hm(10), hm(12)), equipment=Eq(1, 0, 0))
    prefix = (
        RouteStop(StopType.DEPOT_START, "SC-01", hm(9), hm(9), hm(9)),
        RouteStop(
            StopType.VISIT, "B-00001", hm(9, 20), hm(9, 20), hm(10, 45), "A", 1200
        ),  # задержка до 10:45
    )
    res = run([a, b], [VisitItem("B")], frozen_prefix=prefix, earliest_departure=hm(10, 30))
    assert res.feasible and res.stops[:2] == prefix
    sb = visit_stop(res, "B")
    assert sb.arrival == hm(11, 5) and sb.equipment_after == Eq(0, 3, 3)
    # now позже окончания: выезд не раньше now
    res2 = run([a, b], [VisitItem("B")], frozen_prefix=prefix, earliest_departure=hm(11))
    assert visit_stop(res2, "B").arrival == hm(11, 20)


def test_frozen_equipment_counts_and_finished_route() -> None:
    a = D.order("A", equipment=Eq(3, 0, 0))
    b = D.order("B", building="B-00002", slot=(hm(11), hm(13)), equipment=Eq(1, 0, 0))
    prefix = (
        RouteStop(StopType.DEPOT_START, "SC-01", hm(9), hm(9), hm(9)),
        RouteStop(StopType.VISIT, "B-00001", hm(9, 20), hm(9, 20), hm(10, 20), "A", 1200),
    )
    res = run([a, b], [VisitItem("B")], frozen_prefix=prefix)
    assert not res.feasible and res.violation.code is C.H7_EQUIPMENT
    done = (
        *prefix,
        RouteStop(StopType.BREAK, "B-00001", hm(12), hm(12), hm(13)),
        RouteStop(StopType.DEPOT_END, "SC-01", hm(17), hm(17), hm(17)),
    )
    assert not run([a, b], [VisitItem("B")], frozen_prefix=done).feasible
    assert run([a, b], [], frozen_prefix=done).feasible


def test_input_not_mutated() -> None:
    items = (VisitItem("A"),)
    draft_items = tuple(items)
    run([D.order("A")], items)
    assert items == draft_items


# дополнительно: события
def test_blocked_window_and_resume_from_depot() -> None:
    o = D.order("A", slot=(hm(10), hm(13)))
    blocked = run([o], [VisitItem("A")], blocked=(TimeWindow(hm(9), hm(12)),), earliest_departure=hm(9))
    assert not blocked.feasible and blocked.violation.unassigned_hint is R.STAFF_UNAVAILABLE
    prefix = (RouteStop(StopType.DEPOT_START, "SC-01", hm(9), hm(9), hm(9)),)
    res = run(
        [o], [VisitItem("A")], frozen_prefix=prefix, resume_from_depot_at=hm(12), earliest_departure=hm(10)
    )
    assert res.feasible and visit_stop(res, "A").arrival == hm(12, 20)


def test_not_before_new_order_timestamp() -> None:
    o = D.order("N", slot=(hm(9), hm(11)))
    res = run([o], [VisitItem("N")], not_before=(("N", hm(10, 5)),))
    assert visit_stop(res, "N").start == hm(10, 5)


def test_deadline_miss_is_metric_not_violation() -> None:
    o = D.order("A", slot=(hm(9), hm(11)), deadline=hm(9, 30))
    res = run([o], [VisitItem("A")])
    assert res.feasible and res.deadline_misses == ("A",)


def test_wrong_staff_in_draft() -> None:
    ref = D.reference([EMP])
    res = simulate_route(
        EMP, RouteDraft("ST-999", (VisitItem("A"),)), D.day(ref, [D.order("A")]), D.matrix(PTS)
    )
    assert not res.feasible and res.violation.code is C.STRUCTURE


def test_profile_mixed_staff_can_survey() -> None:
    emp = D.employee("ST-201", profiles=(Profile.INSTALL, Profile.LTP3))
    res = run([D.order("S", W.SURVEY_PRIVATE, slot=(hm(10), hm(12)))], [VisitItem("S")], emp=emp)
    assert res.feasible
