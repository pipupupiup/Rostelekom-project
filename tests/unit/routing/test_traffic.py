"""D38: загруженность дорог по часу отправления."""

from __future__ import annotations

from ado.api.service import DispatchService
from ado.config import Settings
from ado.events.session import ReoptConfig
from ado.optimization.improvement import ImproveConfig
from ado.optimization.validator import validate_plan
from ado.routing.matrix import MatrixTravelProvider, TrafficTravelProvider, TravelMatrix
from ado.synthetic import generate_day

CFG = ReoptConfig(improve=ImproveConfig(max_iterations=5, time_limit_sec=1))


def _matrix() -> TravelMatrix:
    return TravelMatrix.from_api(
        {
            "matrix_id": "t",
            "points": ["A", "B"],
            "durations_sec": [[0, 1000], [1000, 0]],
            "distances_m": [[0, 5000], [5000, 0]],
            "hourly_factors": [{"hour": 8, "factor": 1.5}, {"hour": 3, "factor": 0.5}],
        }
    )


def test_factor_by_departure_hour_and_never_below_one() -> None:
    p = TrafficTravelProvider(_matrix())
    assert p.travel_sec("A", "B") == 1000  # базовое — для отсечений
    assert p.factor_at(8 * 3600) == 1.5 and p.factor_at(9 * 3600 - 1) == 1.5
    assert p.factor_at(9 * 3600) == 1.0
    assert p.factor_at(3 * 3600) == 1.0  # < 1 не применяется: H6 проверяется по базовой матрице


def _run(traffic: bool):
    syn = generate_day(n_orders=120, seed=4)
    svc = DispatchService(Settings(traffic=traffic), CFG)
    date = syn.day_input["date"]
    svc.put_reference(syn.reference)
    svc.put_travel_matrix(syn.matrix)
    svc.put_day(date, syn.day_input)
    assert svc.post_plan(date, {"time_limit_sec": 5})[0] == 200
    return svc.session


def test_plan_with_traffic_is_slower_but_valid_by_base_matrix() -> None:
    base, jam = _run(False), _run(True)
    assert isinstance(jam.travel, TrafficTravelProvider) and not isinstance(
        base.travel, TrafficTravelProvider
    )
    # по базовой матрице (как у валидатора организаторов) план с пробками допустим
    issues = validate_plan(jam.plan, jam.day, MatrixTravelProvider(jam.travel.matrix))
    assert issues == []
    # хотя бы одна поездка в час пик стала длиннее базовой
    longer = 0
    for r in jam.plan.routes.values():
        for a, b in zip(r.stops, r.stops[1:], strict=False):
            base_tt = jam.travel.travel_sec(a.location_id, b.location_id) or 0
            if b.travel_sec > base_tt:
                longer += 1
    assert longer > 0
