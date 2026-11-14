"""ADO-16: полный цикл API по сценарию оркестратора + проверка каждого ответа по api_spec.yaml."""

from __future__ import annotations

import pytest

from ado.api.app import create_app
from ado.api.service import DispatchService
from ado.config import Settings
from ado.events.session import ReoptConfig
from ado.optimization.improvement import ImproveConfig
from ado.synthetic import generate_day
from tests.asgi import call
from tests.contract.openapi import errors, response_schema, spec

DATE = "2026-10-26"
P = "/api/v1"


def check(method: str, spec_path: str, status: int, body) -> None:
    allowed = spec()["paths"][spec_path][method]["responses"]
    assert str(status) in allowed, f"{method.upper()} {spec_path}: код {status} не описан в спецификации"
    name = response_schema(spec_path, method, status)
    assert name is not None
    errs = errors(body, name)
    assert not errs, f"{name}: {errs[:5]}"


@pytest.fixture(scope="module")
def world():
    syn = generate_day(n_orders=90, seed=3, n_nodes=30, pgp_incidents=2)
    service = DispatchService(
        Settings(team="ADO-test"), ReoptConfig(improve=ImproveConfig(max_iterations=25, time_limit_sec=5))
    )
    app = create_app(Settings(team="ADO-test"), service)
    return syn, app, service


def test_health(world) -> None:
    _, app, _ = world
    status, body = call(app, "GET", f"{P}/health")
    assert status == 200 and body["status"] == "ok"
    check("get", "/api/v1/health", status, body)


def test_order_errors_before_loading(world) -> None:
    syn, app, _ = world
    status, body = call(app, "PUT", f"{P}/days/{DATE}", syn.day_input)
    check("put", "/api/v1/days/{date}", status, body)
    assert status == 409 and body["error"]["code"] == "REFERENCE_NOT_LOADED"
    status, body = call(app, "GET", f"{P}/days/{DATE}/plan")
    check("get", "/api/v1/days/{date}/plan", status, body)
    assert status == 404


def test_full_cycle(world) -> None:
    syn, app, service = world
    status, body = call(app, "PUT", f"{P}/reference", syn.reference)
    check("put", "/api/v1/reference", status, body)
    assert status == 200 and body["counts"]["staff"] == 40
    status, body = call(app, "PUT", f"{P}/reference/travel-matrix", syn.matrix)
    check("put", "/api/v1/reference/travel-matrix", status, body)
    assert status == 200
    status, body = call(app, "PUT", f"{P}/days/{DATE}", syn.day_input)
    check("put", "/api/v1/days/{date}", status, body)
    assert status == 200

    status, plan = call(app, "POST", f"{P}/days/{DATE}/plan", {"time_limit_sec": 30})
    check("post", "/api/v1/days/{date}/plan", status, plan)
    assert status == 200 and plan["plan_version"] == 1 and plan["based_on_event_id"] is None
    assert plan["as_of"] == f"{DATE}T08:30:00+05:00"
    assert len(plan["routes"]) == 40
    seqs = [[s["seq"] for s in r["stops"]] for r in plan["routes"]]
    assert all(seq == list(range(1, len(seq) + 1)) for seq in seqs)
    placed = {s["order_id"] for r in plan["routes"] for s in r["stops"] if s["type"] == "VISIT"}
    placed |= {u["order_id"] for u in plan["unassigned"]} | {
        o for g in plan["pgp_groups"] for o in g["order_ids"]
    }
    assert placed == {o["order_id"] for o in syn.day_input["orders"]}

    status, again = call(app, "GET", f"{P}/days/{DATE}/plan")
    check("get", "/api/v1/days/{date}/plan", status, again)
    assert again == plan

    # события: новые наряды (как NEW_ORDER), ход времени, отмена
    version = 1
    for i, o in enumerate(syn.late_orders[:5]):
        ev = {"event_id": f"EV-{i:04d}", "type": "NEW_ORDER", "timestamp": o["registered_at"], "order": o}
        status, res = call(app, "POST", f"{P}/days/{DATE}/events", ev)
        check("post", "/api/v1/days/{date}/events", status, res)
        assert status == 200 and res["plan_version"] == version + 1 and not res["duplicate"]
        version += 1
        status, cur = call(app, "GET", f"{P}/days/{DATE}/plan")
        check("get", "/api/v1/days/{date}/plan", status, cur)
        assert cur["plan_version"] == version and cur["based_on_event_id"] == f"EV-{i:04d}"

    # повтор события
    ev = {
        "event_id": "EV-0000",
        "type": "NEW_ORDER",
        "timestamp": syn.late_orders[0]["registered_at"],
        "order": syn.late_orders[0],
    }
    status, res = call(app, "POST", f"{P}/days/{DATE}/events", ev)
    check("post", "/api/v1/days/{date}/events", status, res)
    assert res["duplicate"] and res["plan_version"] == 2

    last_ts = syn.late_orders[4]["registered_at"]
    tick = {"event_id": "EV-T", "type": "TIME_TICK", "timestamp": last_ts}
    status, res = call(app, "POST", f"{P}/days/{DATE}/events", tick)
    assert status == 200 and res["plan_version"] == version + 1
    version += 1

    # событие из прошлого → 409
    old = {"event_id": "EV-OLD", "type": "TIME_TICK", "timestamp": f"{DATE}T08:00:00+05:00"}
    status, res = call(app, "POST", f"{P}/days/{DATE}/events", old)
    check("post", "/api/v1/days/{date}/events", status, res)
    assert status == 409 and res["error"]["code"] == "EVENT_OUT_OF_ORDER"

    # неизвестный наряд → 422; некорректное событие → 400
    bad = {
        "event_id": "EV-X",
        "type": "ORDER_CANCELLED",
        "timestamp": last_ts,
        "order_id": "ORD-20261026-9999",
        "reason": "NOT_CONFIRMED",
    }
    status, res = call(app, "POST", f"{P}/days/{DATE}/events", bad)
    check("post", "/api/v1/days/{date}/events", status, res)
    assert status == 422 and res["error"]["code"] == "UNKNOWN_ORDER"
    status, res = call(app, "POST", f"{P}/days/{DATE}/events", {"event_id": "EV-Y", "type": "TIME_TICK"})
    check("post", "/api/v1/days/{date}/events", status, res)
    assert status == 400 and res["error"]["details"][0]["field"] == "timestamp"

    # отмена неначатого визита
    _, cur = call(app, "GET", f"{P}/days/{DATE}/plan")
    later = next(
        s for r in cur["routes"] for s in r["stops"] if s["type"] == "VISIT" and s["start"] > last_ts
    )
    cancel = {
        "event_id": "EV-C",
        "type": "ORDER_CANCELLED",
        "timestamp": last_ts,
        "order_id": later["order_id"],
        "reason": "NOT_CONFIRMED",
    }
    status, res = call(app, "POST", f"{P}/days/{DATE}/events", cancel)
    check("post", "/api/v1/days/{date}/events", status, res)
    _, cur = call(app, "GET", f"{P}/days/{DATE}/plan")
    assert later["order_id"] in {c["order_id"] for c in cur["cancelled"]}

    # повторное построение после событий → 409
    status, res = call(app, "POST", f"{P}/days/{DATE}/plan", None)
    check("post", "/api/v1/days/{date}/plan", status, res)
    assert status == 409
    assert all(dt < 10 for name, dt in service.timings if "events" in name)


def test_bad_inputs(world) -> None:
    syn, app, _ = world
    status, body = call(app, "PUT", f"{P}/reference", raw=b"{not json")
    check("put", "/api/v1/reference", status, body)
    assert status == 400
    ref = {**syn.reference, "staff": [{**syn.reference["staff"][0], "service_center_id": "SC-99"}]}
    status, body = call(app, "PUT", f"{P}/reference", ref)
    check("put", "/api/v1/reference", status, body)
    assert status == 422 and body["error"]["details"][0]["field"] == "staff[0].service_center_id"
    status, body = call(app, "POST", f"{P}/days/{DATE}/plan", {"time_limit_sec": 301})
    assert status == 400
    status, body = call(app, "PUT", f"{P}/days/2026-13-40", syn.day_input)
    check("put", "/api/v1/days/{date}", status, body)
    assert status == 400


def test_spec_examples_match_their_schemas() -> None:
    ex = spec()["components"]["examples"]
    for name, schema in [
        ("PlanExample", "Plan"),
        ("EventResultExample", "EventResult"),
        ("DayInputExample", "DayInput"),
        ("ReferenceDataExample", "ReferenceData"),
    ]:
        assert errors(ex[name]["value"], schema) == [], name
