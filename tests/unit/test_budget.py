from __future__ import annotations

import pytest

from ado.optimization.budget import TimeBudget


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_budget_expires_with_reserve() -> None:
    clock = FakeClock()
    budget = TimeBudget(10, clock=clock, reserve=2)
    assert budget.remaining() == 8
    clock.t = 7.9
    assert not budget.expired()
    clock.t = 8.0
    assert budget.expired()
    assert budget.remaining() == 0


def test_sub_budget_bounded_by_parent() -> None:
    clock = FakeClock()
    parent = TimeBudget(10, clock=clock)
    clock.t = 7
    child = parent.sub_budget(5)
    assert child.remaining() == 3
    assert parent.sub_budget(1).remaining() == 1


def test_zero_budget_is_expired_immediately() -> None:
    b = TimeBudget(0)
    assert b.expired() and b.remaining() == 0


@pytest.mark.parametrize(("seconds", "reserve"), [(-1, 0), (10, 10), (10, -1)])
def test_invalid_budget(seconds: float, reserve: float) -> None:
    with pytest.raises(ValueError):
        TimeBudget(seconds, reserve=reserve)
