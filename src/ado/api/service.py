"""Сервисный слой REST API (ADO-16), независимый от веб-фреймворка.

Каждый метод соответствует операции `spec/api_spec.yaml` и возвращает `(HTTP-код, тело)`;
тела ошибок — `ErrorResponse`. Состояние держится в памяти (D5): справочники, матрица и один
активный день (`DaySession`). Изменяющие операции выполняются последовательно под блокировкой —
событие, пришедшее во время перепланирования, ждёт его окончания и видит итоговое состояние.
При ошибке состояние не меняется: новые объекты собираются отдельно и подменяются в конце.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ado import __version__
from ado.api.serialize import alert_json, plan_json
from ado.config import Settings
from ado.data.errors import DataPreparationError
from ado.data.prepare import prepare_day_report, prepare_event, prepare_reference_report, timebase_of
from ado.domain.models import DayInstance, ReferenceData
from ado.domain.timebase import TimeBase
from ado.events.session import DaySession, ReoptConfig, SessionError
from ado.persistence.db import DbError
from ado.persistence.store import PlanStore
from ado.routing.matrix import MatrixTravelProvider, TrafficTravelProvider, TravelMatrix

log = logging.getLogger("ado.api")
Response = tuple[int, dict[str, Any]]


def error(status: int, code: str, message: str, details: list[dict[str, str]] | None = None) -> Response:
    return status, {"error": {"code": code, "message": message, "details": details or []}}


def _prep_error(e: DataPreparationError) -> Response:
    status = 400 if e.code == "BAD_REQUEST" else 422
    code = "BAD_REQUEST" if status == 400 else "VALIDATION_FAILED"
    return error(status, code, str(e), [{"field": i.field, "message": i.message} for i in e.issues[:200]])


@dataclass(slots=True)
class DispatchService:
    settings: Settings = field(default_factory=Settings)
    config: ReoptConfig = field(default_factory=ReoptConfig)
    reference: ReferenceData | None = None
    matrix: TravelMatrix | None = None
    session: DaySession | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    timings: list[tuple[str, float]] = field(default_factory=list)
    store: PlanStore | None = None
    """PostgreSQL (ADO-15): история версий и событий. None — работа только в памяти."""
    db_errors: list[str] = field(default_factory=list)
    db_saved_version: int | None = None
    durations: Any = None
    """`DurationModel` по истории нарядов (D39) — только для прогноза рисков и карточек; план по нормативу."""

    def _persist(self, what: str, fn, *args) -> None:
        """Запись в БД в той же критической секции, что и публикация (нет поздней записи устаревшего
        плана). Ошибка БД не ломает API: оценка от БД не зависит, ошибка фиксируется и видна в UI."""
        if self.store is None:
            return
        t0 = time.perf_counter()
        try:
            res = fn(*args)
            if hasattr(res, "version"):
                self.db_saved_version = res.version
        except (DbError, OSError, ConnectionError) as e:
            msg = f"{what}: {type(e).__name__}: {e}"
            log.error("БД: %s", msg)
            self.db_errors.append(msg[:500])
        finally:
            self._timed(f"db {what}", t0)

    def _timed(self, name: str, t0: float) -> None:
        dt = time.perf_counter() - t0
        self.timings.append((name, dt))
        log.info("%s: %.3f с", name, dt)

    # ---------------------------------------------------------------- service
    def health(self) -> Response:
        return 200, {"status": "ok", "team": self.settings.team, "version": __version__}

    # ---------------------------------------------------------------- reference
    def put_reference(self, body: Any) -> Response:
        t0 = time.perf_counter()
        try:
            prepared = prepare_reference_report(body)
        except DataPreparationError as e:
            return _prep_error(e)
        ref = prepared.value
        if self.matrix is not None:
            missing = self.matrix.missing([*ref.service_centers, *ref.buildings])
            if missing:
                return error(
                    422,
                    "MATRIX_MISMATCH",
                    f"в загруженной матрице нет {len(missing)} точек справочника",
                    [{"field": "points", "message": ", ".join(missing[:20])}],
                )
        with self.lock:
            self.reference = ref
            self.session = None  # «все загруженные ранее дни, планы и события удаляются»
            self._persist("reference", self.store.save_reference if self.store else None, body)
        self._timed("PUT /reference", t0)
        return 200, {
            "status": "loaded",
            "counts": {
                "service_centers": len(ref.service_centers),
                "staff": len(ref.employees),
                "access_nodes": len(ref.access_nodes),
                "buildings": len(ref.buildings),
                "work_types": len(ref.work_types),
            },
        }

    def put_travel_matrix(self, body: Any) -> Response:
        t0 = time.perf_counter()
        try:
            matrix = TravelMatrix.from_api(body)
        except DataPreparationError as e:
            return _prep_error(e)
        if self.reference is not None:
            missing = matrix.missing([*self.reference.service_centers, *self.reference.buildings])
            if missing:
                return error(
                    422,
                    "MATRIX_MISMATCH",
                    f"в матрице нет {len(missing)} точек справочника",
                    [{"field": "points", "message": ", ".join(missing[:20])}],
                )
        with self.lock:
            self.matrix = matrix
            if self.session is not None:
                self.session.travel = MatrixTravelProvider(matrix)
        self._timed("PUT /reference/travel-matrix", t0)
        return 200, {"status": "loaded", "counts": {"points": len(matrix)}}

    # ---------------------------------------------------------------- day
    def put_day(self, day_str: str, body: Any) -> Response:
        t0 = time.perf_counter()
        if not _valid_date(day_str):
            return error(
                400,
                "BAD_REQUEST",
                f"Некорректная дата {day_str!r}",
                [{"field": "date", "message": "YYYY-MM-DD"}],
            )
        if self.reference is None:
            return error(409, "REFERENCE_NOT_LOADED", "Справочники не загружены (PUT /api/v1/reference)")
        if self.matrix is None:
            return error(
                409, "TRAVEL_MATRIX_NOT_LOADED", "Матрица не загружена (PUT /api/v1/reference/travel-matrix)"
            )
        if isinstance(body, Mapping) and body.get("date") not in (None, day_str):
            return error(
                422,
                "DATE_MISMATCH",
                f"date в теле ({body.get('date')}) ≠ дате в пути ({day_str})",
                [{"field": "date", "message": "должна совпадать с параметром пути"}],
            )
        try:
            prepared = prepare_day_report(body, self.reference)
        except DataPreparationError as e:
            return _prep_error(e)
        day = prepared.value
        missing = self.matrix.missing(o.building_id for o in day.orders.values())
        if missing:
            return error(422, "MATRIX_MISMATCH", f"в матрице нет домов нарядов: {missing[:10]}")
        travel = (
            TrafficTravelProvider(self.matrix)
            if self.settings.traffic and self.matrix.hourly_factors
            else MatrixTravelProvider(self.matrix)
        )
        session = DaySession(_mutable_day(day), travel, self.config)
        with self.lock:
            self.session = session  # один активный день; повторная загрузка сбрасывает план и события
            self._persist(
                "day",
                self.store.save_day if self.store else None,
                day_str,
                body["now"],
                body["orders"],
                body.get("absent_staff_ids") or [],
            )
        self._timed(f"PUT /days/{day_str}", t0)
        return 200, {"status": "loaded", "counts": {"orders": len(day.orders)}}

    def _session(self, day_str: str) -> DaySession | Response:
        s = self.session
        if s is None or s.day.date != day_str:
            return error(404, "DAY_NOT_FOUND", f"День {day_str} не загружен")
        return s

    # ---------------------------------------------------------------- plan
    def post_plan(self, day_str: str, body: Any) -> Response:
        t0 = time.perf_counter()
        limit = 300
        if body not in (None, b"", {}):
            if not isinstance(body, Mapping):
                return error(400, "BAD_REQUEST", "ожидается объект PlanRequest")
            v = body.get("time_limit_sec", 300)
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 300:
                return error(
                    400,
                    "BAD_REQUEST",
                    "time_limit_sec — целое 1…300",
                    [{"field": "time_limit_sec", "message": "целое 1…300"}],
                )
            limit = v
        with self.lock:
            s = self._session(day_str)
            if isinstance(s, tuple):
                return s
            try:
                plan = s.build_plan(limit)
            except SessionError as e:
                return error(e.status, e.code, str(e))
            body_out = plan_json(plan, s.day, timebase_of(s.day))
            self._persist(
                "plan v1", self.store.save_version if self.store else None, day_str, body_out, None, "initial"
            )
        self._timed(f"POST /days/{day_str}/plan", t0)
        return 200, body_out

    def get_plan(self, day_str: str) -> Response:
        with self.lock:
            s = self._session(day_str)
            if isinstance(s, tuple):
                return s
            if s.plan is None:
                return error(404, "PLAN_NOT_FOUND", f"План на {day_str} ещё не построен")
            return 200, plan_json(s.plan, s.day, timebase_of(s.day))

    # ---------------------------------------------------------------- events
    def post_event(self, day_str: str, body: Any) -> Response:
        t0 = time.perf_counter()
        with self.lock:
            s = self._session(day_str)
            if isinstance(s, tuple):
                return s
            if s.plan is None:
                return error(404, "PLAN_NOT_FOUND", f"План на {day_str} ещё не построен")
            tb = timebase_of(s.day)
            if isinstance(body, Mapping) and body.get("event_id") in s.results:
                prev = s.results[body["event_id"]]
                return 200, {
                    "event_id": prev.event_id,
                    "accepted": True,
                    "duplicate": True,
                    "plan_version": prev.plan_version,
                    "as_of": tb.format(prev.as_of),
                    "processing_ms": 0,
                    "changed_staff_ids": [],
                    "new_alerts": [],
                    "message": "повтор события",
                }
            try:
                event = prepare_event(body, s.day)
            except DataPreparationError as e:
                return _prep_error(e)
            try:
                out = s.apply(event)
            except SessionError as e:
                return error(
                    e.status, e.code, str(e), [{"field": e.field, "message": str(e)}] if e.field else []
                )
            t_db = time.perf_counter()
            if not out.duplicate:
                self._persist(
                    f"plan v{out.plan_version}",
                    self.store.save_version if self.store else None,
                    day_str,
                    plan_json(s.plan, s.day, tb),
                    out.plan_version - 1,
                    event.event_id,
                    dict(body),
                )
            stages = dict(out.stages_ms)
            stages["persist"] = int((time.perf_counter() - t_db) * 1000)
            self._timed(f"POST /days/{day_str}/events {event.type}", t0)
            return 200, {
                "event_id": out.event_id,
                "accepted": out.accepted,
                "duplicate": out.duplicate,
                "plan_version": out.plan_version,
                "as_of": tb.format(out.as_of),
                "processing_ms": int((time.perf_counter() - t0) * 1000),
                "changed_staff_ids": out.changed_staff_ids,
                "new_alerts": [alert_json(a, tb) for a in out.new_alerts],
                "message": out.message,
                "stages_ms": stages,
            }


def _valid_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return len(s) == 10


def _mutable_day(day: DayInstance) -> DayInstance:
    """Сессия добавляет наряды из NEW_ORDER — работаем с собственной копией словаря."""
    return DayInstance(day.date, day.now, dict(day.orders), day.absent_staff_ids, day.reference)


__all__ = ["DispatchService", "TimeBase", "error"]
