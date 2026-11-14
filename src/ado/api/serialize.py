"""Сериализация домена в JSON строго по схемам `spec/api_spec.yaml` (ADO-16).

Моменты — ISO 8601 со смещением `+05:00`, с точностью до секунды; `seq` — 1, 2, 3…;
справочные поля (`travel_sec`, `distance_m`, `totals`, `summary`, `status`, `equipment_after`)
заполняются, валидатор их пересчитывает. Обязательные поля присутствуют всегда.
"""

from __future__ import annotations

import statistics
from typing import Any

from ado.domain.contracts import TravelTimeProvider
from ado.domain.enums import StopType
from ado.domain.models import Alert, DayInstance, PgpGroup, Plan, Route, TimeWindow, UnassignedReason
from ado.domain.timebase import TimeBase


def _t(tb: TimeBase, v: int | None) -> str | None:
    return None if v is None else tb.format(v)


def _window(tb: TimeBase, w: TimeWindow | None) -> dict[str, str] | None:
    return None if w is None else {"start": tb.format(w.start), "end": tb.format(w.end)}


def route_json(route: Route, day: DayInstance, tb: TimeBase, as_of: int) -> dict[str, Any]:
    emp = day.reference.employees.get(route.staff_id)
    stops = []
    visits = travel = dist = work = 0
    for i, s in enumerate(route.stops):
        d: dict[str, Any] = {
            "seq": i + 1,
            "type": s.type.value,
            "location_id": s.location_id,
            "order_id": s.order_id if s.type is StopType.VISIT else None,
            "arrival": tb.format(s.arrival),
            "start": tb.format(s.start),
            "end": tb.format(s.end),
            "travel_sec": int(s.travel_sec),
            "distance_m": int(s.distance_m),
            "status": s.status_at(as_of).value,
        }
        if s.equipment_after is not None:
            d["equipment_after"] = s.equipment_after.to_api()
        if s.explanation:
            d["explanation"] = s.explanation
        stops.append(d)
        travel += s.travel_sec
        dist += s.distance_m
        if s.type is StopType.VISIT:
            visits += 1
            work += s.end - s.start
    avail = (emp.shift.end - emp.shift.start - emp.shift.break_duration) if emp else 0
    return {
        "staff_id": route.staff_id,
        "service_center_id": route.service_center_id,
        "status": route.status.value,
        "unavailable_from": _t(tb, route.unavailable_from),
        "unavailable_until": _t(tb, route.unavailable_until),
        "stops": stops,
        "totals": {
            "visits": visits,
            "travel_sec": int(travel),
            "distance_m": int(dist),
            "work_min": int(work // 60),
            "utilization": round((work + travel) / avail, 4) if avail > 0 else 0.0,
        },
    }


def unassigned_json(u: UnassignedReason, tb: TimeBase) -> dict[str, Any]:
    return {
        "order_id": u.order_id,
        "reason_code": u.code.value,
        "reason_text": u.text,
        "suggested_action": u.suggested_action.value,
        "proposed_slot": _window(tb, u.proposed_slot),
        "since": _t(tb, u.since),
    }


def pgp_json(g: PgpGroup, tb: TimeBase) -> dict[str, Any]:
    d = {
        "group_id": g.group_id,
        "access_node_id": g.access_node_id,
        "order_ids": list(g.order_ids),
        "detected_at": tb.format(g.detected_at),
        "action": g.action.value,
    }
    if g.comment:
        d["comment"] = g.comment
    return d


def alert_json(a: Alert, tb: TimeBase) -> dict[str, Any]:
    return {
        "alert_id": a.alert_id,
        "created_at": tb.format(a.created_at),
        "type": a.type.value,
        "status": a.status.value,
        "resolved_at": _t(tb, a.resolved_at),
        "order_id": a.order_id,
        "group_id": a.group_id,
        "staff_id": a.staff_id,
        "forecast_start": _t(tb, a.forecast_start),
        "lateness_min": a.lateness_min,
        "suggested_action": a.suggested_action.value,
        "suggested_staff_id": a.suggested_staff_id,
        "proposed_slot": _window(tb, a.proposed_slot),
        "applied": a.applied,
        "message": a.message,
    }


def plan_json(
    plan: Plan, day: DayInstance, tb: TimeBase, travel: TravelTimeProvider | None = None
) -> dict[str, Any]:
    routes = [route_json(plan.routes[sid], day, tb, plan.as_of) for sid in sorted(plan.routes)]
    utils = [r["totals"]["utilization"] for r in routes if r["status"] == "ACTIVE"]
    mean = statistics.fmean(utils) if utils else 0.0
    pgp_orders = sum(len(g.order_ids) for g in plan.pgp_groups)
    visits = sum(r["totals"]["visits"] for r in routes)
    d: dict[str, Any] = {
        "date": plan.date,
        "plan_version": plan.plan_version,
        "based_on_event_id": plan.based_on_event_id,
        "as_of": tb.format(plan.as_of),
        "routes": routes,
        "unassigned": [unassigned_json(plan.unassigned[k], tb) for k in sorted(plan.unassigned)],
        "pgp_groups": [pgp_json(g, tb) for g in plan.pgp_groups],
        "cancelled": [
            {"order_id": c.order_id, "cancelled_at": tb.format(c.cancelled_at), "event_id": c.event_id}
            for _, c in sorted(plan.cancelled.items())
        ],
        "alerts": [alert_json(a, tb) for a in plan.alerts],
        "summary": {
            "orders_total": len(day.orders),
            "visits_planned": visits,
            "unassigned": len(plan.unassigned),
            "pgp_orders": pgp_orders,
            "cancelled": len(plan.cancelled),
            "travel_sec_total": sum(r["totals"]["travel_sec"] for r in routes),
            "utilization_cv": round(statistics.pstdev(utils) / mean, 4) if utils and mean > 0 else 0.0,
        },
    }
    if plan.computation_ms is not None:
        d["computation_ms"] = int(plan.computation_ms)
    return d
