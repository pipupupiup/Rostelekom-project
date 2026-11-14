"""ADO-13: 15 обязательных сценариев + 409/422. Каждая версия плана проверяется валидатором H1–H10."""

from __future__ import annotations

import pytest

from ado.domain.enums import (
    AlertAction,
    AlertType,
    CancelReason,
    RouteStatus,
    StopType,
    TransferTarget,
    UnavailableReason,
)
from ado.domain.enums import (
    UnassignedReasonCode as R,
)
from ado.domain.equipment import EquipmentInventory as Eq
from ado.domain.events import (
    NewOrderEvent,
    OrderCancelledEvent,
    OrderTransferredEvent,
    StaffUnavailableEvent,
    TimeTickEvent,
    VisitDelayedEvent,
)
from ado.domain.models import VisitItem
from ado.events.session import DaySession, EventOutOfOrder, EventRejected, PlanNotFound, ReoptConfig
from ado.optimization.improvement import ImproveConfig
from ado.optimization.validator import validate_plan
from tests import dom as D

hm = D.hm
BLD = [f"B-{i:05d}" for i in range(1, 12)]
FAST = ReoptConfig(improve=ImproveConfig(max_iterations=15, time_limit_sec=5))


def session(emps, orders, trips=None, default=20 * 60, cfg=FAST):
    ref = D.reference(emps, buildings=BLD)
    day = D.day(ref, orders)
    travel = D.matrix(["SC-01", *BLD], trips, default)
    s = DaySession(day, travel, cfg)
    s.build_plan(30)
    assert validate_plan(s.plan, day, travel) == []
    return s


def check(s: DaySession, prev=None) -> None:
    issues = validate_plan(
        s.plan, s.day, s.travel, previous=prev, end_overrides=s.end_overrides, not_before=s.not_before
    )
    assert issues == [], issues


def apply(s: DaySession, ev):
    prev = s.plan
    out = s.apply(ev)
    if not out.duplicate:
        check(s, prev)
        assert out.plan_version == prev.plan_version + 1
    return out


def visit_of(s, oid):
    for r in s.plan.routes.values():
        for st in r.stops:
            if st.order_id == oid:
                return r.staff_id, st
    return None, None


def force_route(s: DaySession, sid: str, oids: list[str]) -> None:
    r = s.working.routes[sid]
    for other in s.working.routes.values():
        if other is not r:
            items = [i for i in other.items if not (isinstance(i, VisitItem) and i.order_id in oids)]
            other.set_schedule(items, other.simulate(items, s.day, s.travel), s.day)
    items = [VisitItem(o) for o in oids]
    sim = r.simulate(items, s.day, s.travel)
    assert sim.feasible, sim.violation
    r.set_schedule(items, sim, s.day)
    for o in oids:
        s.working.unassigned.pop(o, None)
    s.plan = s.working.to_plan(1, s.day.now)
    s.history = [s.plan]
    check(s)


# 1
def test_new_urgent_order() -> None:
    s = session([D.employee("ST-001"), D.employee("ST-002")], [D.order("A", building=BLD[0])])
    new = D.order("N", building=BLD[1], slot=(hm(11), hm(13)), repeat=True, registered=hm(10, 5))
    out = apply(s, NewOrderEvent("E1", hm(10, 5), new))
    sid, st = visit_of(s, "N")
    assert sid is not None and st.start >= hm(10, 5) and st.arrival >= hm(10, 5)
    assert out.changed_staff_ids and "назначен" in out.message


# 2, 5
def test_delay_40_minutes_reassigns_next_visit() -> None:
    a = D.order("A", building=BLD[0], slot=(hm(9), hm(9)), latest=hm(9, 20))
    b = D.order("B", building=BLD[1], slot=(hm(10, 40), hm(11)), latest=hm(10, 50))
    s = session([D.employee("ST-001"), D.employee("ST-002")], [a, b])
    force_route(s, "ST-001", ["A", "B"])
    _, sa = visit_of(s, "A")
    out = apply(s, VisitDelayedEvent("E1", sa.start + 30 * 60, "ST-001", "A", sa.end + 40 * 60))
    sid_b, _ = visit_of(s, "B")
    assert sid_b == "ST-002"
    risk = [x for x in out.new_alerts if x.type is AlertType.LATE_RISK]
    assert len(risk) == 1 and risk[0].order_id == "B" and risk[0].suggested_action is AlertAction.REASSIGN
    assert risk[0].suggested_staff_id == "ST-002" and risk[0].staff_id == "ST-001" and risk[0].applied
    assert risk[0].created_at == sa.start + 30 * 60 and risk[0].lateness_min and risk[0].lateness_min > 0
    _, sa2 = visit_of(s, "A")
    assert sa2.end == sa.end + 40 * 60


# 3
def test_cancel_next_order() -> None:
    orders = [D.order(f"O{i}", building=BLD[i], slot=(hm(10 + i), hm(12 + i))) for i in range(3)]
    s = session([D.employee("ST-001")], orders)
    out = apply(s, OrderCancelledEvent("E1", hm(9, 30), "O1", CancelReason.NOT_CONFIRMED))
    assert "O1" in s.plan.cancelled and visit_of(s, "O1") == (None, None)
    assert s.plan.cancelled["O1"].event_id == "E1" and "отменён" in out.message


def test_cancel_started_visit_stays() -> None:
    s = session([D.employee("ST-001")], [D.order("A", building=BLD[0], slot=(hm(9), hm(11)))])
    _, st = visit_of(s, "A")
    out = apply(s, OrderCancelledEvent("E1", st.start + 60, "A", CancelReason.CUSTOMER_REQUEST))
    assert "A" not in s.plan.cancelled and visit_of(s, "A")[0] == "ST-001" and "начат" in out.message


# 4, 15
def test_staff_unavailable_redistributes_and_no_alternatives() -> None:
    orders = [D.order(f"O{i}", building=BLD[i], slot=(hm(11 + 2 * i), hm(13 + 2 * i))) for i in range(3)]
    s = session([D.employee("ST-001"), D.employee("ST-002")], orders)
    owners = {o: visit_of(s, o)[0] for o in ("O0", "O1", "O2")}
    victim = owners["O0"]
    lost = [o for o, sid in owners.items() if sid == victim]
    out = apply(s, StaffUnavailableEvent("E1", hm(10), victim, UnavailableReason.VEHICLE_BREAKDOWN))
    route = s.plan.routes[victim]
    assert route.status is RouteStatus.UNAVAILABLE and route.unavailable_from == hm(10)
    assert all(visit_of(s, o)[0] not in (victim, None) for o in lost)
    assert {a.order_id for a in out.new_alerts if a.type is AlertType.LATE_RISK} == set(lost)
    # второй выбывает тоже — альтернатив нет
    other = next(sid for sid in s.plan.routes if sid != victim)
    out2 = apply(s, StaffUnavailableEvent("E2", hm(10, 30), other, UnavailableReason.ILLNESS))
    assert set(s.plan.unassigned) == {"O0", "O1", "O2"}
    assert {r.code for r in s.plan.unassigned.values()} == {R.STAFF_UNAVAILABLE}
    late = [a for a in out2.new_alerts if a.type is AlertType.LATE_RISK]
    assert late and all(a.suggested_action is AlertAction.RESCHEDULE and a.proposed_slot for a in late)


def test_staff_unavailable_until_then_resumes_from_depot() -> None:
    orders = [D.order("A", building=BLD[0], slot=(hm(9, 30), hm(15)))]
    s = session([D.employee("ST-001")], orders)
    apply(s, StaffUnavailableEvent("E1", hm(9), "ST-001", UnavailableReason.OTHER, until=hm(11)))
    sid, st = visit_of(s, "A")
    assert sid == "ST-001" and st.arrival >= hm(11) + 20 * 60


# 6
def test_reorder_inside_route_keeps_both() -> None:
    x = D.order("X", building=BLD[0], slot=(hm(9), hm(9)), latest=hm(9, 20))
    a = D.order("A", building=BLD[1], slot=(hm(10, 20), hm(14)))
    b = D.order("B", building=BLD[2], slot=(hm(11, 40), hm(12, 10)), latest=hm(12, 10))
    s = session([D.employee("ST-001")], [x, a, b])
    force_route(s, "ST-001", ["X", "A", "B"])
    _, sx = visit_of(s, "X")
    out = apply(s, VisitDelayedEvent("E1", sx.start + 10 * 60, "ST-001", "X", sx.end + 70 * 60))
    order = [st.order_id for st in s.plan.routes["ST-001"].stops if st.type is StopType.VISIT]
    assert order == ["X", "B", "A"] and not s.plan.unassigned
    assert not [a for a in out.new_alerts if a.type is AlertType.LATE_RISK]


# 7
def test_new_order_needs_restock() -> None:
    emp = D.employee("ST-001", start=Eq(0, 0, 0), capacity=Eq(2, 2, 2))
    s = session([emp], [D.order("A", building=BLD[0], slot=(hm(9), hm(11)))])
    new = D.order("N", building=BLD[1], slot=(hm(13), hm(15)), equipment=Eq(1, 0, 0), registered=hm(10))
    apply(s, NewOrderEvent("E1", hm(10), new))
    stops = s.plan.routes["ST-001"].stops
    assert visit_of(s, "N")[0] == "ST-001" and any(st.type is StopType.RESTOCK for st in stops)


# 8
def test_slot_cannot_be_kept_rescheduled() -> None:
    a = D.order("A", building=BLD[0], slot=(hm(9), hm(9)), latest=hm(9, 20))
    b = D.order("B", building=BLD[1], slot=(hm(10, 40), hm(11)), latest=hm(10, 50))
    s = session([D.employee("ST-001")], [a, b])
    _, sa = visit_of(s, "A")
    out = apply(s, VisitDelayedEvent("E1", sa.start + 30 * 60, "ST-001", "A", sa.end + 60 * 60))
    assert "B" in s.plan.unassigned and s.plan.unassigned["B"].proposed_slot is not None
    risk = [x for x in out.new_alerts if x.type is AlertType.LATE_RISK]
    assert risk and risk[0].suggested_action is AlertAction.RESCHEDULE


# 9, 12
def test_two_sequential_events_preserve_done_stops() -> None:
    orders = [D.order(f"O{i}", building=BLD[i], slot=(hm(9 + i), hm(11 + i))) for i in range(5)]
    s = session([D.employee("ST-001"), D.employee("ST-002")], orders)
    v1 = s.plan
    apply(s, TimeTickEvent("E1", hm(10, 30)))
    v2 = s.plan
    apply(s, OrderCancelledEvent("E2", hm(11, 30), "O4", CancelReason.NOT_CONFIRMED))
    assert [p.plan_version for p in s.history] == [1, 2, 3]
    for prev, cur, t in ((v1, v2, hm(10, 30)), (v2, s.plan, hm(11, 30))):
        for sid, r in prev.routes.items():
            started = [st for st in r.stops if st.start < t]
            assert cur.routes[sid].stops[: len(started)] == started


# 10
def test_duplicate_event_idempotent() -> None:
    s = session([D.employee("ST-001")], [D.order("A", building=BLD[0])])
    ev = TimeTickEvent("E1", hm(10))
    first = apply(s, ev)
    plan = s.plan
    again = s.apply(ev)
    assert again.duplicate and again.plan_version == first.plan_version and s.plan is plan


# 11
def test_out_of_order_and_same_timestamp() -> None:
    s = session([D.employee("ST-001")], [D.order("A", building=BLD[0])])
    apply(s, TimeTickEvent("E1", hm(10)))
    apply(s, TimeTickEvent("E2", hm(10)))  # одинаковый timestamp допустим
    with pytest.raises(EventOutOfOrder):
        s.apply(TimeTickEvent("E3", hm(9, 59)))
    assert s.version == 3


# 13
def test_pgp_appears_on_new_order() -> None:
    node = "AN-0777"
    o1 = D.order("R1", building=BLD[0], node=node, registered=hm(7, 50), slot=(hm(11), hm(13)))
    o2 = D.order("R2", building=BLD[1], node=node, registered=hm(8, 10), slot=(hm(11), hm(13)))
    s = session([D.employee("ST-001"), D.employee("ST-002")], [o1, o2])
    assert not s.plan.pgp_groups and visit_of(s, "R1")[0]
    o3 = D.order("R3", building=BLD[2], node=node, registered=hm(8, 40), slot=(hm(11), hm(13)))
    out = apply(s, NewOrderEvent("E1", hm(8, 40), o3))
    g = s.plan.pgp_groups[0]
    assert sorted(g.order_ids) == ["R1", "R2", "R3"] and g.detected_at == hm(8, 40)
    assert visit_of(s, "R1") == (None, None)
    assert any(a.type is AlertType.PGP_DETECTED for a in out.new_alerts)
    # наряды, снятые с маршрута из-за ПГП, получают LATE_RISK с передачей в станционное
    removed = {a.order_id for a in out.new_alerts if a.type is AlertType.LATE_RISK}
    assert removed >= {"R1", "R2"} and "R3" not in removed


# 14
def test_tiny_budget_still_valid() -> None:
    cfg = ReoptConfig(
        event_budget_sec=0.002, event_reserve_sec=0.001, improve=ImproveConfig(max_iterations=5)
    )
    orders = [D.order(f"O{i}", building=BLD[i], slot=(hm(11 + i), hm(13 + i))) for i in range(4)]
    s = session([D.employee("ST-001"), D.employee("ST-002")], orders, cfg=cfg)
    victim = visit_of(s, "O0")[0]
    out = apply(s, StaffUnavailableEvent("E1", hm(10), victim, UnavailableReason.ILLNESS))
    assert out.processing_ms < 10_000


def test_transfer_frees_time() -> None:
    s = session([D.employee("ST-001")], [D.order("A", building=BLD[0], slot=(hm(9), hm(11)))])
    _, st = visit_of(s, "A")
    apply(
        s,
        OrderTransferredEvent(
            "E1", st.start + 15 * 60, "ST-001", "A", st.start + 15 * 60, TransferTarget.STATION_EQUIPMENT
        ),
    )
    _, st2 = visit_of(s, "A")
    assert st2.end == st.start + 15 * 60


def test_rejections_422_and_404() -> None:
    ref = D.reference([D.employee()], buildings=BLD)
    day = D.day(ref, [D.order("A", building=BLD[0], slot=(hm(13), hm(15)))])
    raw = DaySession(day, D.matrix(["SC-01", *BLD]), FAST)
    with pytest.raises(PlanNotFound):
        raw.apply(TimeTickEvent("E0", hm(9)))
    raw.build_plan(30)
    with pytest.raises(EventRejected):
        raw.apply(OrderCancelledEvent("E1", hm(9), "NOPE", CancelReason.NOT_CONFIRMED))
    with pytest.raises(EventRejected, match="не начат"):
        raw.apply(VisitDelayedEvent("E2", hm(9), "ST-001", "A", hm(16)))
    with pytest.raises(EventRejected):
        raw.apply(StaffUnavailableEvent("E3", hm(9), "ST-999", UnavailableReason.OTHER))
    with pytest.raises(EventRejected, match="уже существует"):
        raw.apply(NewOrderEvent("E4", hm(9), raw.day.orders["A"]))
    assert raw.version == 1 and not raw.results
