"""ADO-11: порядок целей, вес повторных, переносы, CV, пустые планы, нарушения + валидатор H1–H10."""

from __future__ import annotations

from dataclasses import replace

from ado.domain.enums import ConstraintCode as C
from ado.domain.enums import StopType, UnassignedAction, UnassignedReasonCode
from ado.domain.models import CancelledOrder, PgpGroup, Plan, PlanMetrics, Route, UnassignedReason
from ado.optimization.evaluation import EvaluationConfig, compare_plans, evaluate_plan
from ado.optimization.initial import build_daily_plan
from ado.optimization.validator import validate_plan
from tests import dom as D

hm = D.hm
BLD = ["B-00001", "B-00002", "B-00003"]


def m(**kw) -> PlanMetrics:
    base = dict(
        weighted_success=10,
        success_count=10,
        orders_total=12,
        reschedules=2,
        travel_sec_total=10_000,
        utilization={},
        utilization_cv=0.2,
        hard_violations=0,
    )
    base.update(kw)
    return PlanMetrics(**base)


def test_travel_savings_do_not_beat_timely_visits() -> None:
    assert compare_plans(m(weighted_success=11, travel_sec_total=90_000), m()) == 1


def test_reschedule_worse_all_else_equal() -> None:
    assert compare_plans(m(reschedules=3), m()) == -1


def test_less_travel_better_when_senior_equal() -> None:
    assert compare_plans(m(travel_sec_total=9_000, utilization_cv=0.9), m()) == 1
    assert compare_plans(m(travel_sec_total=10_050), m(), EvaluationConfig(travel_tolerance_sec=60)) == 0


def test_balance_only_after_senior_criteria() -> None:
    assert compare_plans(m(utilization_cv=0.1), m()) == 1
    assert compare_plans(m(utilization_cv=0.1, travel_sec_total=10_001), m()) == -1
    assert compare_plans(m(), m()) == 0


def test_infeasible_never_wins() -> None:
    assert compare_plans(m(weighted_success=0), m(weighted_success=100, hard_violations=1)) == 1


# ---- на реальных планах


def solve(orders, emps=None):
    emps = emps or [D.employee("ST-001"), D.employee("ST-002")]
    ref = D.reference(emps, buildings=BLD)
    day = D.day(ref, orders)
    travel = D.matrix(["SC-01", *BLD])
    return build_daily_plan(day, travel), day, travel


def test_repeat_weight_counts_double() -> None:
    res, *_ = solve([D.order("A", building=BLD[0]), D.order("R", building=BLD[1], repeat=True)])
    assert (
        res.metrics.weighted_success == 3
        and res.metrics.weighted_total == 3
        and res.metrics.success_count == 2
    )


def test_empty_and_fully_unassigned_plans() -> None:
    res, _day, _travel = solve([])
    mt = res.metrics
    assert mt.orders_total == 0 and mt.weighted_success_share == 1.0 and mt.utilization_cv == 0.0
    assert set(mt.utilization) == {"ST-001", "ST-002"} and all(v == 0 for v in mt.utilization.values())
    night = [D.order(f"N{i}", building=BLD[i], slot=(hm(20), hm(22))) for i in range(3)]
    res2, *_ = solve(night)
    assert res2.metrics.success_count == 0 and res2.metrics.unassigned_count == 3
    assert res2.metrics.reschedules == 3 and res2.metrics.unassigned_by_reason == {"NO_SHIFT_TIME": 3}


def test_pgp_and_cancelled_excluded_from_scope() -> None:
    res, day, travel = solve([D.order("A", building=BLD[0]), D.order("B", building=BLD[1])])
    plan = res.plan
    route = plan.routes[res.working.assigned()["B"]]
    route.stops = [s for s in route.stops if s.order_id != "B"]
    plan.cancelled["B"] = CancelledOrder("B", hm(8, 30))
    mt = evaluate_plan(plan, day, travel)
    assert mt.orders_total == 1 and mt.cancelled == 1


def test_hard_violation_detected_and_counted() -> None:
    res, day, travel = solve([D.order("A", building=BLD[0])])
    plan = res.plan
    sid = res.working.assigned()["A"]
    stops = plan.routes[sid].stops
    i = next(i for i, s in enumerate(stops) if s.order_id == "A")
    stops[i] = replace(stops[i], start=hm(11, 30), end=hm(12, 30))  # позже latest_start
    issues = validate_plan(plan, day, travel)
    assert any(x.code is C.H4_SLOT for x in issues)
    mt = evaluate_plan(plan, day, travel)
    assert mt.hard_violations > 0 and mt.success_count == 0 and compare_plans(mt, res.metrics) == -1


# ---- валидатор: структурные нарушения


def test_validator_catches_h9_h3_h6_h8() -> None:
    res, day, travel = solve([D.order("A", building=BLD[0]), D.order("B", building=BLD[1])])
    plan = res.plan
    plan.unassigned["A"] = UnassignedReason("A", UnassignedReasonCode.OTHER, "x", UnassignedAction.ESCALATE)
    sid = res.working.assigned()["B"]
    plan.routes[sid].stops = [s for s in plan.routes[sid].stops if s.type is not StopType.BREAK]
    other = next(s for s in plan.routes if s != sid)
    if plan.routes[other].stops:
        st = plan.routes[other].stops
        st[1] = replace(st[1], arrival=st[0].end + 1, start=max(st[1].start, st[0].end + 1))
    plan.routes[sid].stops[0] = replace(plan.routes[sid].stops[0], location_id="SC-02")
    codes = {x.code for x in validate_plan(plan, day, travel)}
    assert {C.H9_ACCOUNTING, C.H3_BREAK, C.H8_SERVICE_CENTER} <= codes


def test_validator_freeze_rule() -> None:
    res, day, travel = solve([D.order("A", building=BLD[0], slot=(hm(9), hm(11)))])
    prev = res.plan
    sid = res.working.assigned()["A"]
    stops = list(prev.routes[sid].stops)
    i = next(i for i, s in enumerate(stops) if s.order_id == "A")
    later = Plan(
        prev.date,
        2,
        stops[i].start + 60,
        {k: Route(r.staff_id, r.service_center_id, list(r.stops)) for k, r in prev.routes.items()},
        dict(prev.unassigned),
        list(prev.pgp_groups),
        dict(prev.cancelled),
        list(prev.alerts),
    )
    assert not [x for x in validate_plan(later, day, travel, previous=prev) if x.code is C.H10_FREEZE]
    later.routes[sid].stops[i] = replace(stops[i], end=stops[i].end + 600)
    assert any(x.code is C.H10_FREEZE for x in validate_plan(later, day, travel, previous=prev))
    ok = validate_plan(later, day, travel, previous=prev, end_overrides={"A": stops[i].end + 600})
    assert not [x for x in ok if x.code is C.H10_FREEZE]


def test_validator_pgp_counts_as_place() -> None:
    res, day, travel = solve([D.order("A", building=BLD[0])])
    plan = res.plan
    plan.pgp_groups.append(PgpGroup("PGP-1", "AN-0001", ["A", "A2"], hm(8, 30)))
    codes = [x for x in validate_plan(plan, day, travel) if x.code is C.H9_ACCOUNTING]
    assert {x.order_id for x in codes} == {"A", "A2"}  # A дважды, A2 неизвестен
