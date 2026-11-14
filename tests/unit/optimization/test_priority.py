from __future__ import annotations

from ado.domain.enums import WorkTypeCode
from ado.optimization.priority import PriorityConfig, calculate_order_priority, prioritize_orders
from tests import dom as D

REF = D.reference([D.employee()])
NOW = D.hm(8, 30)


def prio(orders, elig=None, now=NOW, cfg=PriorityConfig()):
    elig = elig or {o.order_id: ["a", "b", "c"] for o in orders}
    return [s.order_id for s in prioritize_orders(orders, elig, now, REF, cfg)]


def test_repeat_first() -> None:
    assert prio([D.order("A"), D.order("B", repeat=True)]) == ["B", "A"]


def test_closer_latest_start_more_urgent() -> None:
    assert prio([D.order("Late", slot=(D.hm(15), D.hm(17))), D.order("Soon", slot=(D.hm(9), D.hm(11)))]) == [
        "Soon",
        "Late",
    ]


def test_narrow_installation_window_beats_wide() -> None:
    inst = D.order("I", WorkTypeCode.ADD_SERVICE, slot=(D.hm(13), D.hm(15)))
    rep = D.order("R", WorkTypeCode.REPAIR, slot=(D.hm(13), D.hm(15)))
    assert prio([rep, inst]) == ["I", "R"]


def test_few_eligible_staff_first_and_none_last() -> None:
    a, b, c = D.order("A"), D.order("B"), D.order("C")
    assert prio([a, b, c], {"A": ["x", "y", "z", "w"], "B": ["x"], "C": []}) == ["B", "A", "C"]


def test_ties_deterministic() -> None:
    orders = [D.order(x) for x in ["Z", "A", "M"]]
    assert prio(orders) == ["A", "M", "Z"]
    assert prio(list(reversed(orders))) == ["A", "M", "Z"]


def test_priority_changes_with_time() -> None:
    o = D.order(slot=(D.hm(14), D.hm(16)))
    morning = calculate_order_priority(o, 3, 3600, D.hm(8))
    noon = calculate_order_priority(o, 3, 3600, D.hm(13))
    assert noon.value > morning.value
    assert noon.components["urgency"] > morning.components["urgency"]
    # слот уже идёт: окно начала сужается
    inside = calculate_order_priority(o, 3, 3600, D.hm(15, 30))
    assert inside.components["tightness"] > noon.components["tightness"]


def test_config_weights_and_explanation() -> None:
    o = D.order(repeat=True)
    s = calculate_order_priority(o, 2, 3600, NOW, PriorityConfig(w_repeat=10))
    assert s.value >= 10 and "repeat=1.00" in s.explain() and "допущено 2" in s.explain()
