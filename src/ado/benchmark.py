"""ADO-17 — воспроизводимое сравнение вариантов алгоритма (только автоматически измеримое).

Варианты (одинаковые данные, seed, оборудование):
* **Baseline** — последовательное назначение в конец маршрута; при событиях вытесненные наряды
  снимаются без перевставки; без ПГП и без предупреждений LATE_RISK;
* **Basic** — допуски + приоритеты + regret-3 + Local Search; при событиях — перевставка (ADO-13
  без LS), без ПГП и без предупреждений;
* **Full** — Basic + ALNS + полное перепланирование + прогноз/предупреждения + ПГП.

Для каждого дня и варианта: время построения плана, время ответа на каждое событие, проверка
**каждой** версии независимым валидатором (с разделением «по вине алгоритма» / «вызвано
событием»), качество после каждого события, итоговые метрики, своевременность LATE_RISK,
выявление ПГП (точность/полнота по эталону генератора). Экспертные критерии (интерфейс,
архитектура) не оцениваются.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import replace
from typing import Any

from ado.api.service import DispatchService
from ado.config import Settings
from ado.domain.enums import AlertType
from ado.domain.models import DayInstance
from ado.events.session import DaySession, ReoptConfig
from ado.optimization.evaluation import evaluate_plan
from ado.optimization.improvement import ImproveConfig
from ado.optimization.initial import InitialConfig
from ado.optimization.pgp import PgpConfig
from ado.optimization.validator import validate_plan
from ado.orchestrator import RunResult, run_day, service_client, synthetic_scenario
from ado.synthetic import SyntheticDay, generate_day

NO_PGP = PgpConfig(min_count=10**6)


def variant_config(name: str, alns_sec: float) -> ReoptConfig:
    if name == "baseline":
        return ReoptConfig(
            initial=InitialConfig(pgp=NO_PGP),
            initial_algorithm="baseline",
            dynamic="naive",
            late_risk_alerts=False,
            improve=ImproveConfig(use_ls=False, use_alns=False),
        )
    if name == "basic":
        return ReoptConfig(
            initial=InitialConfig(pgp=NO_PGP),
            improve=ImproveConfig(use_alns=False, use_ls=True),
            late_risk_alerts=False,
        )
    if name == "full":
        return ReoptConfig(improve=replace(ImproveConfig(), time_limit_sec=alns_sec))
    raise ValueError(name)


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, round(p * (len(s) - 1)))]


def analyze(session: DaySession, run: RunResult, syn: SyntheticDay) -> dict[str, Any]:
    day, travel = session.day, session.travel
    violations: list[tuple[int, str, str]] = []
    trace: list[dict[str, Any]] = []
    prev = None
    for plan, overrides, nb, known in session.snapshots:
        view = DayInstance(
            day.date,
            day.now,
            {k: v for k, v in day.orders.items() if k in known},
            day.absent_staff_ids,
            day.reference,
        )
        issues = validate_plan(plan, view, travel, previous=prev, end_overrides=overrides, not_before=nb)
        for i in issues:
            route = plan.routes.get(i.staff_id or "")
            emp = day.reference.employees.get(i.staff_id or "")
            caused = bool(
                route
                and emp
                and i.code.value in ("H2", "H3")
                and any(
                    st.order_id in overrides
                    and (st.end > emp.shift.end or st.end > emp.shift.break_window.end)
                    for st in route.stops
                )
            )
            violations.append((plan.plan_version, str(i), "event" if caused else "algorithm"))
        m = evaluate_plan(
            plan, view, travel, issues=validate_plan(plan, view, travel, end_overrides=overrides)
        )
        trace.append(
            {
                "version": plan.plan_version,
                "event": plan.based_on_event_id,
                "as_of": plan.as_of,
                "weighted": m.weighted_success,
                "weighted_total": m.weighted_total,
                "unassigned": m.unassigned_count,
                "travel_sec": m.travel_sec_total,
            }
        )
        prev = plan
    final, overrides, _, _ = session.snapshots[-1]
    m = evaluate_plan(final, day, travel, issues=validate_plan(final, day, travel, end_overrides=overrides))

    alerts = {a.order_id: a for a in final.alerts if a.type is AlertType.LATE_RISK}
    moved = timely = inevitable = 0
    prev = None
    for plan, *_ in session.snapshots:
        if prev is not None:
            a = {s.order_id: sid for sid, r in prev.routes.items() for s in r.stops if s.order_id}
            b = {s.order_id: sid for sid, r in plan.routes.items() for s in r.stops if s.order_id}
            for oid, sid in a.items():
                if (
                    b.get(oid) != sid
                    and oid not in plan.cancelled
                    and not any(oid in g.order_ids for g in plan.pgp_groups)
                ):
                    moved += 1
                    deadline = day.orders[oid].slot.start - 1800
                    alert = alerts.get(oid)
                    if alert is not None and alert.created_at <= deadline:
                        timely += 1
                    elif plan.as_of > deadline:
                        inevitable += 1
        prev = plan

    truth = {o for inc in syn.pgp_incidents for o in inc}
    found = {o for g in final.pgp_groups for o in g.order_ids}
    ev = [c.seconds for c in run.calls if c.name.startswith("event")]
    plan_call = next(c for c in run.calls if c.name == "plan")
    first = trace[0]
    return {
        "plan_sec": round(plan_call.seconds, 2),
        "events": len(ev),
        "event_sec_max": round(max(ev), 3) if ev else 0.0,
        "event_sec_p95": round(pct(ev, 0.95), 3),
        "versions": len(session.snapshots),
        "violations_algorithm": sum(v[2] == "algorithm" for v in violations),
        "violations_event": sum(v[2] == "event" for v in violations),
        "violations_sample": violations[:5],
        "initial_weighted_share": round(first["weighted"] / first["weighted_total"], 4)
        if first["weighted_total"]
        else 1,
        "initial_travel_h": round(first["travel_sec"] / 3600, 1),
        "final_success": m.success_count,
        "final_orders": m.orders_total,
        "final_weighted_share": round(m.weighted_success_share, 4),
        "final_unassigned": m.unassigned_count,
        "final_travel_h": round(m.travel_sec_total / 3600, 1),
        "final_cv": round(m.utilization_cv, 3),
        "reschedules": m.reschedules,
        "moved_orders": moved,
        "late_risk_timely": timely,
        "late_risk_inevitable": inevitable,
        "late_risk_timely_share": round(timely / (moved - inevitable), 3) if moved - inevitable > 0 else 1.0,
        "pgp_precision": round(len(truth & found) / len(found), 3) if found else (1.0 if not truth else 0.0),
        "pgp_recall": round(len(truth & found) / len(truth), 3) if truth else 1.0,
        "trace": trace,
        "errors": run.errors,
    }


def run_variant(
    name: str, syn: SyntheticDay, scenario: dict[str, Any], alns_sec: float = 60, time_limit_sec: int = 300
) -> dict[str, Any]:
    svc = DispatchService(Settings(team=f"bench-{name}"), variant_config(name, alns_sec))
    t0 = time.perf_counter()
    run = run_day(
        service_client(svc, syn.day_input["date"]),
        syn.reference,
        syn.matrix,
        syn.day_input,
        scenario,
        time_limit_sec,
    )
    res = analyze(svc.session, run, syn)
    res["wall_sec"] = round(time.perf_counter() - t0, 1)
    res["variant"] = name
    return res


def compare(
    seeds: list[int],
    n_orders: int = 400,
    alns_sec: float = 60,
    variants: tuple[str, ...] = ("baseline", "basic", "full"),
) -> list[dict[str, Any]]:
    rows = []
    for seed in seeds:
        syn = generate_day(n_orders=n_orders, seed=seed)
        scenario = synthetic_scenario(syn.day_input, syn.late_orders, syn.reference, seed=seed)
        for v in variants:
            r = run_variant(v, syn, scenario, alns_sec)
            r["seed"] = seed
            rows.append(r)
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    keys = [
        "plan_sec",
        "event_sec_max",
        "initial_weighted_share",
        "final_weighted_share",
        "final_travel_h",
        "final_cv",
        "final_unassigned",
        "reschedules",
        "late_risk_timely_share",
        "pgp_precision",
        "pgp_recall",
        "violations_algorithm",
        "violations_event",
    ]
    out: dict[str, dict[str, float]] = {}
    for v in dict.fromkeys(r["variant"] for r in rows):
        part = [r for r in rows if r["variant"] == v]
        out[v] = {k: round(statistics.fmean(float(r[k]) for r in part), 4) for k in keys}
        out[v]["event_sec_max"] = max(r["event_sec_max"] for r in part)
    return out
