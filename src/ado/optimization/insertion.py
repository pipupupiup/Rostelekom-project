"""ADO-06 (поиск кандидатов) и ADO-08 (допустимая вставка).

**Кандидаты.** Для каждого допущенного сотрудника (ADO-04) по его текущему маршруту считается
предварительная стоимость — минимальный прирост дорожного времени по позициям, прошедшим
O(1)-предпроверку (`RouteState.precheck`). Учитываются занятость, свободные интервалы и позиция
сотрудника на момент визита (предыдущая остановка в маршруте), а не расстояние от СЦ. Сотрудники
упорядочиваются по `(нужно пополнение, стоимость, staff_id)` и проверяются группами: сначала
`first_group_size`, затем расширение; `full=True` — все подходящие.

**Вставка.** Для маршрута проверяются позиции по возрастанию стоимости; первая, которую
подтвердил симулятор, — лучшая для маршрута (стоимость предпроверки совпадает с точным
приростом дороги, симулятор лишь подтверждает допустимость). Если рюкзака не хватает, перед
визитом вставляется `RESTOCK` в ближайшем по пути СЦ. Оценка вставки — прирост времени в пути,
с; это согласовано с порядком целей D7 (ценность наряда учитывается выше — в выборе наряда).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from ado.domain.contracts import TravelTimeProvider
from ado.domain.models import DayInstance, InsertionOption, Order, PlanItem, RestockItem, VisitItem
from ado.optimization.state import PositionCheck, RouteState


@dataclass(frozen=True, slots=True)
class InsertionConfig:
    max_simulations_per_route: int = 4
    first_group_size: int = 6
    expand_step: int = 6
    min_options: int = 2
    """Сколько допустимых вариантов искать до остановки расширения (для regret нужно ≥ 2)."""
    restock_centers: int = 2


@dataclass(slots=True)
class InsertionStats:
    prechecks: int = 0
    simulations: int = 0
    routes_considered: int = 0
    positions_passed: int = 0

    def merge(self, other: InsertionStats) -> None:
        self.prechecks += other.prechecks
        self.simulations += other.simulations
        self.routes_considered += other.routes_considered
        self.positions_passed += other.positions_passed


@dataclass(frozen=True, slots=True)
class CandidateEstimate:
    staff_id: str
    best_delta: int | None
    positions: tuple[PositionCheck, ...]

    @property
    def promising(self) -> bool:
        return bool(self.positions)


@dataclass(slots=True)
class InsertionEngine:
    day: DayInstance
    travel: TravelTimeProvider
    config: InsertionConfig = field(default_factory=InsertionConfig)
    stats: InsertionStats = field(default_factory=InsertionStats)

    # ------------------------------------------------------------ ADO-06
    def rank_candidates(
        self, order: Order, staff_ids: Sequence[str], routes: Mapping[str, RouteState]
    ) -> list[CandidateEstimate]:
        dur = self.day.reference.work_types[order.work_type_code].duration
        out = []
        for sid in staff_ids:
            route = routes.get(sid)
            if route is None:
                continue
            checks = route.precheck(order, dur, self.day, self.travel)
            self.stats.prechecks += 1
            best = min((c.delta_travel for c in checks), default=None)
            out.append(CandidateEstimate(sid, best, tuple(checks)))
        out.sort(
            key=lambda c: (
                not c.promising,
                min((ch.needs_restock for ch in c.positions), default=True),
                c.best_delta if c.best_delta is not None else 10**12,
                c.staff_id,
            )
        )
        return out

    def candidate_groups(self, ranked: list[CandidateEstimate]) -> Iterator[list[CandidateEstimate]]:
        promising = [c for c in ranked if c.promising]
        size = self.config.first_group_size
        i = 0
        while i < len(promising):
            yield promising[i : i + size]
            i += size
            size = self.config.expand_step

    # ------------------------------------------------------------ ADO-08
    def _restock_centers(self, prev_loc: str, building: str, at: int) -> list[str]:
        scs = []
        for sc in self.day.reference.service_centers.values():
            a = self.travel.travel_sec(prev_loc, sc.service_center_id)
            b = self.travel.travel_sec(sc.service_center_id, building)
            if a is None or b is None or at + a > sc.opening.end - sc.restock_duration:
                continue
            scs.append((a + b, sc.service_center_id))
        return [sid for _, sid in sorted(scs)[: self.config.restock_centers]]

    def best_for_route(
        self, order: Order, route: RouteState, checks: Sequence[PositionCheck] | None = None
    ) -> InsertionOption | None:
        if checks is None:
            dur = self.day.reference.work_types[order.work_type_code].duration
            checks = route.precheck(order, dur, self.day, self.travel)
            self.stats.prechecks += 1
        self.stats.routes_considered += 1
        self.stats.positions_passed += len(checks)
        visit = VisitItem(order.order_id)
        budget = self.config.max_simulations_per_route
        for c in checks:
            if budget <= 0:
                break
            p = c.position
            variants: list[tuple[list[PlanItem], str | None]] = []
            if not c.needs_restock:
                variants.append(([*route.items[:p], visit, *route.items[p:]], None))
            else:
                if p == 0:
                    prev_loc, prev_end = route.origin()
                else:
                    prev = route.chain[p - 1]
                    prev_loc, prev_end = prev.location_id, prev.end
                for sc in self._restock_centers(prev_loc, order.building_id, prev_end):
                    variants.append(([*route.items[:p], RestockItem(sc), visit, *route.items[p:]], sc))
            for items, sc in variants:
                if budget <= 0:
                    break
                budget -= 1
                sim = route.simulate(items, self.day, self.travel)
                self.stats.simulations += 1
                if sim.feasible:
                    delta = sim.travel_sec - route.sim.travel_sec
                    return InsertionOption(
                        order.order_id,
                        route.staff_id,
                        p,
                        delta,
                        sc is not None,
                        sim,
                        float(delta),
                        sc,
                        tuple(items),
                    )
        return None

    def find_feasible_insertions(
        self,
        order: Order,
        staff_ids: Sequence[str],
        routes: Mapping[str, RouteState],
        *,
        full: bool = False,
    ) -> list[InsertionOption]:
        """Допустимые вставки (по одной лучшей на маршрут), по возрастанию стоимости."""
        ranked = self.rank_candidates(order, staff_ids, routes)
        options: list[InsertionOption] = []
        for group in self.candidate_groups(ranked):
            for cand in group:
                opt = self.best_for_route(order, routes[cand.staff_id], cand.positions)
                if opt is not None:
                    options.append(opt)
            if not full and len(options) >= self.config.min_options:
                break
        options.sort(key=lambda o: (o.score, o.staff_id))
        return options
