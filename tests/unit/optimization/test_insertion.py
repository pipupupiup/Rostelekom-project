"""ADO-06/08: кандидаты по стоимости вставки, расширение поиска, экономия проверок."""

from __future__ import annotations

from ado.domain.equipment import EquipmentInventory as Eq
from ado.domain.models import VisitItem
from ado.optimization.initial import empty_routes
from ado.optimization.insertion import InsertionConfig, InsertionEngine
from tests import dom as D

hm = D.hm
BLD = [f"B-{i:05d}" for i in range(1, 10)]


def setup(emps, orders, trips=None, default=20 * 60, cfg=InsertionConfig()):
    ref = D.reference(emps, buildings=BLD)
    day = D.day(ref, orders)
    travel = D.matrix(["SC-01", *BLD], trips, default)
    return InsertionEngine(day, travel, cfg), empty_routes(day), day, travel


def test_insertion_cost_uses_existing_route_not_depot_distance() -> None:
    # ST-001 уже рядом (визит в B1), ST-002 стартует из СЦ — далеко
    trips = {
        ("SC-01", BLD[0]): 40 * 60,
        (BLD[0], "SC-01"): 40 * 60,
        ("SC-01", BLD[1]): 40 * 60,
        (BLD[1], "SC-01"): 40 * 60,
        (BLD[0], BLD[1]): 3 * 60,
        (BLD[1], BLD[0]): 3 * 60,
    }
    a = D.order("A", building=BLD[0], slot=(hm(10), hm(12)))
    b = D.order("B", building=BLD[1], slot=(hm(11), hm(13)))
    eng, routes, day, _travel = setup([D.employee("ST-001"), D.employee("ST-002")], [a, b], trips)
    opt = eng.best_for_route(a, routes["ST-001"])
    routes["ST-001"].set_schedule(list(opt.items), opt.simulation, day)
    ranked = eng.rank_candidates(b, ["ST-002", "ST-001"], routes)
    assert [c.staff_id for c in ranked] == ["ST-001", "ST-002"]
    assert ranked[0].best_delta == 3 * 60 + 40 * 60 - 40 * 60  # B1→B→СЦ вместо B1→СЦ


def test_same_coordinates_zero_travel() -> None:
    a = D.order("A", building=BLD[0], slot=(hm(10), hm(12)))
    b = D.order("B", building=BLD[0], slot=(hm(11), hm(13)))
    eng, routes, day, _ = setup([D.employee()], [a, b])
    opt = eng.best_for_route(a, routes["ST-001"])
    routes["ST-001"].set_schedule(list(opt.items), opt.simulation, day)
    opt_b = eng.best_for_route(b, routes["ST-001"])
    assert opt_b.delta_travel_sec == 0 and opt_b.simulation.feasible


def test_no_local_options_then_expansion() -> None:
    emps = [D.employee(f"ST-{i:03d}") for i in range(1, 9)]
    o = D.order("X", building=BLD[0], slot=(hm(10), hm(12)))
    eng, routes, day, _ = setup(
        emps, [o], cfg=InsertionConfig(first_group_size=3, expand_step=3, min_options=2)
    )
    # первые по алфавиту заняты на всё окно → у них нет вариантов; поиск расширяется
    blocker = D.order
    for i in range(1, 6):
        b = blocker(f"BL{i}", building=BLD[1], slot=(hm(9), hm(9)), latest=hm(9, 20))
        day.orders[b.order_id] = b
        st = routes[f"ST-{i:03d}"]
        sim = st.simulate([VisitItem(b.order_id)], day, eng.travel)
        st.set_schedule([VisitItem(b.order_id)], sim, day)
    opts = eng.find_feasible_insertions(o, list(routes), routes)
    assert len(opts) >= 2 and all(opt.simulation.feasible for opt in opts)


def test_grouped_search_checks_fewer_routes_than_full() -> None:
    emps = [D.employee(f"ST-{i:03d}") for i in range(1, 13)]
    o = D.order("X", building=BLD[0], slot=(hm(10), hm(12)))
    cfg = InsertionConfig(first_group_size=3, min_options=2)
    eng, routes, *_ = setup(emps, [o], cfg=cfg)
    grouped = eng.find_feasible_insertions(o, list(routes), routes)
    grouped_sims = eng.stats.simulations
    eng2, routes2, *_ = setup(emps, [o], cfg=cfg)
    full = eng2.find_feasible_insertions(o, list(routes2), routes2, full=True)
    assert len(full) == 12 and len(grouped) == 3
    assert grouped_sims < eng2.stats.simulations
    assert grouped[0].score == full[0].score  # лучший вариант не потерян


def test_restock_option_when_backpack_short() -> None:
    emp = D.employee(start=Eq(0, 0, 0), capacity=Eq(2, 2, 2))
    o = D.order("A", slot=(hm(10), hm(12)), equipment=Eq(1, 0, 0))
    eng, routes, *_ = setup([emp], [o])
    opt = eng.best_for_route(o, routes["ST-001"])
    assert opt.with_restock and opt.restock_service_center_id == "SC-01"


def test_infeasible_everywhere_returns_none() -> None:
    o = D.order("A", slot=(hm(9), hm(9)), latest=hm(9, 5))  # 9:00 + 20 мин дороги > 9:05
    eng, routes, *_ = setup([D.employee()], [o])
    assert eng.best_for_route(o, routes["ST-001"]) is None
