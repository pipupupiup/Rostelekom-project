"""Служебный API интерфейса (/ui-api): пустое состояние, демо, шаг, since_version, статика."""

from __future__ import annotations

from ado.api.app import create_app
from ado.api.service import DispatchService
from ado.config import Settings
from ado.events.session import ReoptConfig
from ado.optimization.improvement import ImproveConfig
from tests.asgi import call
from tests.contract.openapi import errors


def test_ui_api_flow() -> None:
    svc = DispatchService(Settings(), ReoptConfig(improve=ImproveConfig(max_iterations=10, time_limit_sec=2)))
    app = create_app(Settings(), svc)
    status, body = call(app, "GET", "/ui-api/state")
    assert status == 200 and body["loaded"] is False
    status, body = call(app, "POST", "/ui-api/demo/step", {"count": 1})
    assert status == 409
    status, body = call(app, "POST", "/ui-api/demo/start", {"n_orders": 80, "time_limit_sec": 10, "seed": 2})
    assert status == 200 and body["synthetic"] and body["plan_version"] == 1
    status, st = call(app, "GET", "/ui-api/state")
    assert st["loaded"] and st["synthetic"] and st["plan_version"] == 1
    assert errors(st["plan"], "Plan") == []
    assert set(st["orders"]) >= {
        s["order_id"] for r in st["plan"]["routes"] for s in r["stops"] if s["order_id"]
    }
    assert st["metrics"]["hard_violations"] == 0 and len(st["staff"]) == 40
    status, same = call(app, "GET", "/ui-api/state?since_version=1")
    assert same == {"loaded": True, "unchanged": True, "plan_version": 1, "marks": {}}
    status, step = call(app, "POST", "/ui-api/demo/step", {"count": 3})
    assert status == 200 and len(step["sent"]) == 3
    status, st2 = call(app, "GET", "/ui-api/state?since_version=1")
    assert st2["plan_version"] == 4 and len(st2["versions"]) == 4 and st2["demo"]["sent"] == 3
    status, _ = call(app, "GET", "/")
    assert status in (302, 307)


def test_ui_state_does_not_block_while_plan_is_built() -> None:
    """Пока сервис держит блокировку (расчёт плана до 300 с), /ui-api/state отвечает сразу «busy»."""
    import threading
    import time

    svc = DispatchService(Settings(), ReoptConfig(improve=ImproveConfig(max_iterations=10, time_limit_sec=2)))
    app = create_app(Settings(), svc)
    release = threading.Event()

    def hold() -> None:
        with svc.lock:
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.05)
    try:
        t0 = time.monotonic()
        status, body = call(app, "GET", "/ui-api/state?since_version=3")
        assert status == 200 and body["busy"] is True and body["plan_version"] == 3
        assert time.monotonic() - t0 < 2
    finally:
        release.set()
        t.join()
    status, body = call(app, "GET", "/ui-api/state")
    assert status == 200 and "busy" not in body


def test_ui_state_new_day_same_date_not_cached() -> None:
    svc = DispatchService(Settings(), ReoptConfig(improve=ImproveConfig(max_iterations=5, time_limit_sec=1)))
    app = create_app(Settings(), svc)
    call(app, "POST", "/ui-api/demo/start", {"n_orders": 60, "time_limit_sec": 2, "seed": 2})
    _, a = call(app, "GET", "/ui-api/state")
    call(app, "POST", "/ui-api/demo/start", {"n_orders": 60, "time_limit_sec": 2, "seed": 5})
    _, b = call(app, "GET", "/ui-api/state")
    assert a["plan"]["date"] == b["plan"]["date"] and a["plan"] != b["plan"]
