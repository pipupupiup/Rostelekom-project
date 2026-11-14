"""ADO-12 — раннее выявление рисков опоздания.

`forecast_route()` пересчитывает время остановок маршрута **без вмешательства**: та же
последовательность, начатые остановки — как есть (с учётом фактического окончания из событий),
дальше `arrival = end предыдущей + дорога по матрице`, `start = max(arrival, slot.start)`.
Окна не обрезают прогноз — так видно, насколько сотрудник опоздает. Фиксированных задержек нет:
только текущее состояние и единая матрица.

`predict_delays()` для каждого предстоящего визита считает `slack = latest_start − forecast_start`
(для инсталляции `latest_start = slot.start + 10 мин`, для остальных — `slot.end`) и уровень:
* `VIOLATION` — slack < 0: нарушение гарантировано по текущему прогнозу;
* `HIGH` — 0 ≤ slack < `high_slack_sec` и прогноз начала позже планового: повышенный риск;
плюс проверки процесса: успеваем ли предупредить о переносе (≥ 30 мин до слота) и не пора ли
звонить абоненту (окно 60–30 мин до слота).

Слот абонента автоматически не меняется: модуль только предлагает действие (`REASSIGN` /
`RESCHEDULE`) и передаёт проблемные наряды в ADO-13. Повторные расчёты не дублируют
предупреждения: `DelayPredictor` помнит выданные `(order_id, level)` и закрывает исчезнувшие.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ado.domain.contracts import TravelTimeProvider
from ado.domain.enums import AlertAction, StopType
from ado.domain.models import DayInstance, Plan, Route
from ado.domain.timebase import Seconds


class RiskLevel(StrEnum):
    VIOLATION = "VIOLATION"
    HIGH = "HIGH"


@dataclass(frozen=True, slots=True)
class RiskConfig:
    high_slack_sec: Seconds = 15 * 60
    notice_before_slot_sec: Seconds = 30 * 60
    """Перенос инициируется не позднее чем за 30 мин до начала слота (ТЗ п. 2.2)."""
    call_from_sec: Seconds = 60 * 60
    call_to_sec: Seconds = 30 * 60
    """Звонок абоненту за 30–60 мин до начала слота."""


@dataclass(frozen=True, slots=True)
class Forecast:
    order_id: str
    staff_id: str
    planned_start: Seconds
    forecast_start: Seconds
    latest_start: Seconds

    @property
    def slack(self) -> Seconds:
        return self.latest_start - self.forecast_start


@dataclass(frozen=True, slots=True)
class RiskAlert:
    order_id: str
    staff_id: str
    level: RiskLevel
    forecast_start: Seconds
    latest_start: Seconds
    slot_start: Seconds
    now: Seconds
    suggested_action: AlertAction
    reason: str
    notice_deadline: Seconds
    """Последний момент своевременного предупреждения о переносе (slot.start − 30 мин)."""

    @property
    def slack(self) -> Seconds:
        return self.latest_start - self.forecast_start

    @property
    def lateness_min(self) -> int:
        return max(0, (self.forecast_start - self.latest_start + 59) // 60)

    @property
    def notice_possible(self) -> bool:
        """Ещё можно предупредить абонента о переносе вовремя (≥ 30 мин до слота)."""
        return self.now <= self.notice_deadline


DurationFn = Callable[[str, str], Seconds]
"""(order_id, staff_id) → ожидаемая длительность визита, ≥ норматива (прогноз по истории, D39)."""


def forecast_route(
    route: Route,
    day: DayInstance,
    travel: TravelTimeProvider,
    now: Seconds,
    end_overrides: Mapping[str, Seconds] | None = None,
    durations: DurationFn | None = None,
) -> list[Forecast]:
    end_overrides = end_overrides or {}
    factor_at = getattr(travel, "factor_at", None)
    out: list[Forecast] = []
    prev_loc: str | None = None
    prev_end: Seconds | None = None
    for s in route.stops:
        if s.start < now or prev_end is None:
            end = end_overrides.get(s.order_id or "", s.end) if s.type is StopType.VISIT else s.end
            if (
                durations is not None
                and s.type is StopType.VISIT
                and s.order_id
                and s.order_id not in end_overrides
                and s.start < now < s.end + 1
            ):  # идёт сейчас: по истории сотрудник может закончить позже норматива
                end = max(end, s.start + durations(s.order_id, route.staff_id))
            if s.type is StopType.VISIT and s.order_id and s.start >= now:
                o = day.orders[s.order_id]
                out.append(Forecast(s.order_id, route.staff_id, s.start, s.start, o.latest_start))
            prev_loc, prev_end = s.location_id, end
            continue
        tt = travel.travel_sec(prev_loc, s.location_id) if prev_loc != s.location_id else 0
        if tt and factor_at is not None:  # загруженность дорог по часу отправления (D38)
            tt = math.ceil(tt * factor_at(prev_end))
        arrival = prev_end + (tt or 0)
        if s.type is StopType.VISIT and s.order_id in day.orders:
            o = day.orders[s.order_id]
            start = max(arrival, o.slot.start)
            dur = day.reference.work_types[o.work_type_code].duration
            if durations is not None:
                dur = max(dur, durations(o.order_id, route.staff_id))
            out.append(Forecast(o.order_id, route.staff_id, s.start, start, o.latest_start))
            end = start + dur
        elif s.type is StopType.BREAK:
            start = max(prev_end, s.start)
            end = start + (s.end - s.start)
        elif s.type is StopType.RESTOCK:
            sc = day.reference.service_centers[s.location_id]
            start = max(arrival, sc.opening.start)
            end = start + (s.end - s.start)
        else:
            end = arrival
        prev_loc, prev_end = s.location_id, end
    return out


def predict_delays(
    plan: Plan,
    day: DayInstance,
    travel: TravelTimeProvider,
    now: Seconds,
    config: RiskConfig = RiskConfig(),
    end_overrides: Mapping[str, Seconds] | None = None,
    durations: DurationFn | None = None,
) -> list[RiskAlert]:
    risks: list[RiskAlert] = []
    for sid in sorted(plan.routes):
        for f in forecast_route(plan.routes[sid], day, travel, now, end_overrides, durations):
            if f.forecast_start < now and f.planned_start < now:
                continue  # уже начат
            o = day.orders[f.order_id]
            if f.slack < 0:
                level = RiskLevel.VIOLATION
            elif f.slack < config.high_slack_sec and f.forecast_start > f.planned_start:
                # повышенный риск — только при отклонении от плана: узкое окно инсталляции (10 мин)
                # само по себе не риск, план его соблюдает
                level = RiskLevel.HIGH
            else:
                continue
            notice_ok = now <= o.slot.start - config.notice_before_slot_sec
            action = AlertAction.REASSIGN if level is RiskLevel.VIOLATION else AlertAction.NONE
            late = max(0, (f.forecast_start - f.latest_start + 59) // 60)
            reason = (
                f"прогноз начала {f.forecast_start // 3600:02d}:{f.forecast_start % 3600 // 60:02d}, "
                + (f"опоздание {late} мин" if level is RiskLevel.VIOLATION else f"запас {f.slack // 60} мин")
                + ("" if notice_ok else "; срок предупреждения о переносе (−30 мин) уже прошёл")
            )
            risks.append(
                RiskAlert(
                    f.order_id,
                    sid,
                    level,
                    f.forecast_start,
                    f.latest_start,
                    o.slot.start,
                    now,
                    action,
                    reason,
                    o.slot.start - config.notice_before_slot_sec,
                )
            )
    risks.sort(key=lambda r: (r.level is not RiskLevel.VIOLATION, r.slack, r.order_id))
    return risks


def call_due(plan: Plan, day: DayInstance, now: Seconds, config: RiskConfig = RiskConfig()) -> list[str]:
    """Визиты, по которым сейчас окно звонка абоненту (за 60–30 мин до слота)."""
    out = []
    for r in plan.routes.values():
        for s in r.stops:
            if s.type is StopType.VISIT and s.order_id in day.orders and s.start >= now:
                slot = day.orders[s.order_id].slot.start
                if slot - config.call_from_sec <= now <= slot - config.call_to_sec:
                    out.append(s.order_id)
    return sorted(out)


@dataclass(slots=True)
class DelayPredictor:
    """Помнит выданные предупреждения: повторный расчёт без изменения состояния ничего не добавляет."""

    config: RiskConfig = field(default_factory=RiskConfig)
    active: dict[tuple[str, RiskLevel], RiskAlert] = field(default_factory=dict)

    def update(
        self,
        plan: Plan,
        day: DayInstance,
        travel: TravelTimeProvider,
        now: Seconds,
        end_overrides: Mapping[str, Seconds] | None = None,
    ) -> tuple[list[RiskAlert], list[RiskAlert]]:
        """→ (новые риски, исчезнувшие риски)."""
        current = {
            (r.order_id, r.level): r
            for r in predict_delays(plan, day, travel, now, self.config, end_overrides)
        }
        new = [r for k, r in current.items() if k not in self.active]
        gone = [r for k, r in self.active.items() if k not in current]
        self.active = current
        return sorted(new, key=lambda r: r.order_id), sorted(gone, key=lambda r: r.order_id)
