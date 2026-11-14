"""ADO-14: бюджет проходит через всю цепочку; ноль / почти исчерпан / постоянные улучшения; атомарность."""

from __future__ import annotations

import pytest

from ado.domain.enums import UnassignedReasonCode as R
from ado.domain.events import NewOrderEvent
from ado.events.session import DaySession, ReoptConfig
from ado.optimization.budget import StageTimer, TimeBudget
from ado.optimization.evaluation import compare_plans, working_metrics
from ado.optimization.improvement import ImproveConfig, RouteImprover
from ado.optimization.initial import build_daily_plan
from tests import dom as D

hm = D.hm
BLD = [f"B-{i:05d}" for i in range(1, 12)]


class TickClock:
    """Подменяемые часы: каждое обращение сдвигает время на `step` секунд."""

    def __init__(self, step: float) -> None:
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def instance(n=8, emps=2):
    ref = D.reference([D.employee(f"ST-00{i}") for i in range(1, emps + 1)], buildings=BLD)
    orders = [D.order(f"O{i}", building=BLD[i % 11], slot=(hm(9 + i % 8), hm(11 + i % 8))) for i in range(n)]
    return D.day(ref, orders), D.matrix(["SC-01", *BLD])


def test_zero_budget_returns_valid_plan_with_reasons() -> None:
    day, travel = instance()
    res = build_daily_plan(day, travel, budget=TimeBudget(0))
    assert res.issues == [] and res.stats["stopped_by_budget"] == 1
    assert {r.code for r in res.plan.unassigned.values()} == {R.OTHER}


def test_nearly_exhausted_budget_stops_midway_valid() -> None:
    day, travel = instance(n=10)
    full = build_daily_plan(day, travel)
    clock = TickClock(0.01)
    res = build_daily_plan(day, travel, budget=TimeBudget(0.06, clock=clock))
    assert res.issues == []
    assert 0 < len(res.working.assigned()) < len(full.working.assigned())


def test_budget_not_exhausted_completes() -> None:
    day, travel = instance()
    res = build_daily_plan(day, travel, budget=TimeBudget(60))
    assert res.stats["stopped_by_budget"] == 0 and res.issues == []


def test_alns_with_constant_progress_stops_by_budget() -> None:
    day, travel = instance(n=10, emps=3)
    base = build_daily_plan(day, travel)
    imp = RouteImprover(ImproveConfig(max_iterations=10**6, max_no_improve=10**6, time_limit_sec=10**6))
    out = imp.improve(base.working, TimeBudget(5.0, clock=TickClock(0.001)))
    assert imp.last_stats["alns_iterations"] < 10**6
    assert compare_plans(working_metrics(out), working_metrics(base.working)) >= 0  # лучший допустимый


def test_stage_timer() -> None:
    clock = TickClock(0.5)
    t = StageTimer(clock)
    with t.stage("a"):
        pass
    with t.stage("a"):
        pass
    assert t.ms["a"] == 1000


def test_event_failure_leaves_state_unchanged(monkeypatch) -> None:
    day, travel = instance()
    s = DaySession(day, travel, ReoptConfig(improve=ImproveConfig(max_iterations=5)))
    s.build_plan(30)
    before = (dict(s.day.orders), s.version, s.plan, dict(s.not_before))
    new = D.order("NEW", building=BLD[3], slot=(hm(13), hm(15)), registered=hm(10))

    def boom(*a, **k):
        raise RuntimeError("сбой посреди обработки")

    monkeypatch.setattr(s, "_reconcile", boom)
    with pytest.raises(RuntimeError):
        s.apply(NewOrderEvent("E1", hm(10), new))
    assert (dict(s.day.orders), s.version, s.plan, dict(s.not_before)) == before
    assert "E1" not in s.results
    monkeypatch.undo()
    out = s.apply(NewOrderEvent("E1", hm(10), new))  # после сбоя то же событие обрабатывается штатно
    assert out.plan_version == 2 and set(out.stages_ms) >= {"freeze", "apply_event", "validate"}
