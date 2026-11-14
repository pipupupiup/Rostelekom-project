"""ADO-05 — приоритет рассмотрения нарядов.

Приоритет определяет **очередь** обработки, а не ценность выполнения: ценность наряда в целевой
функции — только его вес (1 или 2 для повторного, ADO-11). Оптимизатор вправе не назначить
наряд с высоким приоритетом, если это ухудшает план.

Формула (все компоненты в [0, 1], веса — `PriorityConfig`):

    priority = w_repeat·repeat + w_scarcity·scarcity + w_window·tightness
             + w_urgency·urgency + w_duration·duration_share

* `repeat` = 1 для повторного обращения;
* `scarcity` = 1 / n_eligible (n — число допущенных сотрудников, ADO-04);
* `tightness` = 1 − min(W, W_cap) / W_cap, W — оставшаяся ширина окна начала
  `[max(slot.start, now), latest_start]`; инсталляция (10 мин) ≈ 0,92, ремонт (120 мин) = 0;
* `urgency` = 1 − min(T, T_cap) / T_cap, T = latest_start − now — сколько осталось до
  последнего допустимого начала;
* `duration_share` = норматив / D_cap (длинные работы труднее вставить).

Наряды без допущенных исполнителей идут в конец (их ждёт `unassigned`). Равные приоритеты
упорядочиваются детерминированно: `(−priority, slot.start, order_id)`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ado.domain.models import Order, ReferenceData
from ado.domain.timebase import Seconds


@dataclass(frozen=True, slots=True)
class PriorityConfig:
    w_repeat: float = 1.0
    w_scarcity: float = 2.0
    w_window: float = 1.5
    w_urgency: float = 1.0
    w_duration: float = 0.5
    window_cap_sec: Seconds = 2 * 3600
    urgency_cap_sec: Seconds = 12 * 3600
    duration_cap_sec: Seconds = 3 * 3600


@dataclass(frozen=True, slots=True)
class PriorityScore:
    order_id: str
    value: float
    components: Mapping[str, float]
    n_eligible: int

    def explain(self) -> str:
        parts = ", ".join(f"{k}={v:.2f}" for k, v in self.components.items())
        return f"приоритет {self.value:.2f} ({parts}; допущено {self.n_eligible})"


def calculate_order_priority(
    order: Order,
    n_eligible: int,
    duration: Seconds,
    now: Seconds,
    config: PriorityConfig = PriorityConfig(),
) -> PriorityScore:
    c = config
    window = max(0, order.latest_start - max(order.slot.start, now))
    to_latest = max(0, order.latest_start - now)
    comps = {
        "repeat": 1.0 if order.is_repeat else 0.0,
        "scarcity": 1.0 / n_eligible if n_eligible > 0 else 0.0,
        "tightness": 1.0 - min(window, c.window_cap_sec) / c.window_cap_sec,
        "urgency": 1.0 - min(to_latest, c.urgency_cap_sec) / c.urgency_cap_sec,
        "duration": min(duration, c.duration_cap_sec) / c.duration_cap_sec,
    }
    value = (
        c.w_repeat * comps["repeat"]
        + c.w_scarcity * comps["scarcity"]
        + c.w_window * comps["tightness"]
        + c.w_urgency * comps["urgency"]
        + c.w_duration * comps["duration"]
    )
    if n_eligible == 0:
        value = float("-inf")
    return PriorityScore(order.order_id, value, comps, n_eligible)


def prioritize_orders(
    orders: Sequence[Order],
    eligibility: Mapping[str, Sequence[str]],
    now: Seconds,
    reference: ReferenceData,
    config: PriorityConfig = PriorityConfig(),
) -> list[PriorityScore]:
    scores = [
        calculate_order_priority(
            o,
            len(eligibility.get(o.order_id, ())),
            reference.work_types[o.work_type_code].duration,
            now,
            config,
        )
        for o in orders
    ]
    slot_start = {o.order_id: o.slot.start for o in orders}
    return sorted(scores, key=lambda s: (-s.value, slot_start[s.order_id], s.order_id))


class OrderPrioritizer:
    """Реализация протокола `OrderPrioritizer`."""

    def __init__(self, reference: ReferenceData, config: PriorityConfig = PriorityConfig()) -> None:
        self.reference = reference
        self.config = config

    def prioritize(
        self, orders: Sequence[Order], eligibility: Mapping[str, Sequence[str]], now: Seconds
    ) -> list[str]:
        return [s.order_id for s in prioritize_orders(orders, eligibility, now, self.reference, self.config)]
