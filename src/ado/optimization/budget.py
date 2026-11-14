"""Бюджет времени расчёта (часть ADO-14).

Здесь только примитив «дедлайн по монотонным часам». Измеряется *реальное* время вычислений
(лимиты 300 с / 10 с), а не модельное — поэтому системные часы тут допустимы. Часы можно
подменить для тестов.
"""

from __future__ import annotations

import time
from collections.abc import Callable

Clock = Callable[[], float]


class StageTimer:
    """Метрики времени по этапам (ADO-14): `with timer.stage("validate"): ...` → `timer.ms`."""

    def __init__(self, clock: Clock = time.perf_counter) -> None:
        self._clock = clock
        self._t0 = clock()
        self.ms: dict[str, int] = {}

    def stage(self, name: str) -> _Stage:
        return _Stage(self, name)

    def total_ms(self) -> int:
        return int((self._clock() - self._t0) * 1000)


class _Stage:
    def __init__(self, timer: StageTimer, name: str) -> None:
        self.timer, self.name = timer, name

    def __enter__(self) -> None:
        self.t0 = self.timer._clock()

    def __exit__(self, *exc: object) -> None:
        dt = int((self.timer._clock() - self.t0) * 1000)
        self.timer.ms[self.name] = self.timer.ms.get(self.name, 0) + dt


class TimeBudget:
    def __init__(self, seconds: float, clock: Clock = time.monotonic, reserve: float = 0.0) -> None:
        """`reserve` — запас на финальную проверку и сериализацию ответа, вычитается из лимита."""
        if seconds < 0:
            raise ValueError("бюджет не может быть отрицательным")
        if reserve < 0 or (seconds > 0 and reserve >= seconds):
            raise ValueError("резерв должен быть в [0, seconds)")
        reserve = min(reserve, seconds)  # нулевой бюджет — сразу исчерпан, но вызывающий получает результат
        self._clock = clock
        self._deadline = clock() + seconds - reserve

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def expired(self) -> bool:
        return self._clock() >= self._deadline

    def sub_budget(self, seconds: float) -> TimeBudget:
        """Вложенный бюджет: не больше `seconds` и не дольше родительского."""
        allowed = min(seconds, self.remaining())
        child = TimeBudget.__new__(TimeBudget)
        child._clock = self._clock
        child._deadline = self._clock() + allowed
        return child
