"""Перечисления и обязательные поля домена совпадают со spec/api_spec.yaml."""

from __future__ import annotations

from dataclasses import fields
from enum import StrEnum

import pytest

from ado.domain import enums as E
from ado.domain import models as M


def _schema(spec: dict, name: str) -> dict:
    return spec["components"]["schemas"][name]


def _prop(spec: dict, schema: str, prop: str) -> dict:
    s = _schema(spec, schema)
    if "allOf" in s:  # события: EventBase + объект с полями
        for part in s["allOf"]:
            if "properties" in part and prop in part["properties"]:
                return part["properties"][prop]
        raise KeyError(prop)
    return s["properties"][prop]


ENUM_CASES: list[tuple[type[StrEnum], str, str | None]] = [
    (E.Technology, "Technology", None),
    (E.Profile, "Profile", None),
    (E.WorkTypeCode, "WorkTypeCode", None),
    (E.BuildingType, "BuildingType", None),
    (E.EventType, "EventType", None),
    (E.NodeType, "AccessNode", "node_type"),
    (E.DeadlineKind, "WorkType", "deadline_kind"),
    (E.StopType, "Stop", "type"),
    (E.StopStatus, "Stop", "status"),
    (E.RouteStatus, "Route", "status"),
    (E.UnassignedReasonCode, "UnassignedOrder", "reason_code"),
    (E.UnassignedAction, "UnassignedOrder", "suggested_action"),
    (E.AlertType, "Alert", "type"),
    (E.AlertStatus, "Alert", "status"),
    (E.AlertAction, "Alert", "suggested_action"),
    (E.PgpAction, "PgpGroup", "action"),
    (E.CancelReason, "OrderCancelledEvent", "reason"),
    (E.TransferTarget, "OrderTransferredEvent", "transferred_to"),
    (E.UnavailableReason, "StaffUnavailableEvent", "reason"),
    (E.OrderOutcome, "OrderFact", "outcome"),
]


@pytest.mark.parametrize(("enum_cls", "schema", "prop"), ENUM_CASES, ids=lambda x: getattr(x, "__name__", x))
def test_enum_matches_spec(spec: dict, enum_cls: type[StrEnum], schema: str, prop: str | None) -> None:
    node = _schema(spec, schema) if prop is None else _prop(spec, schema, prop)
    assert [m.value for m in enum_cls] == node["enum"]


# Обязательные поля схем спецификации → атрибуты домена. Явные переименования перечислены,
# всё остальное обязано совпадать по имени.
REQUIRED_CASES = [
    ("Order", M.Order, {"address": "address"}),
    ("Staff", M.Employee, {}),
    ("Building", M.Building, {}),
    ("AccessNode", M.AccessNode, {"buildings": "building_ids"}),
    (
        "ServiceCenter",
        M.ServiceCenter,
        {"opening_hours": "opening", "restock_duration_min": "restock_duration"},
    ),
    ("WorkType", M.WorkType, {"duration_min": "duration", "max_start_delay_min": "max_start_delay"}),
    ("Shift", M.Shift, {"break_duration_min": "break_duration"}),
    ("UnassignedOrder", M.UnassignedReason, {"reason_code": "code", "reason_text": "text"}),
    ("PgpGroup", M.PgpGroup, {}),
    ("CancelledOrder", M.CancelledOrder, {}),
    ("Alert", M.Alert, {}),
    ("Route", M.Route, {}),
    ("Stop", M.RouteStop, {"seq": None}),  # seq = позиция в списке
    ("Plan", M.Plan, {}),
]


@pytest.mark.parametrize(
    ("schema", "cls", "renames"), REQUIRED_CASES, ids=lambda x: getattr(x, "__name__", x)
)
def test_domain_covers_required_fields(spec: dict, schema: str, cls: type, renames: dict) -> None:
    attrs = {f.name for f in fields(cls)}
    missing = []
    for name in _schema(spec, schema)["required"]:
        target = renames.get(name, name)
        if target is None:
            continue
        if target not in attrs:
            missing.append(name)
    assert not missing, f"{cls.__name__} не покрывает обязательные поля {schema}: {missing}"
