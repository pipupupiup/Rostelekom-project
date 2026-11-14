"""ASGI-приложение REST API (ADO-16) — пути, методы и коды строго по `spec/api_spec.yaml`.

Обработчики — тонкая обёртка над `DispatchService`. Тела читаются как JSON вручную: формат
ошибок обязан совпадать с `ErrorResponse`, поэтому стандартная валидация фреймворка не
используется (проверку схемы выполняет ADO-01 с путём к полю). Тяжёлые операции идут в пуле
потоков, чтобы `GET /health` отвечал во время расчёта плана.

Сборка: `FastAPI(routes=...)`, если FastAPI установлен (Docker-образ), иначе тот же набор
маршрутов на Starlette (на нём основан FastAPI) — для локальной проверки без лишних пакетов.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ado import __version__
from ado.api.service import DispatchService, error
from ado.api.ui import UiApi, ui_dist_dir
from ado.config import Settings

API_PREFIX = "/api/v1"
log = logging.getLogger("ado.api")


class JSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _body(request: Request, optional: bool = False) -> tuple[Any, Response | None]:
    raw = await request.body()
    if not raw.strip():
        if optional:
            return None, None
        status, body = error(400, "BAD_REQUEST", "Пустое тело запроса")
        return None, JSON(body, status_code=status)
    try:
        return json.loads(raw), None
    except (ValueError, UnicodeDecodeError) as e:
        status, body = error(400, "BAD_REQUEST", f"Некорректный JSON: {e}")
        return None, JSON(body, status_code=status)


def build_routes(service: DispatchService) -> list[Route]:
    def handler(
        fn: Callable[..., tuple[int, dict]],
        *,
        needs_body: bool,
        optional: bool = False,
        with_date: bool = False,
    ):
        async def endpoint(request: Request) -> Response:
            args: list[Any] = []
            if with_date:
                args.append(request.path_params["date"])
            if needs_body:
                body, err = await _body(request, optional)
                if err is not None:
                    return err
                args.append(body)
            try:
                status, payload = await run_in_threadpool(fn, *args)
            except Exception as e:  # сервис не должен падать целиком
                log.exception("необработанная ошибка")
                status, payload = error(500, "INTERNAL_ERROR", f"{type(e).__name__}: {e}")
            return JSON(payload, status_code=status)

        return endpoint

    s = service
    return [
        Route(f"{API_PREFIX}/health", handler(s.health, needs_body=False), methods=["GET"]),
        Route(f"{API_PREFIX}/reference", handler(s.put_reference, needs_body=True), methods=["PUT"]),
        Route(
            f"{API_PREFIX}/reference/travel-matrix",
            handler(s.put_travel_matrix, needs_body=True),
            methods=["PUT"],
        ),
        Route(
            f"{API_PREFIX}/days/{{date}}",
            handler(s.put_day, needs_body=True, with_date=True),
            methods=["PUT"],
        ),
        Route(
            f"{API_PREFIX}/days/{{date}}/plan",
            handler(s.post_plan, needs_body=True, optional=True, with_date=True),
            methods=["POST"],
        ),
        Route(
            f"{API_PREFIX}/days/{{date}}/plan",
            handler(s.get_plan, needs_body=False, with_date=True),
            methods=["GET"],
        ),
        Route(
            f"{API_PREFIX}/days/{{date}}/events",
            handler(s.post_event, needs_body=True, with_date=True),
            methods=["POST"],
        ),
    ]


def build_ui_routes(service: DispatchService, geometry=None) -> list[Route | Mount]:
    ui = UiApi(service, geometry)

    def handler(fn, needs_body: bool):
        async def endpoint(request: Request) -> Response:
            args: list[Any] = []
            if needs_body:
                body, err = await _body(request, optional=True)
                if err is not None:
                    return err
                args.append(body)
            try:
                status, payload = await run_in_threadpool(fn, *args)
            except Exception as e:
                log.exception("ошибка UI API")
                status, payload = error(500, "INTERNAL_ERROR", f"{type(e).__name__}: {e}")
            return JSON(payload, status_code=status)

        return endpoint

    async def root(_: Request) -> Response:
        return RedirectResponse("/ui/")

    async def state_endpoint(request: Request) -> Response:
        raw = request.query_params.get("since_version")
        since = int(raw) if raw and raw.isdigit() else None
        status, payload = await run_in_threadpool(ui.state, since)
        return JSON(payload, status_code=status)

    async def geometry_endpoint(request: Request) -> Response:
        raw = request.query_params.get("staff_id") or ""
        ids = [x for x in (p.strip() for p in raw.split(",")) if x][:100]
        status, payload = await run_in_threadpool(ui.route_geometry, ids)
        return JSON(payload, status_code=status)

    routes: list[Route | Mount] = [
        Route("/", root, methods=["GET"]),
        Route("/ui-api/state", state_endpoint, methods=["GET"]),
        Route("/ui-api/demo/start", handler(ui.demo_start, True), methods=["POST"]),
        Route("/ui-api/demo/step", handler(ui.demo_step, True), methods=["POST"]),
        Route("/ui-api/demo/exit", handler(ui.demo_exit, False), methods=["POST"]),
        Route("/ui-api/history", handler(ui.history, True), methods=["POST"]),
        Route("/ui-api/scenario", handler(ui.scenario, True), methods=["POST"]),
        Route("/ui-api/tech/mark", handler(ui.tech_mark, True), methods=["POST"]),
        Route("/ui-api/tech/event", handler(ui.tech_event, True), methods=["POST"]),
        Route("/ui-api/route-geometry", geometry_endpoint, methods=["GET"]),
    ]
    dist = ui_dist_dir()
    if dist is not None:
        routes.append(Mount("/ui", app=StaticFiles(directory=str(dist), html=True), name="ui"))
    else:

        async def no_ui(_: Request) -> Response:
            return HTMLResponse(
                "<h1>Интерфейс не собран</h1><p>cd frontend && npm install && npm run build</p>",
                status_code=503,
            )

        routes.append(Route("/ui/", no_ui, methods=["GET"]))
    return routes


def _store_from(settings: Settings):
    """PostgreSQL по ADO_DATABASE_URL; при недоступности БД сервис стартует без неё (оценка от БД не зависит)."""
    if not settings.database_url:
        return None
    from ado.persistence.db import database_from_url
    from ado.persistence.store import PlanStore

    store = PlanStore(database_from_url(settings.database_url))
    try:
        store.migrate()
        log.info("PostgreSQL подключён, схема актуальна")
    except Exception as e:  # БД может подняться позже — запись версий будет повторять попытки
        log.error("PostgreSQL недоступен при старте: %s", e)
    return store


def _api_spec() -> dict | None:
    """`spec/api_spec.yaml` для Swagger UI (`/docs`). Нет файла или PyYAML — схема FastAPI по умолчанию."""
    from pathlib import Path

    for p in (
        Path(__file__).resolve().parents[3] / "spec" / "api_spec.yaml",
        Path("/app/spec/api_spec.yaml"),
    ):
        if p.exists():
            try:
                import yaml

                return yaml.safe_load(p.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("spec для /docs не прочитан: %s", e)
                return None
    return None


def _load_history(service: DispatchService, path: str) -> None:
    """История нарядов для прогноза длительности (D39). Ошибка не мешает старту сервиса."""
    import json

    from ado.optimization.durations import DurationModel

    try:
        with open(path, encoding="utf-8") as f:
            service.durations = DurationModel.fit(json.load(f), label=path)
        log.info("история нарядов: %s, %d выполненных", path, service.durations.size)
    except (OSError, ValueError) as e:
        log.error("история нарядов %s не загружена: %s", path, e)


def create_app(settings: Settings | None = None, service: DispatchService | None = None, geometry=None):
    settings = settings or Settings.from_env()
    service = service or DispatchService(settings, store=_store_from(settings))
    if settings.history_file and service.durations is None:
        _load_history(service, settings.history_file)
    routes = build_routes(service) + build_ui_routes(service, geometry)
    try:
        from fastapi import FastAPI

        app = FastAPI(title="ADO — Adaptive Dispatch Optimizer", version=__version__, routes=routes)
        spec = _api_spec()
        if spec is not None:  # /docs показывает спецификацию организаторов — те же пути, что реализованы
            spec["servers"] = [{"url": "/", "description": "этот сервис"}]
            app.openapi = lambda: spec
    except ImportError:  # локальная разработка без FastAPI
        app = Starlette(routes=routes)
    app.state.service = service
    return app


app = create_app()
