"""ADO-14: интеграционный замер реакции на события на полном СИНТЕТИЧЕСКОМ дне (400 нарядов, ~90 событий).

Проверяет весь путь API (разбор, перепланирование, проверка, сериализация), а не только солвер.
Порог теста — 10 с из спецификации; фактические значения — в отчёте бенчмарка (десятки мс).
"""

from __future__ import annotations

from ado.api.service import DispatchService
from ado.config import Settings
from ado.events.session import ReoptConfig
from ado.optimization.improvement import ImproveConfig
from ado.orchestrator import run_day, service_client, synthetic_scenario
from ado.synthetic import generate_day


def test_every_event_answered_within_limit() -> None:
    syn = generate_day(n_orders=400, seed=5)
    scenario = synthetic_scenario(syn.day_input, syn.late_orders, syn.reference, seed=5)
    svc = DispatchService(Settings(), ReoptConfig(improve=ImproveConfig(max_iterations=30, time_limit_sec=5)))
    run = run_day(
        service_client(svc, syn.day_input["date"]),
        syn.reference,
        syn.matrix,
        syn.day_input,
        scenario,
        time_limit_sec=30,
    )
    assert run.errors == []
    events = [c for c in run.calls if c.name.startswith("event")]
    assert len(events) > 50
    assert max(c.seconds for c in events) < 10.0
    plan_call = next(c for c in run.calls if c.name == "plan")
    assert plan_call.seconds < 30.0
