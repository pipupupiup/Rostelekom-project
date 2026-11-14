"""ADO-09: 10 сценариев задания + бюджет; каждый план проверяется независимым валидатором."""

from __future__ import annotations

from ado.domain.enums import StopType, Technology
from ado.domain.enums import UnassignedReasonCode as R
from ado.domain.enums import WorkTypeCode as W
from ado.domain.equipment import EquipmentInventory as Eq
from ado.optimization.baseline import build_baseline_plan
from ado.optimization.budget import TimeBudget
from ado.optimization.initial import build_daily_plan
from ado.optimization.validator import validate_plan
from tests import dom as D

hm = D.hm
BLD = [f"B-{i:05d}" for i in range(1, 10)]


def solve(emps, orders, trips=None, default=20 * 60, scs=None, budget=None):
    ref = D.reference(emps, buildings=BLD, scs=scs)
    day = D.day(ref, orders)
    travel = D.matrix(["SC-01", "SC-02", *BLD], trips, default)
    res = build_daily_plan(day, travel, budget=budget)
    assert validate_plan(res.plan, day, travel) == [] == res.issues  # 10. независимый валидатор
    return res, day, travel


def assignee(res, oid):
    return res.working.assigned().get(oid)


# 1
def test_all_orders_assigned() -> None:
    emps = [D.employee("ST-001"), D.employee("ST-002")]
    orders = [
        D.order(f"O{i}", building=BLD[i], slot=(hm(9 + 2 * (i % 4)), hm(11 + 2 * (i % 4)))) for i in range(6)
    ]
    res, *_ = solve(emps, orders)
    assert res.plan.unassigned == {} and res.metrics.success_count == 6
    assert res.metrics.weighted_success_share == 1.0
    assert all(s.explanation for r in res.plan.routes.values() for s in r.stops if s.type is StopType.VISIT)


# 2
def test_some_orders_impossible_with_reasons() -> None:
    emps = [D.employee("ST-001", techs=(Technology.PON,))]
    orders = [
        D.order("OK"),
        D.order("NOTECH", building=BLD[1], tech=Technology.DOCSIS),
        D.order("NIGHT", building=BLD[2], slot=(hm(19), hm(21))),
    ]
    res, *_ = solve(emps, orders)
    assert assignee(res, "OK") == "ST-001"
    assert res.plan.unassigned["NOTECH"].code is R.NO_QUALIFIED_STAFF
    assert res.plan.unassigned["NIGHT"].code is R.NO_SHIFT_TIME
    assert res.plan.unassigned["NIGHT"].proposed_slot is not None
    assert {a.order_id for a in res.plan.alerts} == {"NOTECH", "NIGHT"}


# 3
def test_valuable_order_wins_competition() -> None:
    emps = [D.employee("ST-001")]
    plain = D.order("PLAIN", building=BLD[0], slot=(hm(9), hm(11)), latest=hm(9, 20))
    repeat = D.order("REPEAT", building=BLD[1], slot=(hm(9), hm(11)), latest=hm(9, 20), repeat=True)
    res, *_ = solve(emps, [plain, repeat])
    assert assignee(res, "REPEAT") == "ST-001" and "PLAIN" in res.plan.unassigned
    assert res.metrics.weighted_success == 2


# 4
def test_rare_specialist_reserved_for_scarce_orders() -> None:
    spec = D.employee("ST-001", techs=(Technology.PON, Technology.DOCSIS))
    gen = D.employee("ST-002", techs=(Technology.PON,))
    orders = []
    for i, h in enumerate((9, 11, 14)):
        orders.append(D.order(f"D{i}", building=BLD[i], slot=(hm(h), hm(h, 30)), tech=Technology.DOCSIS))
        orders.append(D.order(f"P{i}", building=BLD[i + 3], slot=(hm(h), hm(h, 30))))
    res, *_ = solve([spec, gen], orders)
    assert res.plan.unassigned == {}
    assert {assignee(res, f"D{i}") for i in range(3)} == {"ST-001"}
    assert {assignee(res, f"P{i}") for i in range(3)} == {"ST-002"}


# 5
def test_overlapping_slots() -> None:
    orders = [D.order(f"O{i}", building=BLD[i], slot=(hm(9), hm(11))) for i in range(3)]
    one, *_ = solve([D.employee("ST-001")], orders)
    assert len(one.working.assigned()) == 2
    assert next(iter(one.plan.unassigned.values())).code is R.NO_SHIFT_TIME
    two, *_ = solve([D.employee("ST-001"), D.employee("ST-002")], orders)
    assert two.plan.unassigned == {}


# 6
def test_restock_inserted_when_backpack_short() -> None:
    emp = D.employee("ST-001", start=Eq(1, 0, 0), capacity=Eq(4, 2, 2))
    orders = [
        D.order(f"O{i}", building=BLD[i], slot=(hm(9 + 2 * i), hm(11 + 2 * i)), equipment=Eq(1, 0, 0))
        for i in range(3)
    ]
    res, *_ = solve([emp], orders)
    stops = res.plan.routes["ST-001"].stops
    assert res.plan.unassigned == {} and any(s.type is StopType.RESTOCK for s in stops)


# 7
def test_staff_from_different_service_centers() -> None:
    e1, e2 = D.employee("ST-001", sc="SC-01"), D.employee("ST-002", sc="SC-02")
    far = 50 * 60
    trips = {
        ("SC-01", BLD[0]): 5 * 60,
        (BLD[0], "SC-01"): 5 * 60,
        ("SC-02", BLD[0]): far,
        (BLD[0], "SC-02"): far,
        ("SC-02", BLD[1]): 5 * 60,
        (BLD[1], "SC-02"): 5 * 60,
        ("SC-01", BLD[1]): far,
        (BLD[1], "SC-01"): far,
    }
    orders = [
        D.order("NEAR1", building=BLD[0], slot=(hm(10), hm(12))),
        D.order("NEAR2", building=BLD[1], slot=(hm(10), hm(12))),
    ]
    res, *_ = solve([e1, e2], orders, trips=trips, scs={"SC-01": (hm(8), hm(21)), "SC-02": (hm(8), hm(21))})
    assert assignee(res, "NEAR1") == "ST-001" and assignee(res, "NEAR2") == "ST-002"
    assert res.plan.routes["ST-002"].stops[0].location_id == "SC-02"


# 8
def test_route_cannot_finish_within_shift() -> None:
    inst = D.installer("ST-101", height=True)
    vls = D.order("VLS", W.INST_PRIVATE_VLS, building=BLD[0], slot=(hm(15), hm(17)), height=True)
    res, *_ = solve([inst], [vls], default=30 * 60)  # 15:30 + 180 мин + 30 > 18:00
    assert res.plan.unassigned["VLS"].code is R.NO_SHIFT_TIME


# 9
def test_repeatable_with_same_input() -> None:
    emps = [D.employee(f"ST-00{i}") for i in range(1, 4)]
    orders = [D.order(f"O{i}", building=BLD[i % 9], slot=(hm(9 + i % 8), hm(11 + i % 8))) for i in range(12)]
    a, *_ = solve(emps, orders)
    b, *_ = solve(emps, list(reversed(orders)))
    assert {s: r.stops for s, r in a.plan.routes.items()} == {s: r.stops for s, r in b.plan.routes.items()}


def test_budget_exhausted_keeps_valid_plan() -> None:
    class Clock:
        t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    budget = TimeBudget(1, clock=clock)
    clock.t = 5  # бюджет исчерпан до цикла
    orders = [D.order(f"O{i}", building=BLD[i]) for i in range(3)]
    res, *_ = solve([D.employee()], orders, budget=budget)
    assert res.stats["stopped_by_budget"] == 1
    assert {r.code for r in res.plan.unassigned.values()} == {R.OTHER}


def test_better_or_equal_than_baseline_on_small_case() -> None:
    emps = [D.employee("ST-001"), D.employee("ST-002")]
    orders = [
        D.order(f"O{i}", building=BLD[i % 9], slot=(hm(9 + i % 5 * 2), hm(11 + i % 5 * 2))) for i in range(10)
    ]
    res, day, travel = solve(emps, orders)
    base = build_baseline_plan(day, travel)
    assert base.issues == []
    assert res.metrics.weighted_success >= base.metrics.weighted_success
