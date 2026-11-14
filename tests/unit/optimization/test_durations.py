"""D39: прогноз длительности по истории."""

from __future__ import annotations

from ado.optimization.durations import DurationModel


def _hist(wt: str, staff: str, mins: list[int]) -> list[dict]:
    return [
        {"work_type_code": wt, "fact": {"outcome": "COMPLETED", "staff_id": staff, "actual_duration_min": m}}
        for m in mins
    ]


def test_staff_slower_than_norm_is_forecast_and_never_below_norm() -> None:
    rows = _hist("REPAIR", "A", [80] * 20) + _hist("REPAIR", "B", [50] * 20)
    rows += [{"work_type_code": "REPAIR", "fact": {"outcome": "CANCELLED", "actual_duration_min": 500}}]
    rows += [{"work_type_code": "REPAIR", "fact": None}]
    m = DurationModel.fit(rows)
    assert m.size == 40
    a = m.predict("REPAIR", "A", 3600)
    b = m.predict("REPAIR", "B", 3600)
    assert a.source == "staff+type" and a.n_staff == 20 and a.seconds > 3600 + 15 * 60
    assert b.seconds == 3600  # быстрее норматива — не меньше норматива (H5: план по нормативу)
    c = m.predict("REPAIR", "NEW", 3600)  # нет истории сотрудника — квантиль по типу
    assert c.source == "type" and c.seconds == 65 * 60  # медиана по типу: (50 + 80) / 2
    p75 = DurationModel.fit(rows, q=0.75).predict("REPAIR", "NEW", 3600)
    assert p75.seconds == 80 * 60


def test_little_history_falls_back_to_norm() -> None:
    m = DurationModel.fit(_hist("DISMANTLE", "A", [90] * 3))
    p = m.predict("DISMANTLE", "A", 1800)
    assert p.source == "norm" and p.seconds == 1800


def test_risk_forecast_uses_durations() -> None:
    """Сотрудник по истории дольше норматива → прогноз начала следующих визитов сдвигается."""
    from ado.api.service import DispatchService
    from ado.config import Settings
    from ado.events.session import ReoptConfig
    from ado.optimization.improvement import ImproveConfig
    from ado.optimization.risk import forecast_route
    from ado.synthetic import generate_day

    syn = generate_day(n_orders=120, seed=4)
    svc = DispatchService(Settings(), ReoptConfig(improve=ImproveConfig(max_iterations=5, time_limit_sec=1)))
    date = syn.day_input["date"]
    svc.put_reference(syn.reference)
    svc.put_travel_matrix(syn.matrix)
    svc.put_day(date, syn.day_input)
    svc.post_plan(date, {"time_limit_sec": 5})
    s = svc.session
    r = max(s.plan.routes.values(), key=lambda rt: len(rt.stops))
    plain = forecast_route(r, s.day, s.travel, s.now)
    slow = forecast_route(r, s.day, s.travel, s.now, durations=lambda oid, st: 2 * 3600)
    assert len(plain) == len(slow) >= 2
    assert slow[-1].forecast_start > plain[-1].forecast_start
