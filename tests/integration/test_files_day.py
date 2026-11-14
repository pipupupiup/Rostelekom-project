"""Полный день по каталогу файлов в формате организаторов (СИНТЕТИКА, `tests/fixtures/files_day`).

Проверяет путь «файлы → prepare_data → оркестратор → сервис» и оператор «якоря» (D32).
"""

from __future__ import annotations

from datetime import time
from pathlib import Path
from types import SimpleNamespace

from ado.api.service import DispatchService
from ado.benchmark import analyze, variant_config
from ado.config import Settings
from ado.data.prepare import _read_json, prepare_data
from ado.orchestrator import run_day, service_client

DATA = Path(__file__).resolve().parents[1] / "fixtures" / "files_day"


def test_files_day_end_to_end() -> None:
    ds = prepare_data(DATA)
    assert not ds.warnings and list(ds.days) == ["2026-10-26"]
    scenario = _read_json(DATA / "events_example.json")
    day_input, late = ds.days["2026-10-26"].day_input(time(8, 30))
    assert len(day_input["orders"]) == 67
    assert {o["order_id"] for o in late} == {
        e["order"]["order_id"] for e in scenario["events"] if e["type"] == "NEW_ORDER"
    }
    svc = DispatchService(Settings(), variant_config("basic", 5))
    run = run_day(
        service_client(svc, "2026-10-26"), ds.reference_raw, ds.travel_matrix_raw, day_input, scenario
    )
    assert run.errors == []
    res = analyze(svc.session, run, SimpleNamespace(pgp_incidents=[]))
    assert res["violations_algorithm"] == 0 and res["violations_event"] == 0
    plan = svc.session.plan
    assert sorted(plan.cancelled) == [
        "ORD-20261026-0008",
        "ORD-20261026-0022",
        "ORD-20261026-0052",
        "ORD-20261026-0063",
    ]
    # ВЛС ORD-0017 (начало 13:00–13:10, 3 ч) встаёт только после переноса соседнего ORD-0053:
    # перерыв берётся на месте предыдущей остановки, из СЦ после обеда к 13:10 не успеть (D32)
    stops = {s.order_id: (sid, s) for sid, r in plan.routes.items() for s in r.stops if s.order_id}
    sid, st = stops["ORD-20261026-0017"]
    assert stops["ORD-20261026-0053"][0] == sid
    assert "перенесён соседний наряд ORD-20261026-0053" in (st.explanation or "")


def test_files_day_breaks_staggered_within_input_windows() -> None:
    """D36: обед смены 09–18 кончается до обеда смены 12–21; во время каждого перерыва работает
    другая смена; все перерывы — в исходных окнах из staff.json (проверка по входным данным)."""
    from dataclasses import replace

    from ado.domain.enums import StopType
    from ado.optimization.validator import validate_plan

    ds = prepare_data(DATA)
    scenario = _read_json(DATA / "events_example.json")
    day_input, _ = ds.days["2026-10-26"].day_input(time(8, 30))
    svc = DispatchService(Settings(), variant_config("basic", 5))
    run_day(service_client(svc, "2026-10-26"), ds.reference_raw, ds.travel_matrix_raw, day_input, scenario)
    s = svc.session
    bp = s.break_plan.to_json()
    assert bp["ok"] and bp["changed"]
    assert [(g["shift"], g["effective_window"]) for g in bp["groups"]] == [
        ("09:00–18:00", "12:00–14:00"),
        ("12:00–21:00", "15:00–17:00"),
    ]
    plan = s.plan
    original = replace(s.day, reference=svc.reference)  # окна из входных данных, не суженные
    issues = validate_plan(plan, original, s.travel, end_overrides=s.end_overrides, not_before=s.not_before)
    assert [i for i in issues if i.code.value.startswith("H3")] == []
    emp = svc.reference.employees
    breaks = [
        (sid, st.start, st.end)
        for sid, r in plan.routes.items()
        for st in r.stops
        if st.type is StopType.BREAK
    ]
    assert breaks
    for sid, b0, b1 in breaks:
        mine = (emp[sid].shift.start, emp[sid].shift.end)
        others_working = [
            o
            for o, e in emp.items()
            if (e.shift.start, e.shift.end) != mine
            and e.shift.start <= b0
            and b1 <= e.shift.end
            and not any(x == o and x0 < b1 and b0 < x1 for x, x0, x1 in breaks)
        ]
        assert others_working, f"{sid}: во время перерыва {b0}–{b1} другая смена не работает"
