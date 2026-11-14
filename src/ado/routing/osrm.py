"""Адаптер OSRM Table API (ADO-02, п. 2–4, 7–8).

Используется **вне** горячего пути: для подготовки матрицы (синтетические сценарии, точки вне
матрицы организаторов) и для геометрии линий в UI. В оцениваемом прогоне время в пути берётся из
единой матрицы организаторов (D2).

* значения — `duration` (с) и `distance` (м) из OSRM, округление до целых по правилу half-up;
* направление сохраняется: `durations[i][j]` — из i в j;
* `null` в ответе OSRM — недостижимая пара (`None`), без подмены прямой линией;
* большие матрицы собираются блоками: в одном запросе не больше `max_table_size` координат.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

Coord = tuple[float, float]
"""(lat, lon)"""
Transport = Callable[[str, float], tuple[int, bytes]]
"""(url, timeout_sec) → (HTTP status, тело ответа)."""


class OsrmError(RuntimeError):
    """Ошибка OSRM: таймаут, HTTP-ошибка, некорректный ответ или `code != Ok`."""


@dataclass(frozen=True, slots=True)
class OsrmConfig:
    base_url: str = "http://osrm:5000"
    profile: str = "driving"
    timeout_sec: float = 10.0
    max_table_size: int = 100
    """Лимит координат в одном запросе (параметр `--max-table-size` у osrm-routed, по умолчанию 100)."""
    retries: int = 1
    """Повторов при сетевой ошибке/таймауте (HTTP 4xx не повторяются)."""


def urllib_transport(url: str, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""


def _round(v: float) -> int:
    return math.floor(v + 0.5)


@dataclass(frozen=True, slots=True)
class TableBlock:
    durations: list[list[int | None]]
    distances: list[list[int | None]]


class OsrmTableClient:
    def __init__(self, config: OsrmConfig | None = None, transport: Transport | None = None) -> None:
        self.config = config or OsrmConfig()
        self._transport = transport or urllib_transport
        self.requests_made = 0

    def _url(self, coords: Sequence[Coord], sources: Sequence[int], destinations: Sequence[int]) -> str:
        c = self.config
        locs = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
        src = ";".join(map(str, sources))
        dst = ";".join(map(str, destinations))
        return (
            f"{c.base_url.rstrip('/')}/table/v1/{c.profile}/{locs}"
            f"?sources={src}&destinations={dst}&annotations=duration,distance"
        )

    def _request(self, url: str) -> dict:
        last: Exception | None = None
        for _ in range(self.config.retries + 1):
            self.requests_made += 1
            try:
                status, body = self._transport(url, self.config.timeout_sec)
            except (TimeoutError, OSError) as e:  # urllib.error.URLError — подкласс OSError
                last = OsrmError(f"OSRM недоступен: {e}")
                continue
            if status != 200:
                raise OsrmError(f"OSRM ответил HTTP {status}: {body[:200]!r}")
            try:
                data = json.loads(body)
            except ValueError as e:
                raise OsrmError(f"OSRM вернул не JSON: {e}") from e
            if data.get("code") != "Ok":
                raise OsrmError(f"OSRM: {data.get('code')}: {data.get('message', '')}")
            return data
        raise last or OsrmError("OSRM недоступен")

    def table(self, sources: Sequence[Coord], destinations: Sequence[Coord]) -> TableBlock:
        """Блок sources × destinations одним запросом (размеры — в пределах лимита)."""
        if len(sources) + len(destinations) > self.config.max_table_size:
            raise ValueError("блок больше max_table_size — используйте matrix()")
        coords = list(sources) + list(destinations)
        n = len(sources)
        data = self._request(self._url(coords, range(n), range(n, len(coords))))
        durs, dists = data.get("durations"), data.get("distances")
        if durs is None or dists is None:
            raise OsrmError("в ответе OSRM нет durations/distances (нужен annotations=duration,distance)")
        if len(durs) != n or any(len(row) != len(destinations) for row in durs):
            raise OsrmError("размер ответа OSRM не совпадает с запросом")

        def conv(m: list[list[float | None]]) -> list[list[int | None]]:
            return [[None if v is None else _round(v) for v in row] for row in m]

        return TableBlock(conv(durs), conv(dists))

    def matrix(self, sources: Sequence[Coord], destinations: Sequence[Coord] | None = None) -> TableBlock:
        """Произвольная матрица, собранная блоками по `max_table_size // 2` координат на сторону."""
        destinations = sources if destinations is None else destinations
        chunk = max(1, self.config.max_table_size // 2)
        durs = [[None] * len(destinations) for _ in sources]
        dists = [[None] * len(destinations) for _ in sources]
        for i0 in range(0, len(sources), chunk):
            src = sources[i0 : i0 + chunk]
            for j0 in range(0, len(destinations), chunk):
                block = self.table(src, destinations[j0 : j0 + chunk])
                for di, (drow, mrow) in enumerate(zip(block.durations, block.distances, strict=True)):
                    durs[i0 + di][j0 : j0 + len(drow)] = drow
                    dists[i0 + di][j0 : j0 + len(mrow)] = mrow
        return TableBlock(durs, dists)
