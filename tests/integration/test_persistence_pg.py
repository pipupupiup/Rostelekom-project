"""ADO-15 на настоящем PostgreSQL 16: версии, блокировки, конкурентные транзакции, откаты."""

from __future__ import annotations

import ast
import copy
import itertools
import threading
import time
from pathlib import Path

import pytest

from ado.api.service import DispatchService
from ado.config import Settings
from ado.events.session import ReoptConfig
from ado.optimization.improvement import ImproveConfig
from ado.orchestrator import ScenarioPlayer, service_client, synthetic_scenario
from ado.persistence.db import IntegrityViolation, PgWireDatabase
from ado.persistence.pgwire import PgWireConnection
from ado.persistence.store import DuplicateEvent, PlanStore, VersionConflict
from ado.synthetic import generate_day
from tests.integration import pg

pytestmark = pytest.mark.skipif(not pg.available(), reason="нужен локальный PostgreSQL и root для initdb")
_n = itertools.count()


@pytest.fixture(scope="module")
def server():
    srv = pg.LocalPostgres()
    yield srv
    srv.stop()


@pytest.fixture
def store(server) -> PlanStore:
    name = f"t{next(_n)}"
    c = PgWireConnection(port=server.port)
    c.query(f"CREATE DATABASE {name}")
    c.close()
    s = PlanStore(PgWireDatabase(port=server.port, database=name))
    s.migrate()
    return s


@pytest.fixture(scope="module")
def plans():
    """Настоящие планы сервиса: v1 и несколько версий после событий (СИНТЕТИКА)."""
    syn = generate_day(n_orders=80, seed=4, n_nodes=30)
    svc = DispatchService(Settings(), ReoptConfig(improve=ImproveConfig(max_iterations=5, time_limit_sec=2)))
    req = service_client(svc, syn.day_input["date"])
    req("PUT", "/api/v1/reference", syn.reference)
    req("PUT", "/api/v1/reference/travel-matrix", syn.matrix)
    req("PUT", f"/api/v1/days/{syn.day_input['date']}", syn.day_input)
    _, v1 = req("POST", f"/api/v1/days/{syn.day_input['date']}/plan", {"time_limit_sec": 10})
    player = ScenarioPlayer(
        req,
        syn.day_input["date"],
        syn.day_input["now"],
        synthetic_scenario(syn.day_input, syn.late_orders, syn.reference, seed=4),
        v1,
    )
    out = [(None, v1)]
    for _ in range(4):
        ev, _res = player.step()
        out.append((ev, copy.deepcopy(player.plan)))
    return syn, out


def load_day(store: PlanStore, syn) -> str:
    store.save_reference(syn.reference)
    store.save_day(syn.day_input["date"], syn.day_input["now"], syn.day_input["orders"])
    return syn.day_input["date"]


def test_migrate_idempotent_and_tables_match_sqlalchemy_models(store) -> None:
    store.migrate()  # повторно — без ошибок
    with store.db.transaction() as tx:
        cols = tx.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'"
        )
    live: dict[str, set[str]] = {}
    for r in cols:
        live.setdefault(r["table_name"], set()).add(r["column_name"])
    src = Path(__file__).resolve().parents[2] / "src/ado/persistence/tables.py"
    declared: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Table":
            name = node.args[0].value
            declared[name] = {a.args[0].value for a in node.args[2:] if isinstance(a, ast.Call)}
    assert declared == live


def test_initial_save_current_stops_and_reasons(store, plans) -> None:
    syn, seq = plans
    d = load_day(store, syn)
    v1 = seq[0][1]
    res = store.save_version(d, v1, None, "initial")
    assert res.version == 1
    version, plan = store.current(d)
    assert version == 1 and plan["plan_version"] == 1
    stops = store.stops(d, 1)
    n = sum(len(r["stops"]) for r in v1["routes"])
    assert len(stops) == n
    for r in v1["routes"]:  # порядок остановок сохранён
        got = [s["seq"] for s in stops if s["staff_id"] == r["staff_id"]]
        assert got == [s["seq"] for s in r["stops"]]
    assert {u["order_id"]: u["reason_code"] for u in store.unassigned(d, 1)} == {
        u["order_id"]: u["reason_code"] for u in v1["unassigned"]
    }


def test_next_versions_events_and_audit(store, plans) -> None:
    syn, seq = plans
    d = load_day(store, syn)
    store.save_version(d, seq[0][1], None, "initial")
    for i, (ev, plan) in enumerate(seq[1:], start=1):
        store.save_version(d, plan, i, ev["event_id"], ev)
    assert [v["version"] for v in store.versions(d)] == [1, 2, 3, 4, 5]
    assert store.current(d)[0] == 5
    assert store.has_event(d, seq[1][0]["event_id"])
    assert store.audit(d)  # были изменения маршрутов (как минимум первичное назначение v1)


def test_stale_version_rejected(store, plans) -> None:
    syn, seq = plans
    d = load_day(store, syn)
    store.save_version(d, seq[0][1], None, "initial")
    store.save_version(d, seq[1][1], 1, "e1", seq[1][0])
    with pytest.raises(VersionConflict):
        store.save_version(d, seq[1][1], 1, "e1-again")  # устаревшее изменение (ожидали 1, уже 2)
    with pytest.raises(VersionConflict):
        store.save_version(d, seq[3][1], 2, "skip")  # версия 4 при текущей 2
    assert store.current(d)[0] == 2


def test_duplicate_event_rolls_back_whole_version(store, plans) -> None:
    syn, seq = plans
    d = load_day(store, syn)
    store.save_version(d, seq[0][1], None, "initial")
    store.save_version(d, seq[1][1], 1, "e1", seq[1][0])
    with pytest.raises(DuplicateEvent):
        store.save_version(d, seq[2][1], 2, "dup", seq[1][0])  # то же event_id
    assert [v["version"] for v in store.versions(d)] == [1, 2]


def test_error_inside_transaction_rolls_back(store, plans) -> None:
    syn, seq = plans
    d = load_day(store, syn)
    store.save_version(d, seq[0][1], None, "initial")
    bad = copy.deepcopy(seq[1][1])
    r = next(r for r in bad["routes"] if len(r["stops"]) > 2)
    r["stops"][1]["end"] = "2026-10-26T05:00:00+05:00"  # end < start → CHECK
    with pytest.raises(IntegrityViolation):
        store.save_version(d, bad, 1, "bad", seq[1][0])
    assert store.current(d)[0] == 1 and not store.has_event(d, seq[1][0]["event_id"])


def test_conflicting_assignment_forbidden(store, plans) -> None:
    syn, seq = plans
    d = load_day(store, syn)
    bad = copy.deepcopy(seq[0][1])
    routes = [r for r in bad["routes"] if any(s["type"] == "VISIT" for s in r["stops"])]
    visit = next(s for s in routes[0]["stops"] if s["type"] == "VISIT")
    dup = dict(visit, seq=len(routes[1]["stops"]) + 1)
    routes[1]["stops"].append(dup)  # один наряд у двух сотрудников
    with pytest.raises(IntegrityViolation):
        store.save_version(d, bad, None, "initial")
    assert store.current(d) is None


def test_concurrent_writers_only_one_wins(store, plans) -> None:
    syn, seq = plans
    d = load_day(store, syn)
    store.save_version(d, seq[0][1], None, "initial")
    results: list[str] = []
    barrier = threading.Barrier(2)

    def writer(i: int) -> None:
        barrier.wait()
        try:
            store.save_version(d, seq[1][1], 1, f"w{i}")
            results.append("ok")
        except VersionConflict:
            results.append("conflict")

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert sorted(results) == ["conflict", "ok"]
    assert store.current(d)[0] == 2


def test_writer_blocks_on_locked_row_then_sees_new_version(store, plans) -> None:
    syn, seq = plans
    d = load_day(store, syn)
    store.save_version(d, seq[0][1], None, "initial")
    holder_ready, release = threading.Event(), threading.Event()

    def holder() -> None:
        with store.db.transaction() as tx:  # первая транзакция держит блокировку и сдвигает версию
            tx.execute("SELECT version FROM plan_current WHERE date = CAST(:d AS date) FOR UPDATE", {"d": d})
            holder_ready.set()
            release.wait(10)
            tx.execute("UPDATE plan_current SET version = 2 WHERE date = CAST(:d AS date)", {"d": d})

    out: list[str] = []

    def writer() -> None:
        try:
            store.save_version(d, seq[1][1], 1, "late")
            out.append("ok")
        except VersionConflict:
            out.append("conflict")

    h = threading.Thread(target=holder)
    h.start()
    holder_ready.wait(10)
    w = threading.Thread(target=writer)
    w.start()
    time.sleep(0.5)
    assert not out  # ждёт снятия блокировки
    release.set()
    h.join(10)
    w.join(10)
    assert out == ["conflict"]  # устаревшая запись не прошла


def test_replan_before_events_replaces_v1_and_day_reload_resets(store, plans) -> None:
    syn, seq = plans
    d = load_day(store, syn)
    store.save_version(d, seq[0][1], None, "initial")
    store.save_version(d, seq[0][1], None, "initial-again")
    assert [v["reason"] for v in store.versions(d)] == ["initial-again"]
    store.save_version(d, seq[1][1], 1, "e1", seq[1][0])
    with pytest.raises(VersionConflict):
        store.save_version(d, seq[0][1], None, "initial")  # после событий — нельзя
    store.save_day(d, syn.day_input["now"], syn.day_input["orders"])  # повторная загрузка дня
    assert (
        store.current(d) is None and store.versions(d) == [] and not store.has_event(d, seq[1][0]["event_id"])
    )


def _run_service(store: PlanStore | None, n_events: int = 6):
    syn = generate_day(n_orders=60, seed=6, n_nodes=25)
    svc = DispatchService(
        Settings(), ReoptConfig(improve=ImproveConfig(max_iterations=5, time_limit_sec=2)), store=store
    )
    req = service_client(svc, syn.day_input["date"])
    req("PUT", "/api/v1/reference", syn.reference)
    req("PUT", "/api/v1/reference/travel-matrix", syn.matrix)
    req("PUT", f"/api/v1/days/{syn.day_input['date']}", syn.day_input)
    status, v1 = req("POST", f"/api/v1/days/{syn.day_input['date']}/plan", {"time_limit_sec": 10})
    assert status == 200
    player = ScenarioPlayer(
        req,
        syn.day_input["date"],
        syn.day_input["now"],
        synthetic_scenario(syn.day_input, syn.late_orders, syn.reference, seed=6),
        v1,
    )
    for _ in range(n_events):
        player.step()
    return syn, svc


def test_service_persists_every_published_version(store) -> None:
    syn, svc = _run_service(store)
    d = syn.day_input["date"]
    assert svc.db_errors == []
    assert [v["version"] for v in store.versions(d)] == [p.plan_version for p in svc.session.history]
    version, plan = store.current(d)
    assert version == svc.session.version and plan["based_on_event_id"] == svc.session.plan.based_on_event_id


def test_service_keeps_working_when_db_down() -> None:
    dead = PlanStore(PgWireDatabase(port=1))  # никто не слушает
    _syn, svc = _run_service(dead, n_events=2)
    assert svc.session.version == 3 and svc.db_errors  # API отработал, ошибки БД зафиксированы
