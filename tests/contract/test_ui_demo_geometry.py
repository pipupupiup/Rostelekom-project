"""D34 (демо отдельно от дня из API, выход из демо) и D35 (дорожная геометрия через OSRM)."""

from __future__ import annotations

import contextlib
import itertools
import json

from ado.api.app import create_app
from ado.api.service import DispatchService
from ado.config import Settings
from ado.events.session import ReoptConfig
from ado.optimization.improvement import ImproveConfig
from ado.routing.geometry import (
    GraphHopperRouteClient,
    OsrmRouteClient,
    RoadGeometryError,
    RouteGeometryService,
    routing_from_settings,
)
from ado.synthetic import generate_day
from tests.asgi import call

CFG = ReoptConfig(improve=ImproveConfig(max_iterations=5, time_limit_sec=1))
DEMO = {"n_orders": 40, "time_limit_sec": 5, "seed": 3}


def _api_day(app) -> str:
    """День через публичный API (как у оркестратора)."""
    syn = generate_day(n_orders=30, seed=11)
    date = syn.day_input["date"]
    assert call(app, "PUT", "/api/v1/reference", syn.reference)[0] == 200
    assert call(app, "PUT", "/api/v1/reference/travel-matrix", syn.matrix)[0] == 200
    assert call(app, "PUT", f"/api/v1/days/{date}", syn.day_input)[0] == 200
    assert call(app, "POST", f"/api/v1/days/{date}/plan", {"time_limit_sec": 5})[0] == 200
    return date


def test_demo_does_not_touch_api_day_and_exit_returns_to_it() -> None:
    svc = DispatchService(Settings(), CFG)
    app = create_app(Settings(), svc)
    date = _api_day(app)
    _, plan_before = call(app, "GET", f"/api/v1/days/{date}/plan")
    _, st = call(app, "GET", "/ui-api/state")
    assert st["synthetic"] is False and st["demo"]["active"] is False

    assert call(app, "POST", "/ui-api/demo/start", DEMO)[0] == 200
    _, st = call(app, "GET", "/ui-api/state")
    assert st["synthetic"] is True and st["demo"]["active"] is True
    call(app, "POST", "/ui-api/demo/step", {"count": 2})
    # публичный API по-прежнему отдаёт день из API, а не демо
    _, plan_during = call(app, "GET", f"/api/v1/days/{date}/plan")
    assert plan_during == plan_before

    status, out = call(app, "POST", "/ui-api/demo/exit")
    assert status == 200 and out == {"exited": True, "showing": "api_day", "date": date}
    _, st = call(app, "GET", "/ui-api/state")
    assert st["synthetic"] is False and st["demo"]["active"] is False
    assert st["plan_version"] == plan_before["plan_version"]
    # повторный запуск демо после выхода — новый демо-день
    assert call(app, "POST", "/ui-api/demo/start", DEMO)[0] == 200
    assert call(app, "GET", "/ui-api/state")[1]["demo"]["sent"] == 0


def test_demo_exit_without_api_day_returns_to_start_screen() -> None:
    app = create_app(Settings(), DispatchService(Settings(), CFG))
    assert call(app, "POST", "/ui-api/demo/exit")[1]["exited"] is False
    assert call(app, "POST", "/ui-api/demo/start", DEMO)[0] == 200
    out = call(app, "POST", "/ui-api/demo/exit")[1]
    assert out == {"exited": True, "showing": "start", "date": None}
    assert call(app, "GET", "/ui-api/state")[1]["loaded"] is False


def test_state_reports_break_plan() -> None:
    app = create_app(Settings(), DispatchService(Settings(), CFG))
    call(app, "POST", "/ui-api/demo/start", DEMO)
    br = call(app, "GET", "/ui-api/state")[1]["breaks"]
    assert br["ok"] is True and [g["effective_window"] for g in br["groups"]] == [
        "12:00–14:00",
        "15:00–17:00",
    ]


# ---------------------------------------------------------------- геометрия


class FakeOsrm:
    """Тестовый транспорт: «дорога» — ломаная через промежуточную точку между остановками."""

    def __init__(self, fail: Exception | None = None, code: str = "Ok") -> None:
        self.fail, self.code, self.urls = fail, code, []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.urls.append(url)
        if self.fail:
            raise self.fail
        locs = [tuple(map(float, p.split(","))) for p in url.split("/driving/")[1].split("?")[0].split(";")]
        line = []
        for (x1, y1), (x2, _y2) in itertools.pairwise(locs):
            line += [[x1, y1], [x2, y1]]
        line.append(list(locs[-1]))
        body = {
            "code": self.code,
            "routes": [
                {"geometry": {"type": "LineString", "coordinates": line}, "distance": 1234.5, "duration": 300}
            ],
            "waypoints": [{"distance": 12.0} for _ in locs],
        }
        return (200 if self.code == "Ok" else 400), json.dumps(body).encode()


def test_route_client_parses_geojson() -> None:
    t = FakeOsrm()
    r = OsrmRouteClient("http://osrm:5000/", transport=t).route([(56.8, 60.6), (56.9, 60.7)])
    assert t.urls[0].startswith("http://osrm:5000/route/v1/driving/60.600000,56.800000;60.700000,56.900000?")
    assert "geometries=geojson" in t.urls[0] and "overview=full" in t.urls[0]
    assert r.coordinates == [(56.8, 60.6), (56.8, 60.7), (56.9, 60.7)]  # (lat, lon)
    assert r.to_json()["distance_km"] == 1.23 and r.max_snap_m == 12


def test_route_client_errors() -> None:
    import pytest

    with pytest.raises(RoadGeometryError, match="недоступен"):
        OsrmRouteClient("http://x", transport=FakeOsrm(fail=ConnectionRefusedError("refused"))).route(
            [(1, 2), (3, 4)]
        )
    with pytest.raises(RoadGeometryError, match="NoRoute"):
        OsrmRouteClient("http://x", transport=FakeOsrm(code="NoRoute")).route([(1, 2), (3, 4)])
    with pytest.raises(RoadGeometryError, match="не настроена"):
        RouteGeometryService(None).route([(1, 2), (3, 4)])


def test_geometry_service_cache_and_backoff() -> None:
    now = [0.0]
    down = FakeOsrm(fail=TimeoutError("timed out"))
    g = RouteGeometryService(
        OsrmRouteClient("http://x", transport=down), clock=lambda: now[0], retry_after_sec=30
    )
    for _ in range(3):
        with contextlib.suppress(RoadGeometryError):
            g.route([(1, 2), (3, 4)])
    assert len(down.urls) == 1  # после ошибки связи — пауза, UI не долбит сервис
    now[0] = 31
    ok = FakeOsrm()
    g.client._transport = ok  # сервис ожил
    g.route([(1, 2), (3, 4)])
    g.route([(1, 2), (3, 4)])
    assert len(ok.urls) == 1  # кеш


def test_geometry_endpoint_unconfigured_and_configured() -> None:
    svc = DispatchService(Settings(), CFG)
    app = create_app(Settings(), svc)
    assert call(app, "GET", "/ui-api/route-geometry")[0] == 409
    call(app, "POST", "/ui-api/demo/start", DEMO)
    st = call(app, "GET", "/ui-api/state")[1]
    sid = next(r["staff_id"] for r in st["plan"]["routes"] if r["totals"]["visits"])
    status, g = call(app, "GET", f"/ui-api/route-geometry?staff_id={sid}")
    assert status == 200 and g["available"] is False and g["configured"] is False
    assert g["routes"] == {} and "ADO_GRAPHHOPPER_URL" in g["message"] and "ADO_OSRM_URL" in g["message"]

    fake = FakeOsrm()
    app2 = create_app(
        Settings(), svc, geometry=routing_from_settings(None, osrm_url="http://osrm:5000", transport=fake)
    )
    call(app2, "POST", "/ui-api/demo/start", DEMO)
    status, g = call(app2, "GET", f"/ui-api/route-geometry?staff_id={sid}")
    assert status == 200 and g["available"] is True and g["source"] == "OSRM http://osrm:5000"
    route = next(r for r in call(app2, "GET", "/ui-api/state")[1]["plan"]["routes"] if r["staff_id"] == sid)
    distinct = [
        s["location_id"]
        for i, s in enumerate(route["stops"])
        if i == 0 or s["location_id"] != route["stops"][i - 1]["location_id"]
    ]
    assert len(g["routes"][sid]["coordinates"]) == 2 * (len(distinct) - 1) + 1
    assert len(fake.urls) == 1

    down = create_app(
        Settings(),
        svc,
        geometry=routing_from_settings(
            None, osrm_url="http://osrm:5000", transport=FakeOsrm(fail=OSError("refused"))
        ),
    )
    call(down, "POST", "/ui-api/demo/start", DEMO)
    g = call(down, "GET", f"/ui-api/route-geometry?staff_id={sid}")[1]
    assert g["available"] is False and g["configured"] is True and "OSRM недоступен" in g["message"]


# ---------------------------------------------------------------- GraphHopper (D37)


class FakeGraphHopper:
    """Тестовый транспорт в формате ответа GraphHopper /route (points_encoded=false)."""

    def __init__(self, status: int = 200, body: dict | None = None, fail: Exception | None = None) -> None:
        self.status, self.body, self.fail, self.urls = status, body, fail, []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        from urllib.parse import parse_qs, urlsplit

        self.urls.append(url)
        if self.fail:
            raise self.fail
        if self.body is not None:
            return self.status, json.dumps(self.body).encode()
        q = parse_qs(urlsplit(url).query)
        pts = [tuple(map(float, p.split(","))) for p in q["point"]]  # lat,lon
        line = []
        for (la1, lo1), (la2, _lo2) in itertools.pairwise(pts):
            line += [[lo1, la1], [lo1, la2]]
        line.append([pts[-1][1], pts[-1][0]])
        snapped = [[lo + 0.001, la] for la, lo in pts]  # ~60 м по долготе на 56°
        body = {
            "paths": [
                {
                    "distance": 4321.0,
                    "time": 600000,
                    "points": {"type": "LineString", "coordinates": line},
                    "snapped_waypoints": {"type": "LineString", "coordinates": snapped},
                }
            ]
        }
        return 200, json.dumps(body).encode()


def test_graphhopper_client_request_and_parse() -> None:
    t = FakeGraphHopper()
    c = GraphHopperRouteClient("http://gh:8989/", transport=t)
    r = c.route([(56.8, 60.6), (56.9, 60.7)])
    url = t.urls[0]
    assert url.startswith("http://gh:8989/route?point=56.800000%2C60.600000&point=56.900000%2C60.700000")
    assert "profile=car" in url and "points_encoded=false" in url and "instructions=false" in url
    assert "key=" not in url
    assert r.coordinates == [(56.8, 60.6), (56.9, 60.6), (56.9, 60.7)]
    assert r.distance_m == 4321 and r.duration_s == 600
    assert 55 < r.max_snap_m < 70


def test_graphhopper_key_and_plain_array_points() -> None:
    body = {"paths": [{"distance": 10, "time": 2000, "points": [[60.6, 56.8, 250.0], [60.7, 56.9, 251.0]]}]}
    t = FakeGraphHopper(body=body)
    r = GraphHopperRouteClient("https://graphhopper.com/api/1", key="SECRET", transport=t).route(
        [(56.8, 60.6), (56.9, 60.7)]
    )
    assert "key=SECRET" in t.urls[0] and r.coordinates == [(56.8, 60.6), (56.9, 60.7)] and r.max_snap_m == 0


def test_graphhopper_errors_are_readable() -> None:
    import pytest

    bad = FakeGraphHopper(status=400, body={"message": "Cannot find point 0: 56.8,60.6", "hints": []})
    with pytest.raises(RoadGeometryError, match="HTTP 400: Cannot find point 0"):
        GraphHopperRouteClient("http://gh", transport=bad).route([(56.8, 60.6), (56.9, 60.7)])
    denied = FakeGraphHopper(status=401, body={})
    with pytest.raises(RoadGeometryError, match="ADO_GRAPHHOPPER_KEY"):
        GraphHopperRouteClient("http://gh", transport=denied).route([(1, 2), (3, 4)])
    g = routing_from_settings(
        "http://graphhopper:8989", transport=FakeGraphHopper(fail=OSError("Name does not resolve"))
    )
    with pytest.raises(RoadGeometryError) as e:
        g.route([(1, 2), (3, 4)])
    assert "GraphHopper недоступен" in str(e.value) and "--profile graphhopper" in str(e.value)


def test_routing_engine_choice() -> None:
    assert routing_from_settings(None).configured is False
    assert routing_from_settings(None, osrm_url="http://o").source == "OSRM http://o"
    both = routing_from_settings("http://gh:8989", osrm_url="http://o")
    assert both.source == "GraphHopper http://gh:8989"  # GraphHopper приоритетнее


def test_geometry_endpoint_graphhopper() -> None:
    svc = DispatchService(Settings(), CFG)
    fake = FakeGraphHopper()
    app = create_app(
        Settings(), svc, geometry=routing_from_settings("http://graphhopper:8989", transport=fake)
    )
    call(app, "POST", "/ui-api/demo/start", DEMO)
    st = call(app, "GET", "/ui-api/state")[1]
    sid = next(r["staff_id"] for r in st["plan"]["routes"] if r["totals"]["visits"])
    g = call(app, "GET", f"/ui-api/route-geometry?staff_id={sid}")[1]
    assert g["available"] is True and g["source"] == "GraphHopper http://graphhopper:8989"
    assert g["routes"][sid]["duration_min"] == 10.0 and len(fake.urls) == 1


def test_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ADO_GRAPHHOPPER_URL", "http://graphhopper:8989")
    monkeypatch.setenv("ADO_GRAPHHOPPER_KEY", "")
    s = Settings.from_env()
    assert (
        s.graphhopper_url == "http://graphhopper:8989"
        and s.graphhopper_key is None
        and s.graphhopper_profile == "car"
    )


# ---------------------------------------------------------------- история, сценарии, сотрудник


def test_demo_has_history_model_and_duration_forecast() -> None:
    app = create_app(Settings(), DispatchService(Settings(), CFG))
    call(app, "POST", "/ui-api/demo/start", DEMO)
    st = call(app, "GET", "/ui-api/state")[1]
    assert st["durations"]["orders"] > 1000 and "синтетическая" in st["durations"]["source"]
    fc = st["duration_forecast"]
    assert fc and all(v["min"] >= v["norm_min"] for v in fc.values())
    assert set(st["joint_orders"]) <= set(st["orders"])


def test_history_upload_and_errors() -> None:
    svc = DispatchService(Settings(), CFG)
    app = create_app(Settings(), svc)
    call(app, "POST", "/ui-api/demo/start", DEMO)
    assert call(app, "POST", "/ui-api/history", {"x": 1})[0] == 400
    assert call(app, "POST", "/ui-api/history", [{"work_type_code": "REPAIR", "fact": None}])[0] == 422
    rows = [
        {
            "work_type_code": "REPAIR",
            "fact": {"outcome": "COMPLETED", "staff_id": "ST-001", "actual_duration_min": 70},
        }
        for _ in range(12)
    ]
    status, body = call(app, "POST", "/ui-api/history", rows)
    assert status == 200 and body["orders"] == 12


def test_scenarios_endpoint() -> None:
    app = create_app(Settings(), DispatchService(Settings(), CFG))
    assert call(app, "POST", "/ui-api/scenario", {"kind": "fewer_staff"})[0] == 409
    call(app, "POST", "/ui-api/demo/start", DEMO)
    assert call(app, "POST", "/ui-api/scenario", {"kind": "nope"})[0] == 400
    st, r = call(
        app, "POST", "/ui-api/scenario", {"kind": "fewer_staff", "n": 3, "samples": 2, "time_limit_sec": 1}
    )
    assert st == 200 and len(r["runs"]) == 2 and all(len(x["absent"]) == 3 for x in r["runs"])
    assert r["scenario"]["success"] <= r["base"]["success"] + 2 and r["synthetic"] is True
    st, r = call(app, "POST", "/ui-api/scenario", {"kind": "backpack", "time_limit_sec": 1})
    assert st == 200 and set(r["recommended_start"]) == {"ONT", "ROUTER", "STB"}
    assert r["scenario"]["restocks"] <= r["base"]["restocks"]
    st, r = call(app, "POST", "/ui-api/scenario", {"kind": "traffic", "time_limit_sec": 1})
    assert st == 200 and r["scenario"]["road_h"] >= r["base"]["road_h"]
    assert call(app, "POST", "/ui-api/scenario", {"kind": "fewer_staff", "n": 999})[0] == 422


def test_tech_marks_and_events() -> None:
    svc = DispatchService(Settings(), CFG)
    app = create_app(Settings(), svc)
    call(app, "POST", "/ui-api/demo/start", DEMO)
    st = call(app, "GET", "/ui-api/state")[1]
    r = next(r for r in st["plan"]["routes"] if r["totals"]["visits"] >= 2)
    sid = r["staff_id"]
    oid = next(s["order_id"] for s in r["stops"] if s["order_id"])
    assert (
        call(app, "POST", "/ui-api/tech/mark", {"kind": "call", "staff_id": sid, "order_id": "X"})[0] == 422
    )
    status, m = call(app, "POST", "/ui-api/tech/mark", {"kind": "call", "staff_id": sid, "order_id": oid})
    assert status == 200 and "call" in m["marks"]
    assert call(app, "GET", "/ui-api/state")[1]["marks"][oid]["call"]
    # «завершил» — только после «прибыл»; время нажатия сохраняется; отметку можно снять
    assert (
        call(app, "POST", "/ui-api/tech/mark", {"kind": "done", "staff_id": sid, "order_id": oid})[0] == 422
    )
    call(app, "POST", "/ui-api/tech/mark", {"kind": "arrive", "staff_id": sid, "order_id": oid})
    status, m = call(app, "POST", "/ui-api/tech/mark", {"kind": "done", "staff_id": sid, "order_id": oid})
    assert status == 200 and {"arrive", "done", "done_wall"} <= set(m["marks"])
    m = call(
        app, "POST", "/ui-api/tech/mark", {"kind": "arrive", "staff_id": sid, "order_id": oid, "undo": True}
    )[1]
    assert "arrive" not in m["marks"] and "done" not in m["marks"] and "call" in m["marks"]
    # задержка по ещё не начатому визиту — отказ, поломка машины — событие API и новая версия плана
    assert (
        call(app, "POST", "/ui-api/tech/event", {"kind": "delay", "staff_id": sid, "order_id": oid})[0] == 422
    )
    v0 = st["plan_version"]
    status, res = call(app, "POST", "/ui-api/tech/event", {"kind": "unavailable", "staff_id": sid})
    assert status == 200 and res["plan_version"] == v0 + 1
    after = call(app, "GET", "/ui-api/state")[1]
    assert next(x for x in after["plan"]["routes"] if x["staff_id"] == sid)["status"] == "UNAVAILABLE"
    # в дне из API (не демо) события присылает оркестратор
    call(app, "POST", "/ui-api/demo/exit")
    app2 = create_app(Settings(), DispatchService(Settings(), CFG))
    assert call(app2, "POST", "/ui-api/tech/event", {"kind": "unavailable", "staff_id": sid})[0] == 409


def test_marks_delivered_without_new_plan_version() -> None:
    app = create_app(Settings(), DispatchService(Settings(), CFG))
    call(app, "POST", "/ui-api/demo/start", DEMO)
    st = call(app, "GET", "/ui-api/state")[1]
    r = next(r for r in st["plan"]["routes"] if r["totals"]["visits"])
    oid = next(s["order_id"] for s in r["stops"] if s["order_id"])
    call(app, "POST", "/ui-api/tech/mark", {"kind": "arrive", "staff_id": r["staff_id"], "order_id": oid})
    un = call(app, "GET", f"/ui-api/state?since_version={st['plan_version']}")[1]
    assert un["unchanged"] is True and "arrive" in un["marks"][oid]
