"""Производственный календарь и расчёт контрольных сроков (ADO-01, п. 5).

Сроки в нарядах организаторов уже рассчитаны (D10), поэтому вычисление здесь используется для
**сверки** и для нарядов, у которых срок нужно получить самостоятельно (сценарный анализ,
синтетика). Источник истины при расхождении — `order.deadline` из входных данных.

Правило «N рабочих дней» (A5): момент регистрации сдвигается на N рабочих дней вперёд с
сохранением времени суток; дни, на которые сдвигаемся, должны быть рабочими. Проверено на примере
спецификации: 2026-10-22T14:12 + 3 р. д. = 2026-10-27T14:12 (пт 23, пн 26, вт 27).

Праздники РФ 2026 (A6) заданы по постановлению о переносе выходных; список можно заменить через
`ProductionCalendar(holidays=..., working_weekends=...)`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ado.domain.enums import DeadlineKind
from ado.domain.models import WorkType

RU_HOLIDAYS_2026: frozenset[date] = frozenset(
    [date(2026, 1, d) for d in range(1, 12)]  # 1–8 янв + перенос 3, 4 янв → 9 янв; 10–11 — выходные
    + [
        date(2026, 2, 23),
        date(2026, 3, 9),  # 8 марта — воскресенье
        date(2026, 5, 1),
        date(2026, 5, 11),  # 9 мая — суббота
        date(2026, 6, 12),
        date(2026, 11, 4),
        date(2026, 12, 31),  # перенос с 4 января
    ]
)


@dataclass(frozen=True, slots=True)
class ProductionCalendar:
    holidays: frozenset[date] = field(default=RU_HOLIDAYS_2026)
    working_weekends: frozenset[date] = field(default_factory=frozenset)

    @classmethod
    def from_dates(
        cls, holidays: Iterable[date], working_weekends: Iterable[date] = ()
    ) -> ProductionCalendar:
        return cls(frozenset(holidays), frozenset(working_weekends))

    def is_working_day(self, d: date) -> bool:
        if d in self.working_weekends:
            return True
        if d in self.holidays:
            return False
        return d.weekday() < 5

    def add_working_days(self, moment: datetime, days: int) -> datetime:
        if days < 0:
            raise ValueError("число рабочих дней не может быть отрицательным")
        current = moment
        remaining = days
        while remaining > 0:
            current += timedelta(days=1)
            if self.is_working_day(current.date()):
                remaining -= 1
        return current

    def next_working_day(self, d: date) -> date:
        current = d + timedelta(days=1)
        while not self.is_working_day(current):
            current += timedelta(days=1)
        return current


DEFAULT_CALENDAR = ProductionCalendar()


def compute_deadline(
    work_type: WorkType,
    registered_at: datetime,
    latest_start: datetime,
    is_repeat: bool,
    calendar: ProductionCalendar = DEFAULT_CALENDAR,
) -> datetime:
    """Контрольный срок по правилам `WorkType` (раздельно: SLOT / HOURS / WORKING_DAYS)."""
    if work_type.deadline_kind is DeadlineKind.SLOT:
        return latest_start + timedelta(seconds=work_type.duration)
    value = work_type.deadline_value
    if is_repeat and work_type.repeat_deadline_value is not None:
        value = work_type.repeat_deadline_value
    if value is None:
        raise ValueError(f"у типа работ {work_type.code} не задан deadline_value")
    if work_type.deadline_kind is DeadlineKind.HOURS:
        return registered_at + timedelta(hours=value)
    return calendar.add_working_days(registered_at, value)
