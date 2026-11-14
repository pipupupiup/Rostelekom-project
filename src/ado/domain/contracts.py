"""Интерфейсы модулей ADO-01…ADO-17.

Здесь только контракты (typing.Protocol). Реализации появляются в пакетах `ado.data`,
`ado.routing`, `ado.optimization`, `ado.events`, `ado.persistence`, `ado.api` по отдельным
заданиям — статус в docs/ADO_TASKS.md. Правила:

* модули обмениваются только доменными объектами из `ado.domain`;
* текущее модельное время всегда передаётся явно (`now`), системные часы не используются;
* проверка жёстких ограничений маршрута — только в `RouteSimulator` (ADO-07); остальные модули
  её вызывают, а не дублируют (правило 9 мастер-промпта);
* ни один метод, вызываемый во внутреннем цикле оптимизации, не обращается к сети или БД.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ado.domain.events import AnyEvent
from ado.domain.models import (
    DayInstance,
    Employee,
    InsertionOption,
    Order,
    PgpGroup,
    Plan,
    PlanMetrics,
    ReferenceData,
    RiskItem,
    RouteDraft,
    SimulationResult,
    UnassignedReason,
)
from ado.domain.timebase import Seconds


class TimeBudgetLike(Protocol):
    """Бюджет времени расчёта (ADO-14). Реализация — `ado.optimization.budget.TimeBudget`."""

    def remaining(self) -> float: ...
    def expired(self) -> bool: ...
    def sub_budget(self, seconds: float) -> TimeBudgetLike: ...


# ADO-01 ------------------------------------------------------------------------------
class DataPreparer(Protocol):
    """Преобразует входные данные API/файлов в домен, проверяет ссылочную целостность."""

    def prepare_reference(self, raw: Mapping[str, Any]) -> ReferenceData: ...
    def prepare_day(self, raw: Mapping[str, Any], reference: ReferenceData) -> DayInstance: ...


# ADO-02 ------------------------------------------------------------------------------
class TravelTimeProvider(Protocol):
    """Время/расстояние между точками (`service_center_id` или `building_id`).

    Основной источник — единая матрица организаторов (решение D2). Вызов — O(1), без сети.
    """

    matrix_id: str

    def travel_sec(self, from_id: str, to_id: str) -> Seconds | None:
        """`None` — дорожной связи нет; такая поездка недопустима. Неизвестная точка — KeyError."""
        ...

    def distance_m(self, from_id: str, to_id: str) -> int | None: ...
    def has_point(self, point_id: str) -> bool: ...


# ADO-03 ------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PgpDetectionResult:
    groups: list[PgpGroup]
    """Все группы дня: ранее выявленные (дополненные) и новые."""
    new_group_ids: list[str]
    grouped_order_ids: frozenset[str]


class PgpDetector(Protocol):
    def detect(
        self,
        orders: Sequence[Order],
        existing: Sequence[PgpGroup],
        started_order_ids: frozenset[str],
        now: Seconds,
        reference: ReferenceData,
    ) -> PgpDetectionResult: ...


# ADO-04 ------------------------------------------------------------------------------
class EligibilityFilter(Protocol):
    """Статические допуски (H1) и грубые фильтры; точная проверка — на этапе вставки."""

    def eligible_staff(self, order: Order, employees: Sequence[Employee]) -> list[str]: ...
    def explain_ineligible(self, order: Order, employees: Sequence[Employee]) -> UnassignedReason | None: ...


# ADO-05 ------------------------------------------------------------------------------
class OrderPrioritizer(Protocol):
    def prioritize(
        self, orders: Sequence[Order], eligibility: Mapping[str, Sequence[str]], now: Seconds
    ) -> list[str]: ...


# ADO-06 ------------------------------------------------------------------------------
class CandidateSearch(Protocol):
    def candidates(self, order: Order, eligible: Sequence[str], plan: Plan, now: Seconds) -> list[str]: ...


# ADO-07 ------------------------------------------------------------------------------
class RouteSimulator(Protocol):
    def simulate(
        self, employee: Employee, draft: RouteDraft, day: DayInstance, travel: TravelTimeProvider
    ) -> SimulationResult: ...


# ADO-08 ------------------------------------------------------------------------------
class InsertionFinder(Protocol):
    def insertions(
        self, order: Order, staff_ids: Sequence[str], plan: Plan, day: DayInstance, now: Seconds
    ) -> list[InsertionOption]: ...


# ADO-09 / ADO-10 ---------------------------------------------------------------------
class InitialOptimizer(Protocol):
    def build(self, day: DayInstance, budget: TimeBudgetLike) -> Plan: ...


class RouteImprover(Protocol):
    def improve(self, plan: Plan, day: DayInstance, now: Seconds, budget: TimeBudgetLike) -> Plan: ...


# ADO-11 ------------------------------------------------------------------------------
class PlanEvaluator(Protocol):
    def evaluate(self, plan: Plan, day: DayInstance, travel: TravelTimeProvider) -> PlanMetrics: ...


class PlanValidator(Protocol):
    """Полная проверка версии плана H1–H10 (использует RouteSimulator для H1–H8)."""

    def validate(self, plan: Plan, previous: Plan | None, day: DayInstance) -> list[Any]: ...


# ADO-12 ------------------------------------------------------------------------------
class DelayPredictor(Protocol):
    def risks(self, plan: Plan, day: DayInstance, now: Seconds) -> list[RiskItem]: ...


# ADO-13 ------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReplanResult:
    plan: Plan
    changed_staff_ids: list[str]
    new_alert_ids: list[str]
    message: str | None = None


class Reoptimizer(Protocol):
    def apply(
        self, event: AnyEvent, plan: Plan, day: DayInstance, budget: TimeBudgetLike
    ) -> ReplanResult: ...


# ADO-15 ------------------------------------------------------------------------------
class PlanRepository(Protocol):
    """Хранилище версий. Вызывается вне горячего пути (после ответа или асинхронно)."""

    def save_version(self, plan: Plan, expected_previous_version: int | None) -> None: ...
    def current(self, date: str) -> Plan | None: ...
