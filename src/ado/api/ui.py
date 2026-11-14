"""Служебные эндпоинты интерфейса диспетчера (ADO-18) — вне путей спецификации (п. 7 допускает).

* `GET  /ui-api/state` — всё, что нужно экрану диспетчера: справочники (СЦ, сотрудники), наряды
  дня с координатами и слотами, текущий план (тот же JSON, что `GET /plan`), риски опоздания
  (ADO-12), звонки к абонентам, метрики (ADO-11), версии плана. Интерфейс ничего не пересчитывает.
* `POST /ui-api/demo/start` — СИНТЕТИЧЕСКИЙ демо-день: генерация данных, загрузка через те же
  операции API, построение плана. `POST /ui-api/demo/step` — следующее событие сценария
  (с триггерами по текущему плану, как у оркестратора). В ответе и в состоянии — пометка `synthetic`.
  Демо работает в **отдельном** экземпляре сервиса в памяти (D34): не трогает день, загруженный
  через API, и не пишет в БД. `POST /ui-api/demo/exit` — выйти из демо: экран снова показывает
  день из API (или стартовый экран); ничего не удаляется.
* `GET /ui-api/route-geometry?staff_id=A,B` — линии маршрутов по дорогам (GraphHopper или OSRM,
  D35/D37); если движок не настроен или недоступен — `available: false` и причина.
* `/ui/` — собранный фронтенд (`frontend/dist`).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ado.api.serialize import plan_json
from ado.api.service import DispatchService, error
from ado.data.prepare import timebase_of
from ado.domain.enums import StopType
from ado.optimization.durations import DurationModel
from ado.optimization.evaluation import evaluate_plan
from ado.optimization.risk import call_due, predict_delays
from ado.optimization.validator import validate_plan
from ado.orchestrator import ScenarioPlayer, service_client, synthetic_scenario
from ado.routing.geometry import RoadGeometryError, RouteGeometryService, routing_from_settings
from ado.scenarios import KINDS, ScenarioInput, run_scenario
from ado.synthetic import generate_day, generate_history


@dataclass(slots=True)
class DemoState:
    service: DispatchService | None = None
    """Отдельный сервис демо-дня (только память, без БД)."""
    player: ScenarioPlayer | None = None
    seed: int = 0
    last_event: dict[str, Any] | None = None
    last_result: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class UiApi:
    def __init__(self, service: DispatchService, geometry: RouteGeometryService | None = None) -> None:
        self.main = service
        self.demo = DemoState()
        st = service.settings
        self.geometry = geometry or routing_from_settings(
            st.graphhopper_url, st.graphhopper_key, st.graphhopper_profile, st.osrm_url
        )
        self._cache: tuple[tuple[int, str, int], dict[str, Any]] | None = None
        self.marks: dict[tuple[int, str], dict[str, str]] = {}
        self._tech_seq = 0

    @property
    def service(self) -> DispatchService:
        """Что показывает экран: демо, пока оно идёт, иначе день из API."""
        d = self.demo.service
        return d if d is not None and self.demo.player is not None else self.main

    # ------------------------------------------------------------ состояние
    def state(self, since_version: int | None = None) -> tuple[int, dict[str, Any]]:
        svc = self.service
        # во время расчёта плана (до 300 с) не ждём блокировку — отвечаем «идёт расчёт»
        if not svc.lock.acquire(timeout=0.5):
            return 200, {
                "loaded": True,
                "busy": True,
                "unchanged": since_version is not None,
                "plan_version": since_version,
            }
        try:
            return self._state_locked(since_version)
        finally:
            svc.lock.release()

    def _state_locked(self, since_version: int | None) -> tuple[int, dict[str, Any]]:
        svc = self.service
        if True:
            s = svc.session
            if s is None or svc.reference is None:
                return 200, {"loaded": False, "synthetic": False, "message": "День не загружен"}
            key = (id(s), s.day.date, s.version)  # новый день на ту же дату — новая сессия
            if (
                since_version is not None
                and since_version == s.version
                and self._cache is not None
                and self._cache[0] == key
                and s.plan is not None
            ):
                return 200, {
                    "loaded": True,
                    "unchanged": True,
                    "plan_version": s.version,
                    # отметки сотрудников меняются без новой версии плана — отдаём их всегда
                    "marks": {oid: m for (k, oid), m in self.marks.items() if k == id(s)},
                }
            if self._cache is not None and self._cache[0] == key:
                body = dict(self._cache[1])
            else:
                body = self._build_state(s)
                self._cache = (key, body)
        body["marks"] = {oid: m for (k, oid), m in self.marks.items() if k == id(s)}
        body["persistence"] = {
            "enabled": svc.store is not None,
            "saved_version": svc.db_saved_version,
            "errors": len(svc.db_errors),
            "last_error": svc.db_errors[-1] if svc.db_errors else None,
        }
        p = self.demo.player
        body["demo"] = {
            "active": p is not None,
            "remaining": p.remaining if p else 0,
            "sent": len(p.sent) if p else 0,
            "last_event": self.demo.last_event,
            "last_message": self.demo.last_result.get("message")
            if isinstance(self.demo.last_result, dict)
            else None,
        }
        return 200, body

    def _build_state(self, s) -> dict[str, Any]:
        ref, day, tb = self.service.reference, s.day, timebase_of(s.day)
        out: dict[str, Any] = {
            "loaded": True,
            "synthetic": self.service is not self.main,
            "date": day.date,
            "now": tb.format(s.now),
            "plan_version": s.version,
            "service_centers": [
                {"service_center_id": sc.service_center_id, "name": sc.name, "lat": sc.lat, "lon": sc.lon}
                for sc in ref.service_centers.values()
            ],
            "staff": [
                {
                    "staff_id": e.staff_id,
                    "display_name": e.display_name or e.staff_id,
                    "service_center_id": e.service_center_id,
                    "profiles": sorted(p.value for p in e.profiles),
                    "technologies": sorted(t.value for t in e.technologies),
                    "height_permit": e.height_permit,
                    "shift_start": tb.format(e.shift.start),
                    "shift_end": tb.format(e.shift.end),
                    "break_window": {
                        "start": tb.format(e.shift.break_window.start),
                        "end": tb.format(e.shift.break_window.end),
                    },
                    "break_window_planned": {
                        "start": tb.format(day.reference.employees[e.staff_id].shift.break_window.start),
                        "end": tb.format(day.reference.employees[e.staff_id].shift.break_window.end),
                    },
                    "break_duration_min": e.shift.break_duration // 60,
                    "absent": e.staff_id in day.absent_staff_ids,
                }
                for e in sorted(ref.employees.values(), key=lambda e: e.staff_id)
            ],
            "orders": {
                o.order_id: {
                    "order_id": o.order_id,
                    "work_type_code": o.work_type_code.value,
                    "work_type_name": ref.work_types[o.work_type_code].name,
                    "duration_min": ref.work_types[o.work_type_code].duration // 60,
                    "technology": o.technology.value,
                    "building_id": o.building_id,
                    "address": o.address,
                    "lat": o.lat,
                    "lon": o.lon,
                    "access_node_id": o.access_node_id,
                    "is_repeat": o.is_repeat,
                    "requires_height": o.requires_height,
                    "registered_at": tb.format(o.registered_at),
                    "slot": {"start": tb.format(o.slot.start), "end": tb.format(o.slot.end)},
                    "latest_start": tb.format(o.latest_start),
                    "deadline": tb.format(o.deadline),
                    "required_equipment": o.required_equipment.to_api(),
                }
                for o in day.orders.values()
            },
            "plan": None,
            "risks": [],
            "calls_due": [],
            "metrics": None,
            "versions": [],
        }
        out["breaks"] = s.break_plan.to_json() if s.break_plan is not None else None
        out["traffic"] = (
            {str(h): f for h, f in sorted(s.travel.matrix.hourly_factors.items())}
            if hasattr(s.travel, "factor_at")
            else None
        )
        out["joint_orders"] = sorted(  # D41: по регламенту — два сотрудника; API кейса этого не выражает
            oid for oid, o in day.orders.items() if o.work_type_code.value == "INST_PRIVATE_VLS"
        )
        if s.plan is None:
            return out
        plan = s.plan
        out["plan"] = plan_json(plan, day, tb)
        out["risks"] = [
            {
                "order_id": r.order_id,
                "staff_id": r.staff_id,
                "level": r.level.value,
                "forecast_start": tb.format(r.forecast_start),
                "latest_start": tb.format(r.latest_start),
                "slack_min": r.slack // 60,
                "lateness_min": r.lateness_min,
                "notice_possible": r.notice_possible,
                "reason": r.reason,
            }
            for r in predict_delays(
                plan, day, s.travel, s.now, end_overrides=s.end_overrides, durations=self._duration_fn(s)
            )
        ]
        dm: DurationModel | None = self.service.durations
        out["durations"] = dm.summary() if dm is not None else None
        out["duration_forecast"] = {}
        if dm is not None:
            for sid, r in plan.routes.items():
                for st in r.stops:
                    if st.type is StopType.VISIT and st.order_id in day.orders:
                        o = day.orders[st.order_id]
                        norm = day.reference.work_types[o.work_type_code].duration
                        out["duration_forecast"][st.order_id] = {
                            **dm.predict(o.work_type_code.value, sid, norm).to_json(),
                            "staff_id": sid,
                        }
        out["calls_due"] = call_due(plan, day, s.now)
        issues = validate_plan(plan, day, s.travel, end_overrides=s.end_overrides, not_before=s.not_before)
        m = evaluate_plan(plan, day, s.travel, issues=issues)
        out["metrics"] = {
            "success": m.success_count,
            "orders_total": m.orders_total,
            "weighted_success": m.weighted_success,
            "weighted_total": m.weighted_total,
            "weighted_share": round(m.weighted_success_share, 4),
            "visits_planned": m.visits_planned,
            "unassigned": m.unassigned_count,
            "pgp_orders": m.pgp_orders,
            "cancelled": m.cancelled,
            "travel_sec_total": m.travel_sec_total,
            "utilization_cv": round(m.utilization_cv, 4),
            "hard_violations": m.hard_violations,
            "violations": [str(i) for i in issues[:20]],
        }
        out["versions"] = [
            {
                "plan_version": p.plan_version,
                "based_on_event_id": p.based_on_event_id,
                "as_of": tb.format(p.as_of),
                "computation_ms": p.computation_ms,
            }
            for p in s.history
        ]
        return out

    # ------------------------------------------------------------ демо (СИНТЕТИКА)
    def demo_start(self, body: Any) -> tuple[int, dict[str, Any]]:
        body = body if isinstance(body, dict) else {}
        try:
            seed = int(body.get("seed", 7))
            n = max(20, min(int(body.get("n_orders", 400)), 600))
            limit = max(5, min(int(body.get("time_limit_sec", 30)), 300))
        except (TypeError, ValueError):
            return error(400, "BAD_REQUEST", "seed, n_orders, time_limit_sec — целые числа")
        with self.demo.lock:
            syn = generate_day(n_orders=n, seed=seed)
            scenario = synthetic_scenario(syn.day_input, syn.late_orders, syn.reference, seed=seed)
            demo_svc = DispatchService(settings=self.main.settings, config=self.main.config)  # без БД
            demo_svc.durations = DurationModel.fit(
                generate_history(syn.reference, seed=seed), label="синтетическая история (демо)"
            )
            req = service_client(demo_svc, syn.day_input["date"])
            for method, path, payload in (
                ("PUT", "/api/v1/reference", syn.reference),
                ("PUT", "/api/v1/reference/travel-matrix", syn.matrix),
                ("PUT", f"/api/v1/days/{syn.day_input['date']}", syn.day_input),
            ):
                status, res = req(method, path, payload)
                if status != 200:
                    return status, res
            status, plan = req(
                "POST", f"/api/v1/days/{syn.day_input['date']}/plan", {"time_limit_sec": limit}
            )
            if status != 200:
                return status, plan
            self.demo.service = demo_svc
            self.demo.player = ScenarioPlayer(
                req, syn.day_input["date"], syn.day_input["now"], scenario, plan
            )
            self.demo.seed, self.demo.last_event, self.demo.last_result = seed, None, None
            self._cache = None
        return 200, {
            "synthetic": True,
            "date": syn.day_input["date"],
            "plan_version": plan["plan_version"],
            "events": self.demo.player.remaining,
        }

    def demo_step(self, body: Any) -> tuple[int, dict[str, Any]]:
        body = body if isinstance(body, dict) else {}
        count = max(1, min(int(body.get("count", 1) or 1), 200))
        with self.demo.lock:
            p = self.demo.player
            if p is None:
                return error(409, "DEMO_NOT_STARTED", "Демо не запущено (POST /ui-api/demo/start)")
            sent = []
            for _ in range(count):
                r = p.step()
                if r is None:
                    break
                ev, res = r
                self.demo.last_event = {k: v for k, v in ev.items() if k != "order"}
                self.demo.last_result = res
                sent.append(ev["event_id"])
        return 200, {
            "sent": sent,
            "remaining": p.remaining,
            "last_event": self.demo.last_event,
            "result": self.demo.last_result,
        }

    def demo_exit(self, body: Any = None) -> tuple[int, dict[str, Any]]:
        """Выход из демо: показываем снова день из API. Ничего не удаляется: демо в БД не писалось,
        день из API и его история (память и PostgreSQL) не затрагивались."""
        with self.demo.lock:
            was = self.demo.player is not None
            self.demo.service = None
            self.demo.player = None
            self.demo.last_event = self.demo.last_result = None
            self._cache = None
        main = self.main.session
        return 200, {
            "exited": was,
            "showing": "api_day" if main is not None and self.main.reference is not None else "start",
            "date": main.day.date if main is not None else None,
        }

    # ------------------------------------------------------------ дорожная геометрия
    def route_geometry(self, staff_ids: list[str]) -> tuple[int, dict[str, Any]]:
        svc = self.service
        with svc.lock:  # только собрать точки; запросы к OSRM — вне блокировки
            s = svc.session
            if s is None or s.plan is None or svc.reference is None:
                return error(409, "PLAN_NOT_BUILT", "План ещё не построен")
            ref, version = svc.reference, s.version
            where: dict[str, tuple[float, float]] = {
                sc.service_center_id: (sc.lat, sc.lon) for sc in ref.service_centers.values()
            }
            for o in s.day.orders.values():
                where[o.building_id] = (o.lat, o.lon)
            wanted = staff_ids or sorted(s.plan.routes)
            points: dict[str, list[tuple[float, float]]] = {}
            for sid in wanted:
                r = s.plan.routes.get(sid)
                if r is None:
                    continue
                seq: list[tuple[float, float]] = []
                last = None
                for st in r.stops:
                    if st.location_id == last or st.location_id not in where:
                        continue
                    last = st.location_id
                    seq.append(where[st.location_id])
                if len(seq) >= 2:
                    points[sid] = seq
        out: dict[str, Any] = {
            "plan_version": version,
            "configured": self.geometry.configured,
            "source": self.geometry.source,
            "routes": {},
            "errors": {},
        }
        for sid, seq in points.items():
            try:
                out["routes"][sid] = self.geometry.route(seq).to_json()
            except RoadGeometryError as e:
                out["errors"][sid] = str(e)
        out["available"] = bool(out["routes"]) or (not out["errors"] and self.geometry.configured)
        out["message"] = (
            None
            if not out["errors"]
            else next(iter(out["errors"].values()))
            if not out["routes"]
            else f"для {len(out['errors'])} маршрутов геометрия не получена"
        )
        return 200, out

    # ------------------------------------------------------------ история, сценарии, сотрудник
    def _duration_fn(self, s):
        dm: DurationModel | None = self.service.durations
        if dm is None:
            return None
        day = s.day

        def fn(oid: str, sid: str) -> int:
            o = day.orders[oid]
            return dm.predict(
                o.work_type_code.value, sid, day.reference.work_types[o.work_type_code].duration
            ).seconds

        return fn

    def history(self, body: Any) -> tuple[int, dict[str, Any]]:
        """Загрузить историю нарядов (`orders_train.json`: массив нарядов с полем `fact`)."""
        rows = body.get("orders") if isinstance(body, dict) else body
        if not isinstance(rows, list):
            return error(
                400, "BAD_REQUEST", "Ожидается массив нарядов (orders_train.json) или {orders: [...]}"
            )
        label = (
            body.get("label", "загружено через интерфейс")
            if isinstance(body, dict)
            else "загружено через интерфейс"
        )
        model = DurationModel.fit(rows, label=str(label))
        if model.size == 0:
            return error(
                422, "NO_HISTORY", "В наборе нет нарядов с fact.outcome = COMPLETED и actual_duration_min"
            )
        svc = self.service
        with svc.lock:
            svc.durations = model
            self._cache = None
        return 200, model.summary()

    def scenario(self, body: Any) -> tuple[int, dict[str, Any]]:
        body = body if isinstance(body, dict) else {}
        kind = body.get("kind")
        if kind not in KINDS:
            return error(400, "BAD_REQUEST", f"kind: одно из {', '.join(KINDS)}")
        svc = self.service
        with svc.lock:  # снимок входных данных; расчёт — вне блокировки
            s = svc.session
            if s is None or svc.matrix is None:
                return error(409, "DAY_NOT_LOADED", "День не загружен")
            inp = ScenarioInput(s.day, svc.matrix)
        try:
            res = run_scenario(inp, kind, body, limit=float(body.get("time_limit_sec", 3)))
        except ValueError as e:
            return error(422, "BAD_SCENARIO", str(e))
        res["synthetic"] = svc is not self.main
        return 200, res

    def tech_mark(self, body: Any) -> tuple[int, dict[str, Any]]:
        """Отметка сотрудника (мобильная версия): позвонил / прибыл / завершил (`undo: true` — отменить).
        Хранится в памяти, видна диспетчеру. Мобильная версия по отметке «завершил» переходит к
        следующему адресу; план не меняется — в API кейса нет события «визит завершён», статусы
        визитов в плане идут по модельному времени (D42)."""
        body = body if isinstance(body, dict) else {}
        kind, oid, sid = body.get("kind"), body.get("order_id"), body.get("staff_id")
        if kind not in ("call", "arrive", "done") or not isinstance(oid, str) or not isinstance(sid, str):
            return error(400, "BAD_REQUEST", "kind ∈ {call, arrive, done}, order_id, staff_id")
        svc = self.service
        with svc.lock:
            s = svc.session
            if s is None or s.plan is None:
                return error(409, "PLAN_NOT_BUILT", "План ещё не построен")
            r = s.plan.routes.get(sid)
            if r is None or not any(st.order_id == oid for st in r.stops):
                return error(422, "NOT_IN_ROUTE", f"{oid} нет в маршруте {sid}")
            tb = timebase_of(s.day)
            marks = self.marks.setdefault((id(s), oid), {})
            if body.get("undo"):
                for k in (kind, f"{kind}_wall"):
                    marks.pop(k, None)
                if kind == "arrive":  # без прибытия не бывает завершения
                    marks.pop("done", None)
                    marks.pop("done_wall", None)
            else:
                if kind == "done" and "arrive" not in marks:
                    return error(422, "NOT_ARRIVED", "Сначала отметьте прибытие на адрес")
                marks[kind] = tb.format(s.now)  # модельное время (в реальном дне совпадает с часами)
                marks[f"{kind}_wall"] = datetime.now(tb.tz).isoformat(timespec="seconds")  # когда нажали
            # кеш состояния не сбрасываем: отметки отдаются отдельно и в ответе «без изменений»
        return 200, {"order_id": oid, "marks": marks}

    def tech_event(self, body: Any) -> tuple[int, dict[str, Any]]:
        """Сотрудник сообщает о задержке на объекте или о поломке машины — уходит событием API
        (`VISIT_DELAYED` / `STAFF_UNAVAILABLE`) в перепланирование. Только в демо: в оцениваемом
        прогоне события присылает оркестратор."""
        body = body if isinstance(body, dict) else {}
        svc = self.service
        if svc is self.main:
            return error(
                409, "DEMO_ONLY", "В реальном дне события присылает оркестратор; кнопка работает в демо"
            )
        kind, sid = body.get("kind"), body.get("staff_id")
        with svc.lock:
            s = svc.session
            if s is None or s.plan is None:
                return error(409, "PLAN_NOT_BUILT", "План ещё не построен")
            tb = timebase_of(s.day)
            self._tech_seq += 1
            ev: dict[str, Any] = {"event_id": f"TECH-{self._tech_seq:04d}", "timestamp": tb.format(s.now)}
            if kind == "delay":
                oid = body.get("order_id")
                extra = int(body.get("extra_min", 20) or 20)
                st = (
                    next((x for x in s.plan.routes.get(sid, None).stops if x.order_id == oid), None)
                    if sid in s.plan.routes
                    else None
                )
                if st is None:
                    return error(422, "NOT_IN_ROUTE", f"{oid} нет в маршруте {sid}")
                if not st.start <= s.now:
                    return error(422, "NOT_STARTED", "Задержку можно сообщить только по начатому визиту")
                ev.update(
                    type="VISIT_DELAYED",
                    staff_id=sid,
                    order_id=oid,
                    expected_end=tb.format(max(st.end, s.now) + extra * 60),
                )
            elif kind == "unavailable":
                ev.update(
                    type="STAFF_UNAVAILABLE", staff_id=sid, reason=body.get("reason", "VEHICLE_BREAKDOWN")
                )
            else:
                return error(400, "BAD_REQUEST", "kind ∈ {delay, unavailable}")
            date = s.day.date
        status, res = svc.post_event(date, ev)
        self._cache = None
        if status == 200:
            self.demo.last_event = ev
            self.demo.last_result = res
        return status, res


def ui_dist_dir() -> Path | None:
    import os

    env = os.environ.get("ADO_UI_DIR")
    candidates = [Path(env)] if env else []
    candidates += [Path(__file__).resolve().parents[3] / "frontend" / "dist", Path("/app/frontend/dist")]
    for c in candidates:
        if (c / "index.html").exists():
            return c
    return None
