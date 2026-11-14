"""Дорожная геометрия маршрутов для карты диспетчера (D35): GraphHopper или OSRM.

Только для отображения. Время в пути в плане по-прежнему берётся из матрицы организаторов (D2):
геометрия не участвует ни в оптимизации, ни в валидации и запрашивается вне блокировки сервиса.

Движки (выбор — `routing_from_env` / `Settings`):
* **GraphHopper** (основной, D37) — `GET {ADO_GRAPHHOPPER_URL}/route?point=lat,lon&…&profile=car
  &points_encoded=false&instructions=false`; свой сервер (graphhopper-web, OSM-выгрузка) или
  GraphHopper Directions API с ключом `ADO_GRAPHHOPPER_KEY`;
* **OSRM** — `GET {ADO_OSRM_URL}/route/v1/driving/{lon,lat;…}?overview=full&geometries=geojson`.

Ответ — линия по дорогам OSM, длина и время движка, расстояние «привязки» точки к дороге (большое —
точка далеко от дорог, это видно в UI). Нет настройки или сервис не отвечает — `RoadGeometryError`,
интерфейс пишет причину и **не** выдаёт прямые отрезки за дороги. Результаты кешируются по
координатам; после ошибки связи повтор не раньше `retry_after_sec`.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlencode

from ado.routing.osrm import Coord, Transport, urllib_transport


class RoadGeometryError(RuntimeError):
    """Движок не настроен, недоступен или не нашёл маршрут. `transient` — ошибка связи."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class RoadRoute:
    coordinates: list[tuple[float, float]]
    """(lat, lon) вдоль дорог."""
    distance_m: float
    duration_s: float
    max_snap_m: float
    """Наибольшее расстояние от точки остановки до ближайшей дороги."""

    def to_json(self) -> dict:
        return {
            "coordinates": [[round(a, 6), round(b, 6)] for a, b in self.coordinates],
            "distance_km": round(self.distance_m / 1000, 2),
            "duration_min": round(self.duration_s / 60, 1),
            "max_snap_m": round(self.max_snap_m),
        }


class RouteClient(Protocol):
    name: str
    base_url: str

    def route(self, coords: Sequence[Coord]) -> RoadRoute: ...


def _haversine_m(a: Coord, b: Coord) -> float:
    r = 6371008.8
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _call(transport: Transport, url: str, timeout: float, name: str) -> tuple[int, dict]:
    try:
        status, body = transport(url, timeout)
    except (OSError, TimeoutError) as e:
        raise RoadGeometryError(f"{name} недоступен: {type(e).__name__}: {e}", transient=True) from e
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RoadGeometryError(f"{name}: некорректный ответ (HTTP {status})") from e
    if not isinstance(data, dict):
        raise RoadGeometryError(f"{name}: некорректный ответ (HTTP {status})")
    return status, data


def _lonlat_list(geom) -> list[tuple[float, float]]:
    """GeoJSON LineString или массив [lon, lat(, ele)] → [(lat, lon)]."""
    pts = geom.get("coordinates") if isinstance(geom, Mapping) else geom
    return [(float(p[1]), float(p[0])) for p in pts]


class GraphHopperRouteClient:
    name = "GraphHopper"

    def __init__(
        self,
        base_url: str,
        profile: str = "car",
        key: str | None = None,
        timeout_sec: float = 10.0,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self._key = key
        self.timeout_sec = timeout_sec
        self._transport = transport or urllib_transport
        self.requests_made = 0

    def url(self, coords: Sequence[Coord]) -> str:
        q: list[tuple[str, str]] = [("point", f"{lat:.6f},{lon:.6f}") for lat, lon in coords]
        q += [
            ("profile", self.profile),
            ("points_encoded", "false"),
            ("instructions", "false"),
            ("calc_points", "true"),
        ]
        if self._key:
            q.append(("key", self._key))
        return f"{self.base_url}/route?{urlencode(q)}"

    def route(self, coords: Sequence[Coord]) -> RoadRoute:
        if len(coords) < 2:
            raise ValueError("нужно не меньше двух точек")
        self.requests_made += 1
        status, data = _call(self._transport, self.url(coords), self.timeout_sec, self.name)
        if status != 200:
            msg = str(data.get("message") or "").strip()
            if status in (401, 403):
                msg = msg or "нет доступа — проверьте ADO_GRAPHHOPPER_KEY"
            raise RoadGeometryError(f"GraphHopper: HTTP {status}{': ' + msg if msg else ''}")
        try:
            p = data["paths"][0]
            pts = _lonlat_list(p["points"])
            snapped = _lonlat_list(p["snapped_waypoints"]) if p.get("snapped_waypoints") else []
            snap = max((_haversine_m(a, b) for a, b in zip(coords, snapped, strict=False)), default=0.0)
            return RoadRoute(pts, float(p["distance"]), float(p["time"]) / 1000, snap)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise RoadGeometryError("GraphHopper: в ответе нет геометрии маршрута") from e


class OsrmRouteClient:
    name = "OSRM"

    def __init__(
        self,
        base_url: str,
        profile: str = "driving",
        timeout_sec: float = 10.0,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.timeout_sec = timeout_sec
        self._transport = transport or urllib_transport
        self.requests_made = 0

    def url(self, coords: Sequence[Coord]) -> str:
        locs = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
        return (
            f"{self.base_url}/route/v1/{quote(self.profile)}/{locs}"
            "?overview=full&geometries=geojson&steps=false"
        )

    def route(self, coords: Sequence[Coord]) -> RoadRoute:
        if len(coords) < 2:
            raise ValueError("нужно не меньше двух точек")
        self.requests_made += 1
        status, data = _call(self._transport, self.url(coords), self.timeout_sec, self.name)
        if status != 200 or data.get("code") != "Ok":
            raise RoadGeometryError(
                f"OSRM: HTTP {status}, {data.get('code', '?')}: {data.get('message', '')}".rstrip(": ")
            )
        try:
            r = data["routes"][0]
            pts = _lonlat_list(r["geometry"])
            snaps = [float(w.get("distance", 0.0)) for w in data.get("waypoints", [])]
            return RoadRoute(pts, float(r["distance"]), float(r["duration"]), max(snaps, default=0.0))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise RoadGeometryError("OSRM: в ответе нет геометрии маршрута") from e


NOT_CONFIGURED = (
    "дорожная геометрия не настроена: задайте ADO_GRAPHHOPPER_URL (сервер GraphHopper) "
    "или ADO_OSRM_URL (сервер OSRM)"
)


class RouteGeometryService:
    """Кеш + понятный статус для UI. `client=None` — геометрия не настроена."""

    def __init__(
        self,
        client: RouteClient | None,
        cache_size: int = 1024,
        retry_after_sec: float = 30.0,
        clock=time.monotonic,
        hint: str | None = None,
    ) -> None:
        self.client = client
        self._cache: OrderedDict[tuple, RoadRoute] = OrderedDict()
        self._size = cache_size
        self._retry_after = retry_after_sec
        self._clock = clock
        self._down_until = 0.0
        self.hint = hint
        """Что сделать, если сервис не отвечает (добавляется к сообщению)."""
        self.last_error: str | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.client is not None

    @property
    def source(self) -> str | None:
        return f"{self.client.name} {self.client.base_url}" if self.client else None

    def route(self, coords: Sequence[Coord]) -> RoadRoute:
        if self.client is None:
            raise RoadGeometryError(NOT_CONFIGURED)
        key = tuple((round(a, 6), round(b, 6)) for a, b in coords)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
            if self._clock() < self._down_until and self.last_error:
                raise RoadGeometryError(self.last_error, transient=True)
        try:
            res = self.client.route(coords)
        except RoadGeometryError as e:
            msg = str(e) + (f". {self.hint}" if e.transient and self.hint else "")
            with self._lock:
                self.last_error = msg
                if e.transient:  # сеть: не долбим сервис при каждом опросе UI
                    self._down_until = self._clock() + self._retry_after
            raise RoadGeometryError(msg, e.transient) from e
        with self._lock:
            self.last_error = None
            self._down_until = 0.0
            self._cache[key] = res
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)
        return res


def routing_from_settings(
    graphhopper_url: str | None,
    graphhopper_key: str | None = None,
    graphhopper_profile: str = "car",
    osrm_url: str | None = None,
    transport: Transport | None = None,
) -> RouteGeometryService:
    """GraphHopper, если задан его адрес; иначе OSRM; иначе «не настроено»."""
    if graphhopper_url:
        return RouteGeometryService(
            GraphHopperRouteClient(
                graphhopper_url, graphhopper_profile, graphhopper_key, transport=transport
            ),
            hint="Свой сервер: docker compose --profile graphhopper up -d (README, «Карта и дороги»)",
        )
    if osrm_url:
        return RouteGeometryService(OsrmRouteClient(osrm_url, transport=transport))
    return RouteGeometryService(None)


__all__ = [
    "NOT_CONFIGURED",
    "GraphHopperRouteClient",
    "OsrmRouteClient",
    "RoadGeometryError",
    "RoadRoute",
    "RouteGeometryService",
    "routing_from_settings",
]
