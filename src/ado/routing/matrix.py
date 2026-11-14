"""Матрица времени в пути (ADO-02).

`TravelMatrix` — неизменяемое хранилище N×N длительностей (с) и расстояний (м) с индексом
`point_id → i`. Доступ O(1) без сети: строки хранятся как `array('l')`, отдельное значение
читается быстрее, чем из numpy, а память — 8 Б на ячейку (≈ 25 МБ на 1265² × 2).
Для векторных операций (ADO-06) есть `as_numpy()`.

Недостижимая пара хранится как `UNREACHABLE` (−1); `travel_sec()` возвращает `None` —
вызывающий код обязан считать такую поездку недопустимой (никакой подмены прямой линией).

Основной источник — единая матрица организаторов (`from_api`, решение D2). Матрицы из OSRM
собирает `ado.routing.cache.OsrmMatrixCache`.
"""

from __future__ import annotations

from array import array
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

from ado.data.errors import BusinessValidationError, InputFormatError, Issue, IssueCollector
from ado.domain.timebase import Seconds

UNREACHABLE = -1


class UnknownPointError(KeyError):
    """Точки нет в матрице. Это ошибка данных, а не повод идти в сеть из горячего цикла."""


class TravelMatrix:
    __slots__ = ("_dist", "_dur", "_index", "hourly_factors", "matrix_id", "points")

    def __init__(
        self,
        matrix_id: str,
        points: Sequence[str],
        durations: Sequence[Sequence[int]],
        distances: Sequence[Sequence[int]],
        hourly_factors: Mapping[int, float] | None = None,
    ) -> None:
        n = len(points)
        if len(set(points)) != n:
            raise ValueError("идентификаторы точек должны быть уникальны")
        if len(durations) != n or len(distances) != n:
            raise ValueError("число строк матрицы не совпадает с числом точек")
        self.matrix_id = matrix_id
        self.points: tuple[str, ...] = tuple(points)
        self._index = {p: i for i, p in enumerate(self.points)}
        self._dur = [array("l", row) for row in durations]
        self._dist = [array("l", row) for row in distances]
        if any(len(r) != n for r in self._dur) or any(len(r) != n for r in self._dist):
            raise ValueError("матрица должна быть квадратной")
        self.hourly_factors = dict(hourly_factors or {})

    # ---------------------------------------------------------------- построение
    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> TravelMatrix:
        """Тело `PUT /reference/travel-matrix` (схема `TravelMatrix`) со строгой проверкой."""
        issues = IssueCollector()
        if not isinstance(raw, Mapping):
            raise InputFormatError([Issue("", "ожидается объект TravelMatrix")])
        for key in ("matrix_id", "points", "durations_sec", "distances_m"):
            if key not in raw:
                issues.format(key, "обязательное поле отсутствует")
        issues.raise_format()
        points = raw["points"]
        if not isinstance(points, list) or len(points) < 2 or not all(isinstance(p, str) for p in points):
            raise InputFormatError([Issue("points", "ожидается массив строк, не менее 2")])
        if len(set(points)) != len(points):
            raise InputFormatError([Issue("points", "идентификаторы точек должны быть уникальны")])
        n = len(points)
        for key in ("durations_sec", "distances_m"):
            m = raw[key]
            if not isinstance(m, list) or len(m) != n:
                issues.format(key, f"ожидается {n} строк")
                continue
            for i, row in enumerate(m):
                if not isinstance(row, list) or len(row) != n:
                    issues.format(f"{key}[{i}]", f"ожидается {n} значений")
                    break
                if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in row):
                    issues.format(f"{key}[{i}]", "значения — целые числа ≥ 0")
                    break
        factors: dict[int, float] = {}
        for i, f in enumerate(raw.get("hourly_factors") or []):
            if not isinstance(f, Mapping) or not isinstance(f.get("hour"), int) or not 0 <= f["hour"] <= 23:
                issues.format(f"hourly_factors[{i}].hour", "час 0–23")
                continue
            if not isinstance(f.get("factor"), int | float) or f["factor"] < 0:
                issues.format(f"hourly_factors[{i}].factor", "коэффициент ≥ 0")
                continue
            factors[f["hour"]] = float(f["factor"])
        issues.raise_format()
        matrix_id = raw["matrix_id"] if isinstance(raw["matrix_id"], str) else str(raw["matrix_id"])
        return cls(matrix_id, points, raw["durations_sec"], raw["distances_m"], factors)

    # ---------------------------------------------------------------- доступ
    def index(self, point_id: str) -> int:
        try:
            return self._index[point_id]
        except KeyError:
            raise UnknownPointError(point_id) from None

    def has_point(self, point_id: str) -> bool:
        return point_id in self._index

    def travel_sec(self, from_id: str, to_id: str) -> Seconds | None:
        v = self._dur[self.index(from_id)][self.index(to_id)]
        return None if v == UNREACHABLE else v

    def distance_m(self, from_id: str, to_id: str) -> int | None:
        v = self._dist[self.index(from_id)][self.index(to_id)]
        return None if v == UNREACHABLE else v

    def travel_by_index(self, i: int, j: int) -> int:
        """Сырое значение по индексам (−1 — недостижимо). Для горячих циклов."""
        return self._dur[i][j]

    def __len__(self) -> int:
        return len(self.points)

    def as_numpy(self) -> tuple[np.ndarray, np.ndarray]:
        return np.array(self._dur, dtype=np.int64), np.array(self._dist, dtype=np.int64)

    # ---------------------------------------------------------------- проверки
    def missing(self, point_ids: Iterable[str]) -> list[str]:
        return sorted({p for p in point_ids if p not in self._index})

    def check_covers(self, point_ids: Iterable[str]) -> None:
        """Все СЦ и дома справочника должны быть в матрице (иначе 422)."""
        missing = self.missing(point_ids)
        if missing:
            head = ", ".join(missing[:10]) + (" …" if len(missing) > 10 else "")
            raise BusinessValidationError([Issue("points", f"в матрице нет {len(missing)} точек: {head}")])

    def diagnostics(self) -> list[Issue]:
        """Предупреждения: ненулевая диагональ, недостижимые пары."""
        out: list[Issue] = []
        bad_diag = [
            self.points[i] for i in range(len(self.points)) if self._dur[i][i] not in (0, UNREACHABLE)
        ]
        if bad_diag:
            out.append(Issue("durations_sec", f"ненулевая диагональ у {len(bad_diag)} точек"))
        unreachable = sum(row.count(UNREACHABLE) for row in self._dur)
        if unreachable:
            out.append(Issue("durations_sec", f"{unreachable} недостижимых пар"))
        return out


class MatrixTravelProvider:
    """`TravelTimeProvider` поверх готовой матрицы (основной режим — матрица организаторов)."""

    def __init__(self, matrix: TravelMatrix) -> None:
        self.matrix = matrix
        self.matrix_id = matrix.matrix_id
        # прямые ссылки — горячий путь оптимизатора (миллионы обращений): без лишних вызовов методов
        self._index = matrix._index
        self._dur = matrix._dur
        self._dist = matrix._dist

    def travel_sec(self, from_id: str, to_id: str) -> Seconds | None:
        try:
            v = self._dur[self._index[from_id]][self._index[to_id]]
        except KeyError as e:
            raise UnknownPointError(e.args[0]) from None
        return None if v == UNREACHABLE else v

    def distance_m(self, from_id: str, to_id: str) -> int | None:
        try:
            v = self._dist[self._index[from_id]][self._index[to_id]]
        except KeyError as e:
            raise UnknownPointError(e.args[0]) from None
        return None if v == UNREACHABLE else v

    def has_point(self, point_id: str) -> bool:
        return self.matrix.has_point(point_id)


class TrafficTravelProvider(MatrixTravelProvider):
    """Матрица + коэффициенты загруженности по часу отправления (`hourly_factors`, D38).

    `travel_sec` — базовое время (нижняя оценка для отсечений в оптимизаторе); симулятор и прогноз
    берут `factor_at(t)` и умножают поездку, начатую в момент `t`. Коэффициенты < 1 не применяются:
    H6 проверяется по базовой матрице, и более короткая поездка дала бы нарушение.
    """

    def __init__(self, matrix: TravelMatrix, factors: Mapping[int, float] | None = None) -> None:
        super().__init__(matrix)
        src = matrix.hourly_factors if factors is None else factors
        self.factors = [max(1.0, float(src.get(h, 1.0))) for h in range(24)]

    def factor_at(self, t: Seconds) -> float:
        return self.factors[(t // 3600) % 24]
