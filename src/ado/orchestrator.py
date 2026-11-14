"""Эмулятор оркестратора организаторов (ADO-14 / ADO-17) — сценарий оценки одного дня.

Повторяет п. 1 спецификации: `PUT /reference` → `PUT /reference/travel-matrix` → `PUT /days`
→ `POST /plan` → для каждого события по возрастанию `timestamp`: сработавшие к этому моменту
триггеры, затем событие, затем `GET /plan` → итоговый `GET /plan`. Триггеры разворачиваются по
**текущему плану команды** (`timestamp = VISIT.start + fire_after_start_min`, исполнитель — из
плана), как в `EventScenarioFile`; если наряда нет в маршруте — триггер пропускается.

Работает поверх любого клиента с интерфейсом `request(method, path, body) → (status, json)`:
`DispatchService` напрямую или HTTP. Замеряет время каждого вызова и проверяет каждую версию
плана независимым валидатором.

Также содержит СИНТЕТИЧЕСКИЙ генератор сценария событий для `ado.synthetic` (не данные кейса).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ado.api.service import DispatchService

Request = Callable[[str, str, Any], tuple[int, Any]]


def service_client(service: DispatchService, day: str) -> Request:
    def request(method: str, path: str, body: Any = None) -> tuple[int, Any]:
        p = path.removeprefix("/api/v1")
        if method == "GET" and p == "/health":
            return service.health()
        if method == "PUT" and p == "/reference":
            return service.put_reference(body)
        if method == "PUT" and p == "/reference/travel-matrix":
            return service.put_travel_matrix(body)
        if method == "PUT" and p == f"/days/{day}":
            return service.put_day(day, body)
        if method == "POST" and p == f"/days/{day}/plan":
            return service.post_plan(day, body)
        if method == "GET" and p == f"/days/{day}/plan":
            return service.get_plan(day)
        if method == "POST" and p == f"/days/{day}/events":
            return service.post_event(day, body)
        raise ValueError(f"{method} {path}")

    return request


@dataclass(slots=True)
class CallLog:
    name: str
    status: int
    seconds: float
    plan_version: int | None = None


@dataclass(slots=True)
class RunResult:
    calls: list[CallLog] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    events_sent: list[dict[str, Any]] = field(default_factory=list)
    skipped_triggers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def final_plan(self) -> dict[str, Any]:
        return self.plans[-1]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _find_visit(plan: dict[str, Any], order_id: str) -> tuple[str, dict[str, Any]] | None:
    for r in plan["routes"]:
        for s in r["stops"]:
            if s["type"] == "VISIT" and s["order_id"] == order_id:
                return r["staff_id"], s
    return None


class ScenarioPlayer:
    """Пошаговое воспроизведение сценария дня (для `run_day` и демо в интерфейсе).

    Каждый `step()` отправляет ровно одно событие — ближайшее по времени среди событий сценария
    и триггеров, развёрнутых по **текущему** плану (триггер с моментом ≤ события идёт раньше).
    """

    def __init__(
        self,
        request: Request,
        day: str,
        now: str,
        scenario: dict[str, Any],
        plan: dict[str, Any],
        log: Callable[[str, str, str, Any], Any] | None = None,
    ) -> None:
        self.request = request
        self.day = day
        self.plan = plan
        self.pending = list(scenario.get("triggers", []))
        self.events = sorted(scenario.get("events", []), key=lambda e: e["timestamp"])
        self.last_ts = _ts(now)
        self.sent: list[dict[str, Any]] = []
        self.skipped: list[str] = []
        self._log = log

    def _call(self, name: str, method: str, path: str, body: Any = None) -> Any:
        if self._log is not None:
            return self._log(name, method, path, body)
        return self.request(method, path, body)[1]

    def _due_trigger(self) -> tuple[datetime, dict[str, Any], str, dict[str, Any]] | None:
        due = []
        for tr in self.pending:
            found = _find_visit(self.plan, tr["order_id"])
            if found is None:
                continue
            sid, stop = found
            ts = _ts(stop["start"]) + timedelta(minutes=tr["fire_after_start_min"])
            due.append((max(ts, self.last_ts), tr, sid, stop))
        if not due:
            return None
        due.sort(key=lambda x: (x[0], x[1]["trigger_id"]))
        return due[0]

    @property
    def remaining(self) -> int:
        return len(self.events) + len(self.pending)

    def next_event(self) -> dict[str, Any] | None:
        trig = self._due_trigger()
        if self.events and (trig is None or trig[0] > _ts(self.events[0]["timestamp"])):
            return self.events.pop(0)
        if trig is None:
            self.skipped += [t["trigger_id"] for t in self.pending]  # наряды не в маршрутах — пропуск
            self.pending.clear()
            return None
        ts, tr, sid, stop = trig
        self.pending.remove(tr)
        ev: dict[str, Any] = {
            "event_id": tr["trigger_id"].replace("TR", "EV-TR"),
            "type": tr["type"],
            "timestamp": ts.isoformat(timespec="seconds"),
            "staff_id": sid,
            "order_id": tr["order_id"],
        }
        if tr["type"] == "VISIT_DELAYED":
            end = _ts(stop["end"]) + timedelta(minutes=tr["extra_duration_min"])
            ev["expected_end"] = end.isoformat(timespec="seconds")
        else:
            ev["actual_end"] = ts.isoformat(timespec="seconds")
            ev["transferred_to"] = tr.get("transferred_to") or "STATION_EQUIPMENT"
        return ev

    def step(self) -> tuple[dict[str, Any], Any] | None:
        ev = self.next_event()
        if ev is None:
            return None
        res = self._call(f"event {ev['type']}", "POST", f"/api/v1/days/{self.day}/events", ev)
        self.sent.append(ev)
        self.last_ts = max(self.last_ts, _ts(ev["timestamp"]))
        self.plan = self._call("get plan", "GET", f"/api/v1/days/{self.day}/plan")
        return ev, res


def run_day(
    request: Request,
    reference: dict[str, Any],
    matrix: dict[str, Any],
    day_input: dict[str, Any],
    scenario: dict[str, Any],
    time_limit_sec: int = 300,
) -> RunResult:
    day = day_input["date"]
    out = RunResult()

    def do(name: str, method: str, path: str, body: Any = None) -> Any:
        t0 = time.perf_counter()
        status, res = request(method, path, body)
        dt = time.perf_counter() - t0
        ver = res.get("plan_version") if isinstance(res, dict) else None
        out.calls.append(CallLog(name, status, dt, ver))
        if status != 200:
            out.errors.append(f"{name}: {status} {res}")
        if name == "get plan":
            out.plans.append(res)
        return res

    do("health", "GET", "/api/v1/health")
    do("reference", "PUT", "/api/v1/reference", reference)
    do("matrix", "PUT", "/api/v1/reference/travel-matrix", matrix)
    do("day", "PUT", f"/api/v1/days/{day}", day_input)
    plan = do("plan", "POST", f"/api/v1/days/{day}/plan", {"time_limit_sec": time_limit_sec})
    out.plans.append(plan)
    player = ScenarioPlayer(request, day, day_input["now"], scenario, plan, log=do)
    while player.step() is not None:
        pass
    out.events_sent = player.sent
    out.skipped_triggers = player.skipped
    out.plans.append(do("final plan", "GET", f"/api/v1/days/{day}/plan"))
    return out


def synthetic_scenario(
    day_input: dict[str, Any],
    late_orders: list[dict[str, Any]],
    reference: dict[str, Any],
    seed: int = 1,
    cancellations: int = 8,
    delays: int = 10,
    transfers: int = 4,
    unavailable: int = 2,
) -> dict[str, Any]:
    """СИНТЕТИЧЕСКИЙ сценарий событий дня (по структуре `EventScenarioFile`)."""
    rnd = random.Random(seed)
    date = day_input["date"]
    events: list[dict[str, Any]] = []
    n = 0

    def eid() -> str:
        nonlocal n
        n += 1
        return f"EV-{date.replace('-', '')}-{n:04d}"

    for o in late_orders:
        events.append({"event_id": eid(), "type": "NEW_ORDER", "timestamp": o["registered_at"], "order": o})
    early = sorted(day_input["orders"], key=lambda o: o["order_id"])
    for o in rnd.sample(early, min(cancellations, len(early))):
        ts = _ts(o["slot"]["start"]) - timedelta(minutes=45)
        if ts > _ts(day_input["now"]):
            events.append(
                {
                    "event_id": eid(),
                    "type": "ORDER_CANCELLED",
                    "timestamp": ts.isoformat(timespec="seconds"),
                    "order_id": o["order_id"],
                    "reason": "NOT_CONFIRMED",
                }
            )
    staff = sorted(s["staff_id"] for s in reference["staff"] if "LTP3" in s["profiles"])
    for k, sid in enumerate(rnd.sample(staff, unavailable)):
        ts = datetime.fromisoformat(f"{date}T11:10:00+05:00") + timedelta(minutes=50 * k)
        ev = {
            "event_id": eid(),
            "type": "STAFF_UNAVAILABLE",
            "timestamp": ts.isoformat(timespec="seconds"),
            "staff_id": sid,
            "reason": "VEHICLE_BREAKDOWN" if k == 0 else "ILLNESS",
        }
        if k % 2 == 1:
            ev["until"] = (ts + timedelta(hours=2)).isoformat(timespec="seconds")
        events.append(ev)
    t = _ts(day_input["now"])
    while t.hour < 20:
        t += timedelta(minutes=30)
        events.append({"event_id": eid(), "type": "TIME_TICK", "timestamp": t.isoformat(timespec="seconds")})
    events.sort(key=lambda e: (e["timestamp"], e["event_id"]))
    pool = sorted(
        o["order_id"] for o in early if o["work_type_code"] in ("REPAIR", "DEGRADATION", "INST_PON_MKD")
    )
    chosen = rnd.sample(pool, min(delays + transfers, len(pool)))
    triggers = []
    for i, oid in enumerate(chosen[:delays]):
        triggers.append(
            {
                "trigger_id": f"TR-{date.replace('-', '')}-{i + 1:02d}",
                "type": "VISIT_DELAYED",
                "order_id": oid,
                "fire_after_start_min": 30,
                "extra_duration_min": rnd.choice([20, 40, 60]),
            }
        )
    for i, oid in enumerate(chosen[delays:]):
        triggers.append(
            {
                "trigger_id": f"TR-{date.replace('-', '')}-{delays + i + 1:02d}",
                "type": "ORDER_TRANSFERRED",
                "order_id": oid,
                "fire_after_start_min": 15,
                "transferred_to": "TRUNK_CABLE",
            }
        )
    return {"date": date, "now": day_input["now"], "events": events, "triggers": triggers}


def http_client(base_url: str, timeout: float = 400.0) -> Request:
    """Клиент оркестратора поверх настоящего HTTP (stdlib), для репетиции развёртывания."""
    import json as _json
    import urllib.error
    import urllib.request

    def request(method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else _json.dumps(body).encode()
        req = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read() or b"null")

    return request
