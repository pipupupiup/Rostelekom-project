"""События в течение смены (схемы `*Event` спецификации). Время — секунды от полуночи дня."""

from __future__ import annotations

from dataclasses import dataclass

from ado.domain.enums import CancelReason, EventType, TransferTarget, UnavailableReason
from ado.domain.models import Order
from ado.domain.timebase import Seconds


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    timestamp: Seconds

    @property
    def type(self) -> EventType:  # переопределяется в подклассах
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class NewOrderEvent(Event):
    order: Order

    @property
    def type(self) -> EventType:
        return EventType.NEW_ORDER


@dataclass(frozen=True, slots=True)
class OrderCancelledEvent(Event):
    order_id: str
    reason: CancelReason

    @property
    def type(self) -> EventType:
        return EventType.ORDER_CANCELLED


@dataclass(frozen=True, slots=True)
class VisitDelayedEvent(Event):
    """Визит уже начат; закончится в `expected_end` (позже планового)."""

    staff_id: str
    order_id: str
    expected_end: Seconds

    @property
    def type(self) -> EventType:
        return EventType.VISIT_DELAYED


@dataclass(frozen=True, slots=True)
class OrderTransferredEvent(Event):
    """Визит начат; повреждение вне зоны 3ЛТП, работы закончились в `actual_end`."""

    staff_id: str
    order_id: str
    actual_end: Seconds
    transferred_to: TransferTarget

    @property
    def type(self) -> EventType:
        return EventType.ORDER_TRANSFERRED


@dataclass(frozen=True, slots=True)
class StaffUnavailableEvent(Event):
    """Недоступен с `timestamp` до `until` (`None` — до конца смены); после — продолжает из своего СЦ."""

    staff_id: str
    reason: UnavailableReason
    until: Seconds | None = None

    @property
    def type(self) -> EventType:
        return EventType.STAFF_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class TimeTickEvent(Event):
    @property
    def type(self) -> EventType:
        return EventType.TIME_TICK


@dataclass(frozen=True, slots=True)
class ScenarioTrigger:
    """Триггер сценария (`ScenarioTrigger`): событие относительно начала визита в *нашем* плане.

    Разворачивает оркестратор: `timestamp = VISIT.start + fire_after_start`.
    """

    trigger_id: str
    type: EventType
    order_id: str
    fire_after_start: Seconds
    extra_duration: Seconds | None = None
    transferred_to: TransferTarget | None = None


AnyEvent = (
    NewOrderEvent
    | OrderCancelledEvent
    | VisitDelayedEvent
    | OrderTransferredEvent
    | StaffUnavailableEvent
    | TimeTickEvent
)
