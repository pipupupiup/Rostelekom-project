"""ADO-12: запас времени, уровни риска, инсталляция, задержка, каскад, срок предупреждения, дедупликация."""

from __future__ import annotations

from dataclasses import replace

from ado.domain.enums import StopType
from ado.domain.enums import WorkTypeCode as W
from ado.optimization.initial import build_daily_plan
from ado.optimization.risk import (
    DelayPredictor,
    RiskConfig,
    RiskLevel,
    call_due,
    forecast_route,
    predict_delays,
)
from tests import dom as D

hm = D.hm
BLD = ["B-00001", "B-00002", "B-00003"]


def planned(orders, emp=None):
    emp = emp or D.employee()
    ref = D.reference([emp], buildings=BLD)
    day = D.day(ref, orders)
    travel = D.matrix(["SC-01", *BLD])
    res = build_daily_plan(day, travel)
    return res.plan, day, travel


def delay(plan, oid, minutes):
    """Сдвинуть окончание визита (как VISIT_DELAYED) прямо в плане."""
    for r in plan.routes.values():
        for i, s in enumerate(r.stops):
            if s.order_id == oid:
                r.stops[i] = replace(s, end=s.end + minutes * 60)
                return s.start


def test_positive_slack_no_risk() -> None:
    plan, day, travel = planned([D.order("A", building=BLD[0], slot=(hm(10), hm(12)))])
    assert predict_delays(plan, day, travel, hm(9)) == []
    f = forecast_route(plan.routes["ST-001"], day, travel, hm(9))
    assert f[0].slack == hm(2)


def test_zero_and_negative_slack_levels() -> None:
    a = D.order("A", building=BLD[0], slot=(hm(9), hm(9)), latest=hm(9, 20))
    b = D.order("B", building=BLD[1], slot=(hm(10, 40), hm(12)), latest=hm(11))
    plan, day, travel = planned([a, b])
    start = delay(plan, "A", 20)  # B: прибытие 10:40 + 20 = 11:00 → slack 0 → HIGH
    risks = predict_delays(plan, day, travel, start + 60)
    assert [(r.order_id, r.level, r.slack) for r in risks] == [("B", RiskLevel.HIGH, 0)]
    delay(plan, "A", 1)  # ещё на минуту → нарушение
    r = predict_delays(plan, day, travel, start + 60)[0]
    assert r.level is RiskLevel.VIOLATION and r.lateness_min == 1


def test_installation_ten_minute_rule() -> None:
    a = D.order("A", building=BLD[0], slot=(hm(9), hm(9)), latest=hm(9, 20))
    inst = D.order("I", W.ADD_SERVICE, building=BLD[1], slot=(hm(10, 40), hm(12, 40)))
    assert inst.latest_start == hm(10, 50)
    emp = D.employee(profiles=("INSTALL", "LTP3"))
    plan, day, travel = planned([a, inst], emp)
    start = delay(plan, "A", 15)  # прибытие 10:55 > 10:50, хотя слот до 12:40
    r = predict_delays(plan, day, travel, start + 60)[0]
    assert r.order_id == "I" and r.level is RiskLevel.VIOLATION and r.lateness_min == 5


def test_current_work_delay_cascades_to_several_visits() -> None:
    orders = [
        D.order("A", building=BLD[0], slot=(hm(9), hm(9)), latest=hm(9, 20)),
        D.order("B", building=BLD[1], slot=(hm(10, 40), hm(10, 40)), latest=hm(10, 50)),
        D.order("C", building=BLD[2], slot=(hm(12), hm(12)), latest=hm(12, 10)),
    ]
    plan, day, travel = planned(orders)
    start = delay(plan, "A", 90)
    risks = predict_delays(plan, day, travel, start + 60)
    assert {r.order_id for r in risks if r.level is RiskLevel.VIOLATION} == {"B", "C"}


def test_notice_deadline_flag() -> None:
    a = D.order("A", building=BLD[0], slot=(hm(9), hm(9)), latest=hm(9, 20))
    b = D.order("B", building=BLD[1], slot=(hm(10, 40), hm(11)), latest=hm(10, 50))
    plan, day, travel = planned([a, b])
    start = delay(plan, "A", 40)
    early = predict_delays(plan, day, travel, start + 60)[0]  # 9:21 — до 10:10 ещё можно предупредить
    assert early.notice_possible and "уже прошёл" not in early.reason
    late = predict_delays(plan, day, travel, hm(10, 15))[0]
    assert not late.notice_possible and "уже прошёл" in late.reason


def test_call_window() -> None:
    plan, day, _ = planned([D.order("A", building=BLD[0], slot=(hm(11), hm(13)))])
    assert call_due(plan, day, hm(10, 15)) == ["A"]
    assert call_due(plan, day, hm(9)) == [] and call_due(plan, day, hm(10, 40)) == []


def test_predictor_does_not_duplicate() -> None:
    a = D.order("A", building=BLD[0], slot=(hm(9), hm(9)), latest=hm(9, 20))
    b = D.order("B", building=BLD[1], slot=(hm(10, 40), hm(11)), latest=hm(10, 50))
    plan, day, travel = planned([a, b])
    start = delay(plan, "A", 40)
    p = DelayPredictor(RiskConfig())
    new, gone = p.update(plan, day, travel, start + 60)
    assert [r.order_id for r in new] == ["B"] and gone == []
    new2, gone2 = p.update(plan, day, travel, start + 120)
    assert new2 == [] and gone2 == []
    delay(plan, "A", -40)
    new3, gone3 = p.update(plan, day, travel, start + 180)
    # задержка снята: прогноз совпадает с планом — риска нет (узкий запас сам по себе не риск)
    assert [(r.order_id, r.level) for r in gone3] == [("B", RiskLevel.VIOLATION)] and new3 == []


def test_forecast_ignores_started_stops() -> None:
    plan, day, travel = planned([D.order("A", building=BLD[0], slot=(hm(9), hm(11)))])
    visit = next(s for s in plan.routes["ST-001"].stops if s.type is StopType.VISIT)
    assert forecast_route(plan.routes["ST-001"], day, travel, visit.start + 1) == []
