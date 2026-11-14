"""Представление времени.

Решение D3 (docs/ADO_DECISIONS.md): внутри оптимизатора время — целое число **секунд от местной
полуночи даты плана** (часовой пояс дня, по умолчанию UTC+05:00). Секунды, а не минуты, потому что
матрица задана в секундах, а допуск валидатора — 1 с. Моменты до полуночи (например,
`registered_at` прошлых дней) дают отрицательные значения — это допустимо.

На границах системы (API) — только timezone-aware `datetime`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

Seconds = int
"""Модельное время или длительность в секундах."""

EKB_TZ = timezone(timedelta(hours=5))
SECONDS_PER_MINUTE = 60
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True, slots=True)
class TimeBase:
    """Отсчёт времени для одного рабочего дня."""

    day: date
    tz: timezone = EKB_TZ

    @property
    def origin(self) -> datetime:
        return datetime.combine(self.day, time(0, 0), tzinfo=self.tz)

    def to_seconds(self, moment: datetime) -> Seconds:
        """Момент → секунды от полуночи дня. Требует timezone-aware datetime."""
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError(f"ожидается datetime со смещением, получено {moment!r}")
        delta = moment - self.origin
        return int(delta.total_seconds())  # точность — секунды (п. 2 спецификации)

    def to_datetime(self, seconds: Seconds) -> datetime:
        """Секунды от полуночи дня → datetime в часовом поясе дня."""
        return self.origin + timedelta(seconds=seconds)

    def format(self, seconds: Seconds) -> str:
        """ISO 8601 со смещением, без долей секунды: `2026-10-26T09:00:00+05:00`."""
        return self.to_datetime(seconds).isoformat(timespec="seconds")


def parse_local_time(value: str) -> Seconds:
    """`HH:MM` (LocalTime из спецификации) → секунды от полуночи."""
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() and len(p) == 2 for p in parts):
        raise ValueError(f"ожидается HH:MM, получено {value!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        raise ValueError(f"некорректное время суток {value!r}")
    return hours * 3600 + minutes * SECONDS_PER_MINUTE


def minutes(value: int) -> Seconds:
    """Длительность в минутах (`*_min` из справочников) → секунды."""
    return value * SECONDS_PER_MINUTE
