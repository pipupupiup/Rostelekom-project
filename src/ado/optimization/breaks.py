"""Разведение обеденных перерывов смен (D36).

Цель: пока одна смена обедает, другая уже (ещё) работает и может принимать наряды. Входные данные
не нарушаются: для каждой группы сотрудников с одинаковой сменой окно **начала** перерыва (H3)
только сужается внутри окна из `staff.json`; длительность перерыва не меняется. Получившиеся окна
передаются симулятору (ADO-07) вместо исходных; валидатор проверяет перерыв по суженному окну,
которое целиком лежит в исходном, — значит, и по исходному.

Шаги:
1. Группы — сотрудники с одинаковыми (начало смены, конец смены, окно, длительность перерыва).
2. Покрытие: перерыв группы целиком должен приходиться на время, когда на смене есть другая
   группа. Окно сужается до такой части; если её нет — окно не трогается, причина в отчёте.
3. Занятость группы перерывом — `[окно.from, окно.to + длительность]`. Если занятости двух групп
   пересекаются, окна делятся точкой `x`: ранняя группа заканчивает обед не позже `x`, поздняя
   начинает не раньше `x`. `x` выбирается так, чтобы меньшее из двух окон было как можно длиннее.

Невыполнимое (одна смена, окна нельзя развести, нет другой смены во время обеда) не «чинится»
нарушением ограничений — оно попадает в `BreakPlan.issues`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from ado.domain.models import Employee, TimeWindow
from ado.domain.timebase import Seconds

STEP = 60


def _hm(s: Seconds) -> str:
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}"


@dataclass(slots=True)
class ShiftGroup:
    key: str
    shift_start: Seconds
    shift_end: Seconds
    break_duration: Seconds
    window: TimeWindow
    """Окно начала перерыва из входных данных."""
    effective: TimeWindow
    """Окно, по которому планируются перерывы (⊆ `window`)."""
    staff_ids: list[str] = field(default_factory=list)
    profiles: dict[str, int] = field(default_factory=dict)

    @property
    def busy_from(self) -> Seconds:
        return self.effective.start

    @property
    def busy_to(self) -> Seconds:
        return self.effective.end + self.break_duration


@dataclass(slots=True)
class BreakPlan:
    groups: list[ShiftGroup]
    issues: list[str]
    changed: bool

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "issues": list(self.issues),
            "groups": [
                {
                    "key": g.key,
                    "shift": f"{_hm(g.shift_start)}–{_hm(g.shift_end)}",
                    "break_duration_min": g.break_duration // 60,
                    "input_window": f"{_hm(g.window.start)}–{_hm(g.window.end)}",
                    "effective_window": f"{_hm(g.effective.start)}–{_hm(g.effective.end)}",
                    "break_busy": f"{_hm(g.busy_from)}–{_hm(g.busy_to)}",
                    "staff": len(g.staff_ids),
                    "profiles": dict(sorted(g.profiles.items())),
                }
                for g in self.groups
            ],
        }


def _groups(employees: Mapping[str, Employee]) -> list[ShiftGroup]:
    by: dict[tuple, ShiftGroup] = {}
    for sid in sorted(employees):
        e = employees[sid]
        sh = e.shift
        k = (sh.start, sh.end, sh.break_window.start, sh.break_window.end, sh.break_duration)
        g = by.get(k)
        if g is None:
            g = by[k] = ShiftGroup(
                key=f"смена {_hm(sh.start)}–{_hm(sh.end)}",
                shift_start=sh.start,
                shift_end=sh.end,
                break_duration=sh.break_duration,
                window=sh.break_window,
                effective=sh.break_window,
            )
        g.staff_ids.append(sid)
        for p in e.profiles:
            g.profiles[p.value] = g.profiles.get(p.value, 0) + 1
    groups = sorted(by.values(), key=lambda g: (g.window.start, g.shift_start, g.window.end))
    names = [g.key for g in groups]
    for g in groups:  # одинаковые смены с разными окнами — различаем по окну
        if names.count(g.key) > 1:
            g.key += f", окно {_hm(g.window.start)}–{_hm(g.window.end)}"
    return groups


def _split(a: ShiftGroup, b: ShiftGroup) -> tuple[TimeWindow, TimeWindow] | None:
    """Точка x: `a` обедает до x, `b` — с x. None — развести нельзя."""
    best: tuple[tuple[int, int], TimeWindow, TimeWindow] | None = None
    lo = max(b.effective.start, a.effective.start + a.break_duration)
    hi = min(b.effective.end, a.effective.end + a.break_duration)
    for x in range(lo, hi + 1, STEP):
        wa = TimeWindow(a.effective.start, min(a.effective.end, x - a.break_duration))
        wb = TimeWindow(max(b.effective.start, x), b.effective.end)
        if wa.end < wa.start or wb.end < wb.start:
            continue
        la, lb = wa.end - wa.start, wb.end - wb.start
        score = (min(la, lb), la + lb)
        if best is None or score > best[0]:
            best = (score, wa, wb)
    return None if best is None else (best[1], best[2])


def _overlap(a: ShiftGroup, b: ShiftGroup) -> bool:
    return a.busy_from < b.busy_to and b.busy_from < a.busy_to


def stagger_breaks(employees: Mapping[str, Employee]) -> tuple[dict[str, Employee], BreakPlan]:
    groups = _groups(employees)
    issues: list[str] = []
    if len(groups) < 2:
        if groups:
            issues.append(
                f"во входных данных одна смена ({groups[0].key}): во время её перерыва другой смены нет, "
                "перекрытие обеспечить нельзя"
            )
        return dict(employees), BreakPlan(groups, issues, changed=False)

    # шаг 2: во время перерыва группы на смене должна быть другая группа (зависит только от
    # смен, не от окон, поэтому делается до деления — и деление балансирует итоговые окна)
    for g in groups:
        best: TimeWindow | None = None
        for h in groups:
            if h is g:
                continue
            s = max(g.effective.start, h.shift_start)
            e = min(g.effective.end, h.shift_end - g.break_duration)
            if s <= e and (best is None or e - s > best.end - best.start):
                best = TimeWindow(s, e)
        if best is None:
            issues.append(
                f"во время перерыва «{g.key}» (окно {_hm(g.effective.start)}–{_hm(g.effective.end)}, "
                f"{g.break_duration // 60} мин) нет другой смены на работе"
            )
        else:
            g.effective = best

    # шаг 3: развести пересекающиеся перерывы (только сужает окна — покрытие сохраняется)
    for i, a in enumerate(groups):
        for b in groups[i + 1 :]:
            if not _overlap(a, b):
                continue
            res = _split(a, b)
            if res is None:
                issues.append(
                    f"перерывы «{a.key}» (окно {_hm(a.window.start)}–{_hm(a.window.end)}) и «{b.key}» "
                    f"(окно {_hm(b.window.start)}–{_hm(b.window.end)}) нельзя развести во времени "
                    "внутри входных окон"
                )
                continue
            a.effective, b.effective = res

    # профили: кто примет наряды профиля, пока группа обедает
    for g in groups:
        for prof in g.profiles:
            if not any(h is not g and h.profiles.get(prof) for h in groups):
                issues.append(f"пока «{g.key}» на перерыве, сотрудников профиля {prof} в других сменах нет")

    out = dict(employees)
    changed = False
    for g in groups:
        if g.effective != g.window:
            changed = True
            for sid in g.staff_ids:
                e = out[sid]
                out[sid] = replace(e, shift=replace(e.shift, break_window=g.effective))
    return out, BreakPlan(groups, issues, changed)


__all__ = ["BreakPlan", "ShiftGroup", "stagger_breaks"]
