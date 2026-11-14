from __future__ import annotations

from dataclasses import replace

from ado.domain.enums import StopStatus, StopType
from ado.domain.models import Order, Plan, PlanMetrics, RiskItem, Route, RouteStop


def test_repeat_weight(synthetic_order: Order) -> None:
    assert synthetic_order.weight == 1
    assert replace(synthetic_order, is_repeat=True).weight == 2


def test_start_window_uses_latest_start(synthetic_order: Order) -> None:
    install_like = replace(synthetic_order, latest_start=synthetic_order.slot.start + 600)
    window = install_like.start_window
    assert window.contains(synthetic_order.slot.start)
    assert window.contains(synthetic_order.slot.start + 600)
    assert not window.contains(synthetic_order.slot.start + 601)
    assert not window.contains(synthetic_order.slot.start - 1)


def test_stop_freeze_rule_is_strict_less() -> None:
    stop = RouteStop(StopType.VISIT, "B-1", arrival=100, start=200, end=300, order_id="O")
    assert not stop.is_started(200)  # start < t — строго
    assert stop.is_started(201)
    assert stop.status_at(150) is StopStatus.PLANNED
    assert stop.status_at(250) is StopStatus.IN_PROGRESS
    assert stop.status_at(300) is StopStatus.DONE


def test_plan_assigned_orders() -> None:
    route = Route(
        "ST-001",
        "SC-01",
        stops=[
            RouteStop(StopType.DEPOT_START, "SC-01", 0, 0, 0),
            RouteStop(StopType.VISIT, "B-1", 10, 10, 20, order_id="O1"),
            RouteStop(StopType.BREAK, "B-1", 20, 20, 30),
            RouteStop(StopType.DEPOT_END, "SC-01", 40, 40, 40),
        ],
    )
    plan = Plan(date="2026-10-26", plan_version=1, as_of=0, routes={"ST-001": route})
    assert plan.assigned_order_ids() == {"O1": "ST-001"}


def _metrics(**kw) -> PlanMetrics:
    base = dict(
        weighted_success=100,
        success_count=90,
        orders_total=100,
        reschedules=5,
        travel_sec_total=50_000,
        utilization={},
        utilization_cv=0.2,
        hard_violations=0,
    )
    base.update(kw)
    return PlanMetrics(**base)


def test_lexicographic_priority() -> None:
    # успех важнее любой экономии дороги
    assert _metrics(weighted_success=101, travel_sec_total=999_999).key() > _metrics().key()
    # при равном успехе — меньше переносов
    assert _metrics(reschedules=4, travel_sec_total=60_000).key() > _metrics().key()
    # при равных переносах — меньше дороги
    assert _metrics(travel_sec_total=49_000, utilization_cv=0.9).key() > _metrics().key()
    # нарушения жёстких ограничений хуже всего
    assert _metrics(weighted_success=0).key() > _metrics(weighted_success=200, hard_violations=1).key()


def test_risk_slack() -> None:
    assert RiskItem("O", "ST", forecast_start=1000, latest_start=1600).slack == 600
