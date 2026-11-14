"""ADO-04 — фильтр потенциальных исполнителей.

Две ступени (п. 10 задания):

* **статическая пригодность** — профиль, технология, допуск к высоте (H1). Не зависит от
  времени и плана, кешируется по ключу `(тип работ, технология, высота)`;
* **динамическая возможность** — выход на смену, недоступность по событиям, грубое окно
  «смена ∩ слот» (с учётом дороги из/в СЦ, если передан провайдер времени), ёмкость рюкзака.

Точную вставку в расписание проверяют ADO-07/ADO-08. Нехватка АО в рюкзаке **не исключает**
сотрудника, если `equipment_capacity` покрывает потребность: кандидат помечается
`needs_restock = True` (пополнение в СЦ). Исключаем только когда даже полный рюкзак не покрывает
наряд.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ado.domain.contracts import TravelTimeProvider
from ado.domain.enums import UnassignedAction, UnassignedReasonCode
from ado.domain.models import Employee, Order, ReferenceData, TimeWindow, UnassignedReason
from ado.domain.timebase import Seconds


class ExclusionReason(StrEnum):
    PROFILE = "PROFILE"
    TECHNOLOGY = "TECHNOLOGY"
    HEIGHT = "HEIGHT"
    ABSENT = "ABSENT"
    UNAVAILABLE = "UNAVAILABLE"
    SHIFT = "SHIFT"
    EQUIPMENT_CAPACITY = "EQUIPMENT_CAPACITY"


STATIC_REASONS = frozenset({ExclusionReason.PROFILE, ExclusionReason.TECHNOLOGY, ExclusionReason.HEIGHT})

REASON_TEXT = {
    ExclusionReason.PROFILE: "профиль",
    ExclusionReason.TECHNOLOGY: "технология",
    ExclusionReason.HEIGHT: "допуск к работам на высоте",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    staff_id: str
    needs_restock: bool = False


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    order_id: str
    eligible: tuple[Candidate, ...]
    excluded: Mapping[str, tuple[ExclusionReason, ...]]

    @property
    def staff_ids(self) -> list[str]:
        return [c.staff_id for c in self.eligible]

    @property
    def statically_qualified(self) -> list[str]:
        out = [c.staff_id for c in self.eligible]
        out += [sid for sid, rs in self.excluded.items() if not STATIC_REASONS & set(rs)]
        return sorted(out)


@dataclass(frozen=True, slots=True)
class AvailabilityState:
    """Динамическое состояние сотрудников на момент проверки."""

    absent: frozenset[str] = frozenset()
    unavailable: Mapping[str, tuple[TimeWindow, ...]] = field(default_factory=dict)
    """Периоды недоступности; конец `None` в событии нужно передать как конец смены."""


def static_reasons(order: Order, employee: Employee, reference: ReferenceData) -> tuple[ExclusionReason, ...]:
    wt = reference.work_types[order.work_type_code]
    reasons = []
    if not (employee.profiles & wt.profiles):
        reasons.append(ExclusionReason.PROFILE)
    if order.technology not in employee.technologies:
        reasons.append(ExclusionReason.TECHNOLOGY)
    if order.requires_height and not employee.height_permit:
        reasons.append(ExclusionReason.HEIGHT)
    return tuple(reasons)


def _subtract(lo: Seconds, hi: Seconds, blocked: Iterable[TimeWindow]) -> bool:
    """Есть ли в [lo, hi] момент вне всех [from, until) недоступности."""
    t = lo
    for w in sorted(blocked, key=lambda w: w.start):
        if w.end <= t:
            continue
        if w.start > t:
            return True
        t = max(t, w.end)
        if t > hi:
            return False
    return t <= hi


class EligibilityIndex:
    def __init__(self, reference: ReferenceData, travel: TravelTimeProvider | None = None) -> None:
        self.reference = reference
        self.travel = travel
        self._static_cache: dict[tuple, frozenset[str]] = {}
        self.static_cache_hits = 0

    def static_qualified(self, order: Order) -> frozenset[str]:
        key = (order.work_type_code, order.technology, order.requires_height)
        cached = self._static_cache.get(key)
        if cached is not None:
            self.static_cache_hits += 1
            return cached
        ids = frozenset(
            sid for sid, e in self.reference.employees.items() if not static_reasons(order, e, self.reference)
        )
        self._static_cache[key] = ids
        return ids

    def _start_bounds(self, order: Order, e: Employee) -> tuple[Seconds, Seconds] | None:
        dur = self.reference.work_types[order.work_type_code].duration
        out = back = 0
        if self.travel is not None:
            t1 = self.travel.travel_sec(e.service_center_id, order.building_id)
            t2 = self.travel.travel_sec(order.building_id, e.service_center_id)
            if t1 is None or t2 is None:
                return None
            out, back = t1, t2
        lo = max(order.slot.start, e.shift.start + out, order.registered_at)
        hi = min(order.latest_start, e.shift.end - dur - back)
        return (lo, hi) if lo <= hi else None

    def find_eligible_staff(
        self,
        order: Order,
        employees: Sequence[Employee],
        state: AvailabilityState | None = None,
    ) -> EligibilityResult:
        state = state or AvailabilityState()
        qualified = self.static_qualified(order)
        need = order.required_equipment
        eligible: list[Candidate] = []
        excluded: dict[str, tuple[ExclusionReason, ...]] = {}
        for e in sorted(employees, key=lambda e: e.staff_id):
            sid = e.staff_id
            known = self.reference.employees.get(sid) == e
            if (sid not in qualified) if known else bool(static_reasons(order, e, self.reference)):
                excluded[sid] = static_reasons(order, e, self.reference)
                continue
            if sid in state.absent:
                excluded[sid] = (ExclusionReason.ABSENT,)
                continue
            reasons: list[ExclusionReason] = []
            bounds = self._start_bounds(order, e)
            if bounds is None:
                reasons.append(ExclusionReason.SHIFT)
            elif not _subtract(bounds[0], bounds[1], state.unavailable.get(sid, ())):
                reasons.append(ExclusionReason.UNAVAILABLE)
            if not e.equipment_capacity.covers(need):
                reasons.append(ExclusionReason.EQUIPMENT_CAPACITY)
            if reasons:
                excluded[sid] = tuple(reasons)
            else:
                eligible.append(Candidate(sid, needs_restock=not e.equipment_start.covers(need)))
        return EligibilityResult(order.order_id, tuple(eligible), excluded)

    def explain(
        self, order: Order, result: EligibilityResult, since: Seconds | None = None
    ) -> UnassignedReason | None:
        """Причина нераспределения, если подходящих кандидатов нет (иначе None)."""
        if result.eligible:
            return None
        qualified = [sid for sid, rs in result.excluded.items() if not STATIC_REASONS & set(rs)]
        if not qualified:
            counts = {r: sum(r in rs for rs in result.excluded.values()) for r in STATIC_REASONS}
            missing = [REASON_TEXT[r] for r, n in sorted(counts.items()) if n]
            wt = self.reference.work_types[order.work_type_code]
            text = (
                f"На смене нет сотрудника с профилем {'/'.join(sorted(wt.profiles))}, технологией "
                f"{order.technology}"
                + (" и допуском к работам на высоте" if order.requires_height else "")
                + f" (не совпадает: {', '.join(missing)})"
            )
            return UnassignedReason(
                order.order_id,
                UnassignedReasonCode.NO_QUALIFIED_STAFF,
                text,
                UnassignedAction.RESCHEDULE,
                since=since,
            )
        reasons = [set(result.excluded[sid]) for sid in qualified]
        if all(rs & {ExclusionReason.ABSENT, ExclusionReason.UNAVAILABLE} for rs in reasons):
            return UnassignedReason(
                order.order_id,
                UnassignedReasonCode.STAFF_UNAVAILABLE,
                f"Все {len(qualified)} подходящих сотрудника отсутствуют или недоступны в окне визита",
                UnassignedAction.REASSIGN,
                since=since,
            )
        if any(ExclusionReason.SHIFT in rs for rs in reasons):
            return UnassignedReason(
                order.order_id,
                UnassignedReasonCode.NO_SHIFT_TIME,
                "Окно начала визита не пересекается со сменами подходящих сотрудников с учётом дороги",
                UnassignedAction.RESCHEDULE,
                since=since,
            )
        return UnassignedReason(
            order.order_id,
            UnassignedReasonCode.NO_EQUIPMENT,
            "Требуемое АО превышает вместимость рюкзака у всех подходящих сотрудников",
            UnassignedAction.ESCALATE,
            since=since,
        )


def find_eligible_staff(
    order: Order,
    employees: Sequence[Employee],
    state: AvailabilityState | None,
    reference: ReferenceData,
    travel: TravelTimeProvider | None = None,
) -> EligibilityResult:
    """Функциональный интерфейс из задания (без кеша между вызовами)."""
    return EligibilityIndex(reference, travel).find_eligible_staff(order, employees, state)
