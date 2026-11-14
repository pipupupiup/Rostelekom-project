"""ADO-10 — улучшение допустимого плана: Local Search + Adaptive Large Neighborhood Search.

Все изменения проходят симулятор (D6); оценка — общая `working_metrics` + `compare_plans`
(ADO-11). Замороженные остановки (`RouteState.frozen`) операторы не видят: они работают только с
`items`, поэтому модуль безопасен для перепланирования (ADO-13).

**Local Search** (первое улучшение, до отсутствия улучшений или бюджета):
* `insert_unassigned` — regret-вставка нераспределённых нарядов (рост успеха);
* `intra_relocate` — перенос визита на другую позицию маршрута; `intra_reverse` — разворот отрезка;
* `inter_relocate` — перенос визита другому допущенному сотруднику (лучшая позиция, ADO-08);
* `swap` — обмен визитами между маршрутами (кандидаты — ближайшие по дороге);
* `drop_restock` — удаление ставших лишними заездов в СЦ.

**ALNS** (Ропке–Писингер): destroy {random, worst, related (Shaw), low-slack} × repair
{regret-3, greedy}; веса операторов адаптируются по сегментам (σ = 33/9/13 за новый лучший /
лучше текущего / принят), приёмка — отжиг по `scalar_objective` (только для приёмки;
публикуется лучший по `compare_plans`, никогда не хуже исходного). Детерминирован при
фиксированном seed и ограничении по итерациям; ограничение по времени — внешний `TimeBudget`.
"""

from __future__ import annotations

import math
import random
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ado.domain.enums import StopType
from ado.domain.models import PlanItem, RestockItem, VisitItem
from ado.optimization.budget import TimeBudget
from ado.optimization.evaluation import compare_plans, scalar_objective, working_metrics
from ado.optimization.initial import InitialConfig, explain_option, regret_insertion
from ado.optimization.insertion import InsertionEngine
from ado.optimization.state import RouteState, WorkingPlan


@dataclass(frozen=True, slots=True)
class ImproveConfig:
    seed: int = 0
    max_iterations: int = 5000
    max_no_improve: int = 400
    time_limit_sec: float = 90.0
    """Верхний предел на ALNS внутри внешнего бюджета (300 с на план)."""
    destroy_min: int = 3
    destroy_max: int = 20
    destroy_frac: float = 0.10
    sa_temperature: float = 430.0
    """Ухудшение пути на 300 с принимается с вероятностью ≈ 0,5 в начале."""
    sa_cooling: float = 0.998
    reaction: float = 0.2
    segment: int = 40
    scores: tuple[float, float, float] = (33.0, 9.0, 13.0)
    ls_max_rounds: int = 4
    swap_neighbors: int = 5
    use_ls: bool = True
    use_alns: bool = True


@dataclass(slots=True)
class OperatorStats:
    calls: int = 0
    new_best: int = 0
    better: int = 0
    accepted: int = 0
    seconds: float = 0.0


@dataclass(slots=True)
class ImproveStats:
    ls_moves: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    ls_seconds: float = 0.0
    alns_iterations: int = 0
    alns_seconds: float = 0.0
    operators: dict[str, OperatorStats] = field(default_factory=lambda: defaultdict(OperatorStats))
    weights: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, float | int]:
        out: dict[str, float | int] = {
            "ls_sec": round(self.ls_seconds, 3),
            "alns_iterations": self.alns_iterations,
            "alns_sec": round(self.alns_seconds, 3),
        }
        for k, v in self.ls_moves.items():
            out[f"ls_{k}"] = v
        for k, op in self.operators.items():
            out[f"op_{k}_calls"] = op.calls
            out[f"op_{k}_best"] = op.new_best
        return out


# ------------------------------------------------------------------ примитивы


def _apply(working: WorkingPlan, sid: str, items: list[PlanItem]) -> bool:
    route = working.routes[sid]
    sim = route.simulate(items, working.day, working.travel)
    if not sim.feasible:
        return False
    route.set_schedule(items, sim, working.day)
    return True


def _try(working: WorkingPlan, sid: str, items: list[PlanItem]):
    sim = working.routes[sid].simulate(items, working.day, working.travel)
    return sim if sim.feasible else None


def _without(items: list[PlanItem], oid: str) -> list[PlanItem]:
    return [i for i in items if not (isinstance(i, VisitItem) and i.order_id == oid)]


def remove_orders(working: WorkingPlan, order_ids: set[str]) -> tuple[list[str], set[str]]:
    """Снять наряды с маршрутов (только незамороженные элементы). → (снятые, изменённые маршруты)."""
    removed: list[str] = []
    changed: set[str] = set()
    for sid in sorted(working.routes):
        r = working.routes[sid]
        hit = [oid for oid in r.visit_ids() if oid in order_ids]
        if not hit:
            continue
        items = [i for i in r.items if not (isinstance(i, VisitItem) and i.order_id in order_ids)]
        if _apply(working, sid, items):
            removed.extend(hit)
            changed.add(sid)
        else:  # редкий случай (перерыв): снимаем по одному
            for oid in hit:
                if _apply(working, sid, _without(working.routes[sid].items, oid)):
                    removed.append(oid)
                    changed.add(sid)
    return removed, changed


def open_pool(working: WorkingPlan, changed: set[str] | None = None) -> list[str]:
    """Нераспределённые наряды, которые могут встать в изменённые маршруты (или во все)."""
    day = working.day
    assigned = working.assigned()
    pgp = {oid for g in working.pgp_groups for oid in g.order_ids}
    out = []
    for oid, el in working.eligible.items():
        if not el or oid in assigned or oid in pgp or oid in working.cancelled or oid not in day.orders:
            continue
        if changed is None or changed.intersection(el):
            out.append(oid)
    return sorted(out)


# ------------------------------------------------------------------ Local Search


class LocalSearch:
    def __init__(
        self, engine: InsertionEngine, init_cfg: InitialConfig, cfg: ImproveConfig, stats: ImproveStats
    ):
        self.engine = engine
        self.init_cfg = init_cfg
        self.cfg = cfg
        self.stats = stats

    def insert_unassigned(
        self, working: WorkingPlan, budget: TimeBudget, changed: set[str] | None = None
    ) -> bool:
        pool = open_pool(working, changed)
        if not pool:
            return False
        out = regret_insertion(working, pool, self.engine, self.init_cfg, budget)
        self.stats.ls_moves["insert_unassigned"] += len(out.assigned)
        return bool(out.assigned)

    def intra(self, working: WorkingPlan, budget: TimeBudget) -> bool:
        improved = False
        for sid in sorted(working.routes):
            r = working.routes[sid]
            n = len(r.items)
            if n < 2:
                continue
            base = r.sim.travel_sec
            done = False
            for i in range(n):
                if done or budget.expired():
                    break
                for j in range(n):
                    if i == j:
                        continue
                    items = list(r.items)
                    it = items.pop(i)
                    items.insert(j, it)
                    sim = _try(working, sid, items)
                    if sim and sim.travel_sec < base:
                        r.set_schedule(items, sim, working.day)
                        self.stats.ls_moves["intra_relocate"] += 1
                        improved = done = True
                        break
            if not done and n >= 3:
                for i in range(n - 1):
                    for j in range(i + 2, n + 1):
                        items = r.items[:i] + list(reversed(r.items[i:j])) + r.items[j:]
                        sim = _try(working, sid, items)
                        if sim and sim.travel_sec < base:
                            r.set_schedule(items, sim, working.day)
                            self.stats.ls_moves["intra_reverse"] += 1
                            improved = True
                            break
                    else:
                        continue
                    break
        return improved

    def inter_relocate(self, working: WorkingPlan, budget: TimeBudget) -> bool:
        improved = False
        day = working.day
        for sid in sorted(working.routes):
            r1 = working.routes[sid]
            for oid in list(r1.visit_ids()):
                if budget.expired():
                    return improved
                rem_items = _without(r1.items, oid)
                rem = _try(working, sid, rem_items)
                if rem is None:
                    continue
                saving = r1.sim.travel_sec - rem.travel_sec
                best = None
                for sid2 in working.eligible.get(oid, ()):
                    if sid2 == sid or sid2 not in working.routes:
                        continue
                    opt = self.engine.best_for_route(day.orders[oid], working.routes[sid2])
                    if (
                        opt is not None
                        and opt.delta_travel_sec < saving
                        and (best is None or opt.score < best.score)
                    ):
                        best = opt
                if best is not None:
                    r1.set_schedule(rem_items, rem, day)
                    working.routes[best.staff_id].set_schedule(list(best.items), best.simulation, day)
                    self.stats.ls_moves["inter_relocate"] += 1
                    improved = True
        return improved

    def swap(self, working: WorkingPlan, budget: TimeBudget) -> bool:
        improved = False
        day, travel = working.day, working.travel
        where = working.movable()
        visits = sorted(where)
        for a in visits:
            if budget.expired():
                break
            sa = where.get(a)
            if sa is None:
                continue
            ba = day.orders[a].building_id
            near = sorted(
                (b for b in visits if where.get(b) not in (None, sa)),
                key=lambda b: (travel.travel_sec(ba, day.orders[b].building_id) or 10**9, b),
            )[: self.cfg.swap_neighbors]
            for b in near:
                sb = where[b]
                if sb not in working.eligible.get(a, ()) or sa not in working.eligible.get(b, ()):
                    continue
                ra, rb = working.routes[sa], working.routes[sb]
                ia = [VisitItem(b) if isinstance(i, VisitItem) and i.order_id == a else i for i in ra.items]
                ib = [VisitItem(a) if isinstance(i, VisitItem) and i.order_id == b else i for i in rb.items]
                sim_a = _try(working, sa, ia)
                if sim_a is None:
                    continue
                sim_b = _try(working, sb, ib)
                if sim_b is None:
                    continue
                if sim_a.travel_sec + sim_b.travel_sec < ra.sim.travel_sec + rb.sim.travel_sec:
                    ra.set_schedule(ia, sim_a, day)
                    rb.set_schedule(ib, sim_b, day)
                    where[a], where[b] = sb, sa
                    self.stats.ls_moves["swap"] += 1
                    improved = True
                    break
        return improved

    def anchor_insert(
        self,
        working: WorkingPlan,
        budget: TimeBudget,
        changed: set[str] | None = None,
        radius_sec: int = 900,
    ) -> set[str]:
        """Нераспределённый наряд `u` + «якорь» (D32).

        Иногда `u` не встаёт ни в один маршрут лишь потому, что сотруднику нечего делать рядом до
        обеда: перерыв берётся на месте предыдущей остановки, из СЦ к началу окна не успеть.
        Оператор переносит к допущенному сотруднику соседний наряд `v` (≤ `radius_sec` езды до `u`)
        и вставляет `u` следом. Принимается, только если оба маршрута допустимы: +1 выполненный наряд.
        `changed` (при событии) — якоря только из изменённых маршрутов, кандидаты `u` — допущенные к
        ним; так ответ на событие не растёт с числом нераспределённых. → изменённые маршруты.
        """
        day, travel = working.day, working.travel
        pool = open_pool(working, changed)
        touched: set[str] = set()
        if not pool:
            return touched
        where = working.movable()
        prio = working.priority
        for u in sorted(pool, key=lambda x: (-prio.get(x, 0), x)):
            if budget.expired():
                break
            ou = day.orders[u]
            near = []
            for v, sv in where.items():
                if changed is not None and sv not in changed:
                    continue
                tv = travel.travel_sec(day.orders[v].building_id, ou.building_id)
                if tv is not None and tv <= radius_sec:
                    near.append((tv, v))
            near.sort()
            done = False
            for sid2 in sorted(working.eligible.get(u, ())):
                if done or budget.expired() or sid2 not in working.routes:
                    break
                r2 = working.routes[sid2]
                if self.engine.best_for_route(ou, r2) is not None:
                    continue  # встанет и без якоря — это работа insert_unassigned
                for _, v in near[: self.cfg.swap_neighbors]:
                    sv = where.get(v)
                    if sv is None or sv == sid2 or sid2 not in working.eligible.get(v, ()):
                        continue
                    r1 = working.routes[sv]
                    rem_items = _without(r1.items, v)
                    rem = _try(working, sv, rem_items)
                    if rem is None:
                        continue
                    opt_v = self.engine.best_for_route(day.orders[v], r2)
                    if opt_v is None:
                        continue
                    old_items, old_sim = list(r2.items), r2.sim
                    r2.set_schedule(list(opt_v.items), opt_v.simulation, day)
                    opt_u = self.engine.best_for_route(ou, r2)
                    if opt_u is None:
                        r2.set_schedule(old_items, old_sim, day)
                        continue
                    r1.set_schedule(rem_items, rem, day)
                    r2.set_schedule(list(opt_u.items), opt_u.simulation, day)
                    working.unassigned.pop(u, None)
                    working.explanations[u] = (
                        explain_option(opt_u, 0, ou)
                        + f"; к сотруднику перенесён соседний наряд {v} ({sv} → {sid2})"
                    )
                    where[v] = sid2
                    where[u] = sid2
                    self.stats.ls_moves["anchor_insert"] += 1
                    touched.update((sv, sid2))
                    done = True
                    break
        return touched

    def drop_restock(self, working: WorkingPlan, budget: TimeBudget) -> bool:
        improved = False
        for sid in sorted(working.routes):
            r = working.routes[sid]
            for i, it in enumerate(list(r.items)):
                if isinstance(it, RestockItem):
                    items = r.items[:i] + r.items[i + 1 :]
                    sim = _try(working, sid, items)
                    if sim and sim.travel_sec <= r.sim.travel_sec:
                        r.set_schedule(items, sim, working.day)
                        self.stats.ls_moves["drop_restock"] += 1
                        improved = True
                        break
        return improved

    def run(self, working: WorkingPlan, budget: TimeBudget, changed: set[str] | None = None) -> WorkingPlan:
        t0 = time.perf_counter()
        for _ in range(self.cfg.ls_max_rounds):
            if budget.expired():
                break
            moved = self.insert_unassigned(working, budget, changed)
            moved |= self.drop_restock(working, budget)
            moved |= self.intra(working, budget)
            moved |= self.inter_relocate(working, budget)
            moved |= self.swap(working, budget)
            moved |= self.insert_unassigned(working, budget)
            moved |= bool(self.anchor_insert(working, budget))
            changed = None
            if not moved:
                break
        self.stats.ls_seconds += time.perf_counter() - t0
        return working


# ------------------------------------------------------------------ ALNS

Destroy = Callable[[WorkingPlan, int, random.Random], set[str]]


def _visit_stops(working: WorkingPlan):
    for sid, r in working.routes.items():
        n_frozen = len(r.frozen)
        stops = r.sim.stops
        for i in range(n_frozen, len(stops)):
            s = stops[i]
            if s.type is StopType.VISIT and s.order_id:
                yield sid, i, s


def destroy_random(working: WorkingPlan, q: int, rnd: random.Random) -> set[str]:
    ids = sorted(oid for r in working.routes.values() for oid in r.visit_ids())
    return set(rnd.sample(ids, min(q, len(ids))))


def destroy_worst(working: WorkingPlan, q: int, rnd: random.Random) -> set[str]:
    travel = working.travel
    savings = []
    for sid, i, s in _visit_stops(working):
        stops = working.routes[sid].sim.stops
        prev = stops[i - 1]
        nxt = next((x for x in stops[i + 1 :] if x.type is not StopType.BREAK), None)
        if nxt is None:
            continue
        direct = travel.travel_sec(prev.location_id, nxt.location_id) or 0
        out = travel.travel_sec(s.location_id, nxt.location_id) or 0
        savings.append((s.travel_sec + out - direct, s.order_id))
    savings.sort(reverse=True)
    chosen: set[str] = set()
    while savings and len(chosen) < q:
        k = int(len(savings) * rnd.random() ** 3)  # смещение к худшим (p = 3)
        chosen.add(savings.pop(k)[1])
    return chosen


def destroy_related(working: WorkingPlan, q: int, rnd: random.Random) -> set[str]:
    day, travel = working.day, working.travel
    visits = [(s.order_id, s.start) for _, _, s in _visit_stops(working)]
    if not visits:
        return set()
    visits.sort()
    seed_id, seed_start = visits[rnd.randrange(len(visits))]
    seed_b = day.orders[seed_id].building_id

    def rel(v: tuple[str, int]) -> float:
        b = day.orders[v[0]].building_id
        return (travel.travel_sec(seed_b, b) or 10**6) / 600 + abs(v[1] - seed_start) / 1800

    ranked = sorted(visits, key=lambda v: (rel(v), v[0]))
    return {v[0] for v in ranked[:q]}


def destroy_slack(working: WorkingPlan, q: int, rnd: random.Random) -> set[str]:
    day = working.day
    slack = sorted(
        (day.orders[s.order_id].latest_start - s.start, s.order_id) for _, _, s in _visit_stops(working)
    )
    chosen: set[str] = set()
    while slack and len(chosen) < q:
        k = int(len(slack) * rnd.random() ** 2)
        chosen.add(slack.pop(k)[1])
    return chosen


DESTROYS: dict[str, Destroy] = {
    "random": destroy_random,
    "worst": destroy_worst,
    "related": destroy_related,
    "slack": destroy_slack,
}
REPAIRS = ("regret", "greedy")


def _roulette(weights: dict[str, float], rnd: random.Random) -> str:
    names = sorted(weights)
    total = sum(weights[n] for n in names)
    x = rnd.random() * total
    for n in names:
        x -= weights[n]
        if x <= 0:
            return n
    return names[-1]


class RouteImprover:
    """Улучшатель для `build_daily_plan(improver=...)` и для ADO-13. Хранит `last_stats`."""

    def __init__(
        self, config: ImproveConfig = ImproveConfig(), init_config: InitialConfig = InitialConfig()
    ) -> None:
        self.config = config
        self.init_config = init_config
        self.last_stats: dict[str, float | int] = {}

    def __call__(self, working: WorkingPlan, budget: TimeBudget) -> WorkingPlan:
        return self.improve(working, budget)

    def improve(
        self, working: WorkingPlan, budget: TimeBudget, changed: set[str] | None = None
    ) -> WorkingPlan:
        cfg = self.config
        stats = ImproveStats()
        engine = InsertionEngine(working.day, working.travel, self.init_config.insertion)
        ls = LocalSearch(engine, self.init_config, cfg, stats)
        start_metrics = working_metrics(working)
        cur = working.copy()
        if cfg.use_ls:
            ls.run(cur, budget, changed)
        best = cur.copy()
        best_m = working_metrics(best)
        if cfg.use_alns:
            alns_budget = budget.sub_budget(cfg.time_limit_sec)
            cur, best, best_m = self._alns(cur, best, best_m, engine, alns_budget, stats)
            if cfg.use_ls and not budget.expired():
                cand = ls.run(best.copy(), budget)
                cm = working_metrics(cand)
                if compare_plans(cm, best_m) > 0:
                    best, best_m = cand, cm
        if compare_plans(best_m, start_metrics) < 0:  # никогда не хуже исходного
            best, best_m = working, start_metrics
        self.last_stats = stats.as_dict()
        self.last_stats.update(
            start_weighted=start_metrics.weighted_success,
            final_weighted=best_m.weighted_success,
            start_travel=start_metrics.travel_sec_total,
            final_travel=best_m.travel_sec_total,
        )
        return best

    def _alns(self, cur, best, best_m, engine, budget: TimeBudget, stats: ImproveStats):
        cfg = self.config
        rnd = random.Random(cfg.seed)
        weights = {**{f"d:{k}": 1.0 for k in DESTROYS}, **{f"r:{k}": 1.0 for k in REPAIRS}}
        seg_score: dict[str, float] = defaultdict(float)
        seg_used: dict[str, int] = defaultdict(int)
        cur_m = working_metrics(cur)
        cur_s = scalar_objective(cur_m)
        temp = cfg.sa_temperature
        no_improve = 0
        greedy_cfg = replace(self.init_config, regret_k=1)
        t0 = time.perf_counter()
        it = 0
        for it in range(1, cfg.max_iterations + 1):
            if budget.expired() or no_improve >= cfg.max_no_improve:
                break
            n_assigned = sum(len(r.visit_ids()) for r in cur.routes.values())
            if n_assigned == 0:
                break
            q = max(
                cfg.destroy_min,
                min(cfg.destroy_max, int(n_assigned * cfg.destroy_frac * (0.5 + rnd.random()))),
            )
            d_name = _roulette({k[2:]: v for k, v in weights.items() if k.startswith("d:")}, rnd)
            r_name = _roulette({k[2:]: v for k, v in weights.items() if k.startswith("r:")}, rnd)
            t_op = time.perf_counter()
            cand = cur.copy()
            removed, changed = remove_orders(cand, DESTROYS[d_name](cand, q, rnd))
            opened = [oid for oid in open_pool(cand, changed) if oid not in removed]
            pool = sorted(set(removed) | set(opened))
            regret_insertion(
                cand,
                pool,
                engine,
                self.init_config if r_name == "regret" else greedy_cfg,
                budget,
                only_routes={oid: changed for oid in opened},
            )
            cand_m = working_metrics(cand)
            cand_s = scalar_objective(cand_m)
            dt = time.perf_counter() - t_op
            for name in (f"d:{d_name}", f"r:{r_name}"):
                op = stats.operators[name]
                op.calls += 1
                op.seconds += dt
                seg_used[name] += 1
            sigma = 0.0
            if compare_plans(cand_m, best_m) > 0:
                best, best_m = cand.copy(), cand_m
                sigma = cfg.scores[0]
                no_improve = 0
                for name in (f"d:{d_name}", f"r:{r_name}"):
                    stats.operators[name].new_best += 1
            else:
                no_improve += 1
            if cand_s >= cur_s or rnd.random() < math.exp((cand_s - cur_s) / max(temp, 1e-9)):
                if not sigma:
                    sigma = cfg.scores[1] if cand_s > cur_s else cfg.scores[2]
                    for name in (f"d:{d_name}", f"r:{r_name}"):
                        if cand_s > cur_s:
                            stats.operators[name].better += 1
                        stats.operators[name].accepted += 1
                cur, cur_m, cur_s = cand, cand_m, cand_s
            for name in (f"d:{d_name}", f"r:{r_name}"):
                seg_score[name] += sigma
            temp *= cfg.sa_cooling
            if it % cfg.segment == 0:
                for name in weights:
                    if seg_used[name]:
                        weights[name] = (1 - cfg.reaction) * weights[name] + cfg.reaction * seg_score[
                            name
                        ] / seg_used[name]
                        weights[name] = max(weights[name], 0.05)
                seg_score.clear()
                seg_used.clear()
        stats.alns_iterations = it if it else 0
        stats.alns_seconds = time.perf_counter() - t0
        stats.weights = dict(weights)
        return cur, best, best_m


def improve_routes(
    working: WorkingPlan, budget: TimeBudget, config: ImproveConfig = ImproveConfig()
) -> WorkingPlan:
    return RouteImprover(config).improve(working, budget)


def route_state_items(route: RouteState) -> list[PlanItem]:
    return list(route.items)
