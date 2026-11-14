"""Кеш дорожной матрицы на базе OSRM (ADO-02, п. 6, 9–12).

* дубликаты координат схлопываются: несколько ID с одинаковыми координатами (≈ 0,1 м) делят
  одну строку матрицы, соответствие исходным ID сохраняется;
* `add_points()` досчитывает **только** недостающие элементы: новые × все и старые × новые;
* `travel_sec()`/`distance_m()` никогда не обращаются к сети — неизвестная точка даёт
  `UnknownPointError`, недостижимая пара — `None`;
* `snapshot()` даёт неизменяемую `TravelMatrix` для оптимизатора.
"""

from __future__ import annotations

from array import array
from collections.abc import Mapping

from ado.routing.matrix import UNREACHABLE, TravelMatrix, UnknownPointError
from ado.routing.osrm import Coord, OsrmTableClient


def _key(c: Coord) -> tuple[int, int]:
    return round(c[0] * 1e6), round(c[1] * 1e6)


class OsrmMatrixCache:
    def __init__(self, client: OsrmTableClient, matrix_id: str = "osrm") -> None:
        self.client = client
        self.matrix_id = matrix_id
        self._coords: list[Coord] = []
        self._coord_index: dict[tuple[int, int], int] = {}
        self._point_coord: dict[str, int] = {}
        self._dur: list[array] = []
        self._dist: list[array] = []

    @property
    def point_ids(self) -> list[str]:
        return list(self._point_coord)

    def has_point(self, point_id: str) -> bool:
        return point_id in self._point_coord

    def add_points(self, points: Mapping[str, Coord]) -> int:
        """Добавить точки; вернуть число новых уникальных координат. Повторное добавление — без запросов."""
        # Сначала считаем во временных структурах: при ошибке OSRM кеш остаётся согласованным.
        coord_index = dict(self._coord_index)
        point_coord = dict(self._point_coord)
        new_coords: list[Coord] = []
        for pid, coord in points.items():
            k = _key(coord)
            idx = coord_index.get(k)
            if idx is None:
                idx = len(self._coords) + len(new_coords)
                coord_index[k] = idx
                new_coords.append(coord)
            prev = point_coord.get(pid)
            if prev is not None and prev != idx:
                raise ValueError(f"точка {pid} уже зарегистрирована с другими координатами")
            point_coord[pid] = idx
        if not new_coords:
            self._point_coord = point_coord
            return 0
        old = list(self._coords)
        n_old = len(old)
        # новые × (старые + новые)
        everything = old + new_coords
        block_new = self.client.matrix(new_coords, everything)
        # старые × новые
        block_old = self.client.matrix(old, new_coords) if old else None
        for i in range(n_old):
            assert block_old is not None
            self._dur[i].extend(_conv(block_old.durations[i]))
            self._dist[i].extend(_conv(block_old.distances[i]))
        for i in range(len(new_coords)):
            self._dur.append(array("l", _conv(block_new.durations[i])))
            self._dist.append(array("l", _conv(block_new.distances[i])))
        self._coords = everything
        self._coord_index = coord_index
        self._point_coord = point_coord
        return len(new_coords)

    def _idx(self, point_id: str) -> int:
        try:
            return self._point_coord[point_id]
        except KeyError:
            raise UnknownPointError(point_id) from None

    def travel_sec(self, from_id: str, to_id: str) -> int | None:
        v = self._dur[self._idx(from_id)][self._idx(to_id)]
        return None if v == UNREACHABLE else v

    def distance_m(self, from_id: str, to_id: str) -> int | None:
        v = self._dist[self._idx(from_id)][self._idx(to_id)]
        return None if v == UNREACHABLE else v

    def snapshot(self) -> TravelMatrix:
        ids = self.point_ids
        idx = [self._point_coord[p] for p in ids]
        durs = [[self._dur[i][j] for j in idx] for i in idx]
        dists = [[self._dist[i][j] for j in idx] for i in idx]
        return TravelMatrix(self.matrix_id, ids, durs, dists)


def _conv(row: list[int | None]) -> list[int]:
    return [UNREACHABLE if v is None else v for v in row]
