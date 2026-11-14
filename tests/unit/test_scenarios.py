"""D40: сценарный анализ на синтетическом дне."""

from __future__ import annotations

import pytest

from ado.api.service import DispatchService
from ado.config import Settings
from ado.scenarios import ScenarioInput, run_scenario, start_of_day
from ado.synthetic import generate_day


@pytest.fixture(scope="module")
def inp() -> ScenarioInput:
    syn = generate_day(n_orders=150, seed=5)
    svc = DispatchService(Settings())
    d = syn.day_input["date"]
    svc.put_reference(syn.reference)
    svc.put_travel_matrix(syn.matrix)
    svc.put_day(d, syn.day_input)
    return ScenarioInput(svc.session.day, svc.matrix)


def test_start_of_day_excludes_late_orders(inp: ScenarioInput) -> None:
    d = start_of_day(inp.day)
    assert all(o.registered_at <= d.now for o in d.orders.values())


def test_fewer_staff_reduces_capacity(inp: ScenarioInput) -> None:
    r = run_scenario(inp, "fewer_staff", {"n": 10, "samples": 2}, limit=1)
    assert r["scenario"]["success"] < r["base"]["success"]
    assert r["base"]["hard_violations"] == 0 and all(x["hard_violations"] == 0 for x in r["runs"])


def test_backpack_full_stock_removes_restocks(inp: ScenarioInput) -> None:
    r = run_scenario(inp, "backpack", {}, limit=1)
    assert all(r["recommended_start"][k] <= r["capacity"][k] for k in r["capacity"])
    assert r["scenario"]["restocks"] <= r["base"]["restocks"] and r["scenario"]["hard_violations"] == 0


def test_unknown_kind(inp: ScenarioInput) -> None:
    with pytest.raises(ValueError):
        run_scenario(inp, "x", {})
