"""Сквозной прогон ADO-01 → ADO-02 → ADO-09 на СИНТЕТИЧЕСКОМ дне полного масштаба.

Проверяет: данные проходят подготовку без предупреждений о сроках, план строится в пределах
лимита, независимый валидатор не находит нарушений, результат не хуже baseline и воспроизводим.
Цифры качества на синтетике не являются результатами на данных организаторов.
"""

from __future__ import annotations

import pytest

from ado.data.prepare import prepare_day_report, prepare_reference_report
from ado.optimization.baseline import build_baseline_plan
from ado.optimization.initial import build_daily_plan
from ado.routing.matrix import MatrixTravelProvider, TravelMatrix
from ado.synthetic import generate_day


@pytest.fixture(scope="module")
def instance():
    syn = generate_day(n_orders=400, seed=7)
    ref = prepare_reference_report(syn.reference)
    day = prepare_day_report(syn.day_input, ref.value)
    matrix = TravelMatrix.from_api(syn.matrix)
    matrix.check_covers([*ref.value.service_centers, *ref.value.buildings])
    return syn, ref, day, MatrixTravelProvider(matrix)


def test_preparation_clean(instance) -> None:
    __syn, ref, day, _ = instance
    assert not [w for w in day.warnings if "deadline" in w.field or "latest_start" in w.field]
    assert len(ref.value.employees) == 40 and len(ref.value.service_centers) == 5
    assert 300 <= len(day.value.orders) <= 400


def test_initial_plan_valid_fast_and_better_than_baseline(instance) -> None:
    _, _, prepared, travel = instance
    day = prepared.value
    res = build_daily_plan(day, travel)
    assert res.issues == []
    assert res.stats["total_sec"] < 60
    base = build_baseline_plan(day, travel)
    assert base.issues == []
    assert res.metrics.weighted_success >= base.metrics.weighted_success
    assert res.metrics.travel_sec_total <= base.metrics.travel_sec_total
    # учёт H9: каждый наряд ровно в одном месте
    assigned = res.working.assigned()
    pgp = {o for g in res.plan.pgp_groups for o in g.order_ids}
    assert set(assigned) | set(res.plan.unassigned) | pgp == set(day.orders)
    again = build_daily_plan(day, travel)
    assert {s: r.stops for s, r in again.plan.routes.items()} == {
        s: r.stops for s, r in res.plan.routes.items()
    }
