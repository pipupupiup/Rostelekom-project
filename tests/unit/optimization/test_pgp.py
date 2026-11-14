from __future__ import annotations

import pytest

from ado.domain.enums import WorkTypeCode
from ado.optimization.pgp import PgpConfig, PgpDetector
from tests import dom as D

REF = D.reference([D.employee()])


def rep(oid: str, minute: int, node: str = "AN-0001", wt: WorkTypeCode = WorkTypeCode.REPAIR):
    return D.order(oid, wt, node=node, registered=D.hm(7) + minute * 60)


def detect(orders, existing=(), started=frozenset(), now=D.hm(8, 30), cfg=None, cancelled=frozenset()):
    return PgpDetector(cfg).detect(
        orders, existing, started, now, REF, day="2026-10-26", cancelled_order_ids=cancelled
    )


def test_three_faults_same_node_form_group() -> None:
    r = detect([rep("A", 0), rep("B", 20), rep("C", 35)])
    assert len(r.groups) == 1
    g = r.groups[0]
    assert g.order_ids == ["A", "B", "C"] and g.access_node_id == "AN-0001"
    assert g.group_id == "PGP-20261026-01" and g.detected_at == D.hm(8, 30)
    assert "3 обращений" in g.comment and "35 мин" in g.comment
    assert r.grouped_order_ids == {"A", "B", "C"} and r.new_group_ids == [g.group_id]


def test_different_nodes_do_not_group() -> None:
    r = detect(
        [rep("A", 0, "AN-0001"), rep("B", 5, "AN-0002"), rep("C", 10, "AN-0003"), rep("D", 15, "AN-0001")]
    )
    assert r.groups == []


def test_outside_window() -> None:
    assert detect([rep("A", 0), rep("B", 40), rep("C", 61)]).groups == []


@pytest.mark.parametrize(("minutes", "expected"), [((0, 30, 60), 1), ((0, 30, 61), 0)])
def test_window_boundary_inclusive(minutes, expected) -> None:
    orders = [rep(f"O{i}", m) for i, m in enumerate(minutes)]
    assert len(detect(orders).groups) == expected


def test_threshold_boundary_and_config() -> None:
    two = [rep("A", 0), rep("B", 10)]
    assert detect(two).groups == []
    assert len(detect(two, cfg=PgpConfig(min_count=2)).groups) == 1
    with pytest.raises(ValueError):
        PgpConfig(min_count=1)


def test_sliding_not_fixed_buckets() -> None:
    # 07:50, 08:10, 08:20 — разные «часовые корзины», но одно окно 60 мин
    orders = [rep("A", 50), rep("B", 70), rep("C", 80)]
    assert len(detect(orders).groups) == 1


def test_group_span_limited_to_window_and_far_burst_separate() -> None:
    # D в 45 мин от C, но в 100 мин от A: разброс группы не превышает окно — D не входит
    orders = [
        rep(x, m) for x, m in [("A", 0), ("B", 30), ("C", 55), ("D", 100), ("E", 300), ("F", 310), ("G", 320)]
    ]
    r = detect(orders, now=D.hm(15))
    assert [g.order_ids for g in r.groups] == [["A", "B", "C"], ["E", "F", "G"]]


def test_duplicate_order_id_counted_once() -> None:
    a = rep("A", 0)
    assert detect([a, a, rep("B", 5)]).groups == []


def test_new_order_joins_existing_group_detected_at_kept() -> None:
    first = detect([rep("A", 0), rep("B", 10), rep("C", 20)])
    later = detect(
        [rep("A", 0), rep("B", 10), rep("C", 20), rep("D", 50), rep("Z", 75)],
        existing=first.groups,
        now=D.hm(9),
    )
    assert len(later.groups) == 1 and later.new_group_ids == []
    assert later.groups[0].order_ids == ["A", "B", "C", "D"]  # Z (75 мин от A) за пределами окна группы
    assert later.groups[0].detected_at == D.hm(8, 30)
    assert first.groups[0].order_ids == ["A", "B", "C"]  # вход не мутируется


def test_rerun_is_idempotent() -> None:
    orders = [rep("A", 0), rep("B", 10), rep("C", 20)]
    first = detect(orders)
    again = detect(orders, existing=first.groups)
    assert [g.order_ids for g in again.groups] == [["A", "B", "C"]] and again.new_group_ids == []


def test_installations_never_form_pgp() -> None:
    orders = [rep(f"I{i}", i * 5, wt=WorkTypeCode.INST_PON_MKD) for i in range(5)]
    assert detect(orders).groups == []
    mixed = [rep("A", 0), rep("B", 5, wt=WorkTypeCode.DEGRADATION), rep("I", 6, wt=WorkTypeCode.DISMANTLE)]
    assert detect(mixed).groups == []


def test_started_order_counts_but_stays_in_route() -> None:
    r = detect([rep("A", 0), rep("B", 10), rep("C", 20)], started=frozenset({"A"}))
    assert r.groups[0].order_ids == ["B", "C"]
    assert "A" not in r.grouped_order_ids and "остаются в маршруте" in r.groups[0].comment


def test_future_and_cancelled_orders_ignored() -> None:
    orders = [rep("A", 0), rep("B", 10), rep("C", 200)]  # C зарегистрирован в 10:20 > now
    assert detect(orders).groups == []
    assert detect([rep("A", 0), rep("B", 10), rep("C", 20)], cancelled=frozenset({"C"})).groups == []
