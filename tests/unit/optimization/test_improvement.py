"""ADO-10: операторы LS, ALNS, заморозка, детерминированность, бюджет, сравнение с исходным."""

from __future__ import annotations

from ado.domain.enums import StopType
from ado.domain.equipment import EquipmentInventory as Eq
from ado.domain.models import RestockItem, VisitItem
from ado.optimization.budget import TimeBudget
from ado.optimization.evaluation import compare_plans, working_metrics
from ado.optimization.improvement import (
    ImproveConfig,
    ImproveStats,
    LocalSearch,
    RouteImprover,
    remove_orders,
)
from ado.optimization.initial import InitialConfig, build_daily_plan
from ado.optimization.insertion import InsertionEngine
from ado.optimization.state import RouteState
from ado.optimization.validator import validate_plan
from tests import dom as D

hm = D.hm
BLD = [f"B-{i:05d}" for i in range(1, 9)]
FAR = 50 * 60


def instance(emps, orders, trips=None, default=20 * 60):
    ref = D.reference(emps, buildings=BLD)
    day = D.day(ref, orders)
    travel = D.matrix(["SC-01", *BLD], trips, default)
    res = build_daily_plan(day, travel)
    return res.working, day, travel


def ls_for(working):
    stats = ImproveStats()
    eng = InsertionEngine(working.day, working.travel)
    return LocalSearch(eng, InitialConfig(), ImproveConfig(), stats), stats


def force(working, sid, items):
    r = working.routes[sid]
    sim = r.simulate(items, working.day, working.travel)
    assert sim.feasible, sim.violation
    r.set_schedule(items, sim, working.day)


def big_budget():
    return TimeBudget(60)


def test_intra_relocate_improves_order() -> None:
    # квадрат СЦ(0,0) A(0,1) B(1,1) C(1,0), шаг 10 мин: A,B,C = 40 мин, плохой порядок A,C,B = 60 мин
    pos = {"SC-01": (0, 0), BLD[0]: (0, 1), BLD[1]: (1, 1), BLD[2]: (1, 0)}
    trips = {
        (a, b): (abs(pa[0] - pb[0]) + abs(pa[1] - pb[1])) * 600
        for a, pa in pos.items()
        for b, pb in pos.items()
        if a != b
    }
    orders = [D.order(x, building=BLD[i], slot=(hm(9), hm(17))) for i, x in enumerate("ABC")]
    w, *_ = instance([D.employee()], orders, trips)
    force(w, "ST-001", [VisitItem("A"), VisitItem("C"), VisitItem("B")])
    before = w.routes["ST-001"].sim.travel_sec
    ls, stats = ls_for(w)
    assert ls.intra(w, big_budget())
    assert w.routes["ST-001"].sim.travel_sec < before
    assert stats.ls_moves["intra_relocate"] + stats.ls_moves["intra_reverse"] >= 1


def test_inter_relocate_moves_to_nearer_staff() -> None:
    e1, e2 = D.employee("ST-001"), D.employee("ST-002", sc="SC-01")
    orders = [
        D.order("A", building=BLD[0], slot=(hm(10), hm(12))),
        D.order("B", building=BLD[1], slot=(hm(10), hm(12))),
    ]
    trips = {(BLD[0], BLD[1]): 2 * 60, (BLD[1], BLD[0]): 2 * 60}
    w, *_ = instance([e1, e2], orders, trips)
    force(w, "ST-001", [VisitItem("A")])
    force(w, "ST-002", [VisitItem("B")])
    before = working_metrics(w).travel_sec_total
    ls, _ = ls_for(w)
    assert ls.inter_relocate(w, big_budget())
    assert working_metrics(w).travel_sec_total < before
    assert {len(r.visit_ids()) for r in w.routes.values()} == {0, 2}


def test_swap_operator_and_invalid_swap() -> None:
    # A около СЦ1-зоны ST-002, B около ST-001: обмен выгоден
    from ado.domain.enums import Technology

    e1, e2 = D.employee("ST-001"), D.employee("ST-002")
    orders = [
        D.order("A", building=BLD[0], slot=(hm(10), hm(10, 30))),
        D.order("B", building=BLD[1], slot=(hm(10), hm(10, 30))),
        D.order("C", building=BLD[2], slot=(hm(12), hm(14))),
        D.order("E", building=BLD[3], slot=(hm(12), hm(14))),
    ]
    trips = {
        (BLD[0], BLD[3]): 2 * 60,
        (BLD[3], BLD[0]): 2 * 60,
        (BLD[1], BLD[2]): 2 * 60,
        (BLD[2], BLD[1]): 2 * 60,
    }
    w, *_ = instance([e1, e2], orders, trips)
    force(w, "ST-001", [VisitItem("A"), VisitItem("C")])
    force(w, "ST-002", [VisitItem("B"), VisitItem("E")])
    before = working_metrics(w).travel_sec_total
    ls, _ = ls_for(w)
    assert ls.swap(w, big_budget())
    assert working_metrics(w).travel_sec_total < before
    # недопустимый обмен: у ST-002 нет технологии — обмен не выполняется
    e2x = D.employee("ST-002", techs=(Technology.FTTB,))
    orders2 = [
        D.order("A", building=BLD[0], slot=(hm(10), hm(10, 30))),
        D.order("B", building=BLD[1], slot=(hm(10), hm(10, 30)), tech=Technology.FTTB),
        D.order("C", building=BLD[2], slot=(hm(12), hm(14))),
        D.order("E", building=BLD[3], slot=(hm(12), hm(14)), tech=Technology.FTTB),
    ]
    w2, *_ = instance([e1, e2x], orders2, trips)
    ls2, _ = ls_for(w2)
    ls2.swap(w2, big_budget())
    plan = w2.to_plan()
    assert validate_plan(plan, w2.day, w2.travel) == []


def test_drop_unneeded_restock() -> None:
    w, *_ = instance([D.employee(start=Eq(3, 3, 3))], [D.order("A", building=BLD[0], slot=(hm(10), hm(12)))])
    force(w, "ST-001", [RestockItem("SC-01"), VisitItem("A")])
    ls, _stats = ls_for(w)
    assert ls.drop_restock(w, big_budget())
    assert not any(isinstance(i, RestockItem) for i in w.routes["ST-001"].items)


def test_insert_unassigned_after_freeing_room() -> None:
    orders = [D.order(f"O{i}", building=BLD[i], slot=(hm(9), hm(11))) for i in range(3)]
    w, _day, _ = instance([D.employee("ST-001"), D.employee("ST-002")], orders)
    assert len(w.assigned()) == 3
    removed, _ = remove_orders(w, {"O0"})
    assert removed == ["O0"] and "O0" not in w.assigned()
    ls, _ = ls_for(w)
    assert ls.insert_unassigned(w, big_budget())
    assert "O0" in w.assigned()


def test_frozen_stops_preserved() -> None:
    orders = [D.order(f"O{i}", building=BLD[i], slot=(hm(9 + i), hm(11 + i))) for i in range(4)]
    w, day, travel = instance([D.employee("ST-001"), D.employee("ST-002")], orders)
    now = hm(10, 30)
    frozen_by = {}
    for sid, r in w.routes.items():
        k = sum(1 for s in r.sim.stops if s.start < now)
        frozen = r.sim.stops[:k]
        rest = [
            VisitItem(s.order_id) if s.type is StopType.VISIT else RestockItem(s.location_id)
            for s in r.sim.stops[k:]
            if s.type in (StopType.VISIT, StopType.RESTOCK)
        ]
        st = RouteState(r.employee, frozen=frozen, earliest_departure=now)
        sim = st.simulate(rest, day, travel)
        st.set_schedule(rest, sim, day)
        w.routes[sid] = st
        frozen_by[sid] = frozen
    out = RouteImprover(ImproveConfig(max_iterations=30, seed=1)).improve(w, big_budget())
    for sid, frozen in frozen_by.items():
        assert out.routes[sid].sim.stops[: len(frozen)] == frozen


def synthetic(n=120, seed=5):
    from ado.data.prepare import prepare_day, prepare_reference
    from ado.routing.matrix import MatrixTravelProvider, TravelMatrix
    from ado.synthetic import generate_day

    syn = generate_day(n_orders=n, seed=seed, n_nodes=40)
    ref = prepare_reference(syn.reference)
    return prepare_day(syn.day_input, ref), MatrixTravelProvider(TravelMatrix.from_api(syn.matrix))


def test_alns_deterministic_with_seed_and_not_worse() -> None:
    day, travel = synthetic()
    cfg = ImproveConfig(seed=3, max_iterations=40, time_limit_sec=120, max_no_improve=1000)
    a = build_daily_plan(day, travel, improver=RouteImprover(cfg))
    b = build_daily_plan(day, travel, improver=RouteImprover(cfg))
    assert {s: r.stops for s, r in a.plan.routes.items()} == {s: r.stops for s, r in b.plan.routes.items()}
    base = build_daily_plan(day, travel)
    assert a.issues == [] and compare_plans(a.metrics, base.metrics) >= 0


def test_budget_interrupts_alns() -> None:
    class Clock:
        t = 0.0

        def __call__(self):
            self.t += 0.01  # каждое обращение «съедает» 10 мс
            return self.t

    day, travel = synthetic(60)
    base = build_daily_plan(day, travel)
    imp = RouteImprover(ImproveConfig(max_iterations=10_000, time_limit_sec=1000))
    out = imp.improve(base.working, TimeBudget(2.0, clock=Clock()))
    assert imp.last_stats["alns_iterations"] < 10_000
    assert compare_plans(working_metrics(out), working_metrics(base.working)) >= 0


def test_no_improvement_possible_keeps_plan() -> None:
    w, *_ = instance([D.employee()], [D.order("A", building=BLD[0], slot=(hm(10), hm(12)))])
    before = working_metrics(w)
    out = RouteImprover(ImproveConfig(max_iterations=20)).improve(w, big_budget())
    after = working_metrics(out)
    assert compare_plans(after, before) == 0
