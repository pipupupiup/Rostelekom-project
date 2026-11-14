"""Доменные сущности ADO.

Внутреннее представление для оптимизатора: неизменяемые dataclass'ы с временем в секундах
(см. `timebase.py`). Схемы API (pydantic, точные имена полей `api_spec.yaml`) живут отдельно в
`ado.api.schemas` и преобразуются в домен модулем ADO-01 (`ado.data`).

Соответствие сущностей схемам спецификации — docs/ADO_CONTRACTS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ado.domain.enums import (
    AlertAction,
    AlertStatus,
    AlertType,
    BuildingType,
    ConstraintCode,
    DeadlineKind,
    NodeType,
    PgpAction,
    Profile,
    RouteStatus,
    StopStatus,
    StopType,
    Technology,
    UnassignedAction,
    UnassignedReasonCode,
    WorkTypeCode,
)
from ado.domain.equipment import EquipmentInventory
from ado.domain.timebase import Seconds

REPEAT_WEIGHT = 2
"""Вес повторного обращения в оценке (п. 5 спецификации)."""


# ---------------------------------------------------------------- справочники


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: Seconds
    end: Seconds

    def contains(self, moment: Seconds) -> bool:
        return self.start <= moment <= self.end


@dataclass(frozen=True, slots=True)
class ServiceCenter:
    service_center_id: str
    name: str
    address: str
    lat: float
    lon: float
    opening: TimeWindow
    """Режим работы (время суток в секундах), в котором доступно пополнение АО."""
    restock_duration: Seconds


@dataclass(frozen=True, slots=True)
class Shift:
    start: Seconds
    end: Seconds
    break_duration: Seconds
    break_window: TimeWindow
    """Интервал, в котором должен **начаться** перерыв (H3)."""


@dataclass(frozen=True, slots=True)
class Employee:
    """Сотрудник (`Staff` в спецификации)."""

    staff_id: str
    service_center_id: str
    profiles: frozenset[Profile]
    technologies: frozenset[Technology]
    height_permit: bool
    shift: Shift
    equipment_start: EquipmentInventory
    equipment_capacity: EquipmentInventory
    display_name: str | None = None
    experience_years: float | None = None


@dataclass(frozen=True, slots=True)
class Building:
    building_id: str
    address: str
    lat: float
    lon: float
    building_type: BuildingType
    overhead_line: bool
    zone_service_center_id: str | None = None


@dataclass(frozen=True, slots=True)
class AccessNode:
    access_node_id: str
    node_type: NodeType
    technology: Technology
    name: str
    lat: float
    lon: float
    building_ids: tuple[str, ...]
    address: str | None = None


@dataclass(frozen=True, slots=True)
class WorkType:
    code: WorkTypeCode
    name: str
    profiles: frozenset[Profile]
    duration: Seconds
    max_start_delay: Seconds | None
    deadline_kind: DeadlineKind
    deadline_value: int | None
    repeat_deadline_value: int | None
    pgp_relevant: bool


@dataclass(frozen=True, slots=True)
class ReferenceData:
    """Справочники прогона (`PUT /reference`)."""

    service_centers: dict[str, ServiceCenter]
    employees: dict[str, Employee]
    access_nodes: dict[str, AccessNode]
    buildings: dict[str, Building]
    work_types: dict[WorkTypeCode, WorkType]


# ---------------------------------------------------------------- наряды


@dataclass(frozen=True, slots=True)
class Order:
    """Наряд. Все моменты — секунды от полуночи дня плана.

    `latest_start` и `deadline` приходят уже рассчитанными (спецификация, схема `Order`);
    сервис их не пересчитывает (решение D10).
    """

    order_id: str
    work_type_code: WorkTypeCode
    technology: Technology
    building_id: str
    address: str
    lat: float
    lon: float
    building_type: BuildingType
    requires_height: bool
    access_node_id: str
    registered_at: Seconds
    slot: TimeWindow
    latest_start: Seconds
    deadline: Seconds
    required_equipment: EquipmentInventory
    is_repeat: bool
    previous_order_id: str | None = None

    @property
    def weight(self) -> int:
        return REPEAT_WEIGHT if self.is_repeat else 1

    @property
    def start_window(self) -> TimeWindow:
        """Допустимое окно начала визита (H4): `[slot.start, latest_start]`."""
        return TimeWindow(self.slot.start, self.latest_start)


@dataclass(frozen=True, slots=True)
class DayInstance:
    """Пул нарядов дня (`PUT /days/{date}`) + справочники и матрица."""

    date: str
    now: Seconds
    orders: dict[str, Order]
    absent_staff_ids: frozenset[str]
    reference: ReferenceData

    @property
    def active_employees(self) -> list[Employee]:
        return [e for sid, e in self.reference.employees.items() if sid not in self.absent_staff_ids]


# ---------------------------------------------------------------- план


@dataclass(frozen=True, slots=True)
class RouteStop:
    """Остановка маршрута. `seq` не хранится: это позиция в `Route.stops` + 1."""

    type: StopType
    location_id: str
    arrival: Seconds
    start: Seconds
    end: Seconds
    order_id: str | None = None
    travel_sec: Seconds = 0
    distance_m: int = 0
    equipment_after: EquipmentInventory | None = None
    explanation: str | None = None

    def status_at(self, now: Seconds) -> StopStatus:
        if self.end <= now and self.start < now:
            return StopStatus.DONE
        if self.start < now:
            return StopStatus.IN_PROGRESS
        return StopStatus.PLANNED

    def is_started(self, now: Seconds) -> bool:
        """Правило заморозки (п. 3 спецификации): `start < t` — остановка начата."""
        return self.start < now


@dataclass(slots=True)
class Route:
    staff_id: str
    service_center_id: str
    stops: list[RouteStop] = field(default_factory=list)
    status: RouteStatus = RouteStatus.ACTIVE
    unavailable_from: Seconds | None = None
    unavailable_until: Seconds | None = None
    """`None` при `UNAVAILABLE` — до конца смены."""

    @property
    def visit_order_ids(self) -> list[str]:
        return [s.order_id for s in self.stops if s.type is StopType.VISIT and s.order_id]


@dataclass(frozen=True, slots=True)
class UnassignedReason:
    """`UnassignedOrder` спецификации."""

    order_id: str
    code: UnassignedReasonCode
    text: str
    suggested_action: UnassignedAction
    proposed_slot: TimeWindow | None = None
    since: Seconds | None = None


@dataclass(slots=True)
class PgpGroup:
    group_id: str
    access_node_id: str
    order_ids: list[str]
    detected_at: Seconds
    """Не меняется при пополнении группы."""
    action: PgpAction = PgpAction.TRANSFER_TO_STATION
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class CancelledOrder:
    order_id: str
    cancelled_at: Seconds
    event_id: str | None = None


@dataclass(slots=True)
class Alert:
    """Предупреждение диспетчеру. Список алертов дня только пополняется."""

    alert_id: str
    created_at: Seconds
    type: AlertType
    status: AlertStatus
    suggested_action: AlertAction
    message: str
    order_id: str | None = None
    group_id: str | None = None
    staff_id: str | None = None
    forecast_start: Seconds | None = None
    lateness_min: int | None = None
    suggested_staff_id: str | None = None
    proposed_slot: TimeWindow | None = None
    applied: bool = False
    resolved_at: Seconds | None = None


@dataclass(slots=True)
class Plan:
    """Версия плана дня. H9: каждый наряд ровно в одном из routes / unassigned / pgp / cancelled."""

    date: str
    plan_version: int
    as_of: Seconds
    routes: dict[str, Route]
    unassigned: dict[str, UnassignedReason] = field(default_factory=dict)
    pgp_groups: list[PgpGroup] = field(default_factory=list)
    cancelled: dict[str, CancelledOrder] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    based_on_event_id: str | None = None
    computation_ms: int | None = None

    def assigned_order_ids(self) -> dict[str, str]:
        """order_id → staff_id для нарядов в маршрутах."""
        return {oid: sid for sid, route in self.routes.items() for oid in route.visit_order_ids}


# ---------------------------------------------------------------- результаты модулей


@dataclass(frozen=True, slots=True)
class Violation:
    """Нарушение жёсткого ограничения, найденное симуляцией или валидатором."""

    code: ConstraintCode
    message: str
    stop_index: int | None = None
    order_id: str | None = None
    unassigned_hint: UnassignedReasonCode | None = None
    """Какой причине нераспределения соответствует нарушение (для ADO-08/09)."""


@dataclass(frozen=True, slots=True)
class VisitItem:
    """Элемент последовательности маршрута: визит по наряду."""

    order_id: str


@dataclass(frozen=True, slots=True)
class RestockItem:
    """Элемент последовательности маршрута: пополнение АО в СЦ."""

    service_center_id: str


PlanItem = VisitItem | RestockItem
"""Перерыв (`BREAK`) в последовательность не входит — его размещает симулятор (D12)."""


@dataclass(frozen=True, slots=True)
class RouteDraft:
    """Вход симуляции (ADO-07).

    * `frozen_prefix` — уже начатые остановки (H10): не пересчитываются, по ним
      восстанавливаются позиция, время, остаток АО и признак перерыва;
    * `items` — что выполнить дальше, по порядку;
    * `earliest_departure` — `now`: новое движение не раньше `max(end последней, now)`;
    * `resume_from_depot_at` — после `STAFF_UNAVAILABLE.until` сотрудник продолжает из своего СЦ;
    * `blocked` — периоды недоступности: в них не может начинаться ни одна остановка;
    * `not_before` — визит по наряду из `NEW_ORDER` не раньше `timestamp` события;
    * `end_at_depot` — завершать ли маршрут `DEPOT_END` (у выбывшего до конца смены — нет);
    * `require_break` — нужен ли обеденный перерыв (A9).
    """

    staff_id: str
    items: tuple[PlanItem, ...] = ()
    frozen_prefix: tuple[RouteStop, ...] = ()
    earliest_departure: Seconds | None = None
    resume_from_depot_at: Seconds | None = None
    blocked: tuple[TimeWindow, ...] = ()
    not_before: tuple[tuple[str, Seconds], ...] = ()
    end_at_depot: bool = True
    require_break: bool = True
    """`False` — у выбывшего по событию сотрудника перерыв не требуется (A9)."""
    break_margin: Seconds = 3600
    """Запас до конца окна перерыва (D21): перерыв не откладывается за остановку, после которой до
    конца окна остаётся меньше `break_margin` — иначе задержка на объекте сорвёт обед. 0 — «лениво»."""


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Результат `simulate_route()` (ADO-07) — единственный конструктор расписания маршрута."""

    feasible: bool
    stops: tuple[RouteStop, ...] = ()
    violations: tuple[Violation, ...] = ()
    travel_sec: Seconds = 0
    distance_m: int = 0
    work_sec: Seconds = 0
    wait_sec: Seconds = 0
    deadline_misses: tuple[str, ...] = ()
    """Визиты, которые начинаются в слот, но заканчиваются позже `deadline` (не H-нарушение)."""

    @property
    def violation(self) -> Violation | None:
        return self.violations[0] if self.violations else None


@dataclass(frozen=True, slots=True)
class InsertionOption:
    """Допустимая вставка наряда в маршрут сотрудника (ADO-08)."""

    order_id: str
    staff_id: str
    position: int
    """Индекс в последовательности визитов/служебных остановок, куда вставляется наряд."""
    delta_travel_sec: Seconds
    with_restock: bool
    simulation: SimulationResult
    score: float
    """Чем меньше, тем лучше: прирост времени в пути, с (ADO-08; согласовано с порядком целей D7)."""
    restock_service_center_id: str | None = None
    items: tuple[PlanItem, ...] = ()
    """Новая последовательность маршрута после вставки."""


@dataclass(frozen=True, slots=True)
class PlanMetrics:
    """Оценка плана (ADO-11). Сравнение — только через `ado.optimization.evaluation.compare_plans`.

    Внутренняя оценка команды, не официальная формула организаторов: валидатор организаторов
    пересчитывает метрики по своей матрице и правилам.
    """

    weighted_success: int
    """Σ весов успешных нарядов (визит в окне начала, завершён до deadline, без нарушений)."""
    success_count: int
    orders_total: int
    """Нарядов в оценке: все наряды дня, кроме отменённых и переданных по ПГП."""
    reschedules: int
    travel_sec_total: Seconds
    utilization: dict[str, float]
    utilization_cv: float
    hard_violations: int
    weighted_total: int = 0
    slot_ok_count: int = 0
    deadline_ok_count: int = 0
    visits_planned: int = 0
    unassigned_count: int = 0
    unassigned_by_reason: dict[str, int] = field(default_factory=dict)
    pgp_orders: int = 0
    cancelled: int = 0
    distance_m_total: int = 0

    @property
    def hard_feasible(self) -> bool:
        return self.hard_violations == 0

    @property
    def weighted_success_share(self) -> float:
        return self.weighted_success / self.weighted_total if self.weighted_total else 1.0

    @property
    def slot_share(self) -> float:
        return self.slot_ok_count / self.orders_total if self.orders_total else 1.0

    @property
    def deadline_share(self) -> float:
        return self.deadline_ok_count / self.orders_total if self.orders_total else 1.0

    def key(self) -> tuple[int, int, int, Seconds, float]:
        """Больше — лучше: допустимость, успехи, −переносы, −дорога, −CV загрузки."""
        return (
            -self.hard_violations,
            self.weighted_success,
            -self.reschedules,
            -self.travel_sec_total,
            -self.utilization_cv,
        )


@dataclass(frozen=True, slots=True)
class RiskItem:
    """Наряд с риском опоздания (ADO-12)."""

    order_id: str
    staff_id: str | None
    forecast_start: Seconds
    latest_start: Seconds

    @property
    def slack(self) -> Seconds:
        """Slack = последний допустимый момент начала − прогноз начала."""
        return self.latest_start - self.forecast_start
