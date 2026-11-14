"""Перечисления домена.

Значения строго совпадают с `spec/api_spec.yaml` (п. 7 спецификации: «значения перечислений —
строго как в спецификации»). Соответствие проверяется тестом `tests/unit/test_spec_enums.py`.
"""

from __future__ import annotations

from enum import StrEnum


class Technology(StrEnum):
    PON = "PON"
    FTTB = "FTTB"
    XDSL = "XDSL"
    DOCSIS = "DOCSIS"


class Profile(StrEnum):
    INSTALL = "INSTALL"
    LTP3 = "LTP3"


class WorkTypeCode(StrEnum):
    INST_PON_MKD = "INST_PON_MKD"
    INST_FTTB_MKD = "INST_FTTB_MKD"
    INST_PRIVATE_VLS = "INST_PRIVATE_VLS"
    ADD_SERVICE = "ADD_SERVICE"
    SURVEY_PRIVATE = "SURVEY_PRIVATE"
    REPAIR = "REPAIR"
    DEGRADATION = "DEGRADATION"
    DISMANTLE = "DISMANTLE"


class BuildingType(StrEnum):
    MKD = "MKD"
    PRIVATE = "PRIVATE"


class NodeType(StrEnum):
    OLT = "OLT"
    SWITCH = "SWITCH"
    DSLAM = "DSLAM"
    CMTS = "CMTS"


class DeadlineKind(StrEnum):
    SLOT = "SLOT"
    HOURS = "HOURS"
    WORKING_DAYS = "WORKING_DAYS"


class StopType(StrEnum):
    DEPOT_START = "DEPOT_START"
    VISIT = "VISIT"
    BREAK = "BREAK"
    RESTOCK = "RESTOCK"
    DEPOT_END = "DEPOT_END"


class StopStatus(StrEnum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


class RouteStatus(StrEnum):
    ACTIVE = "ACTIVE"
    UNAVAILABLE = "UNAVAILABLE"


class UnassignedReasonCode(StrEnum):
    NO_QUALIFIED_STAFF = "NO_QUALIFIED_STAFF"
    NO_SHIFT_TIME = "NO_SHIFT_TIME"
    SLOT_UNREACHABLE = "SLOT_UNREACHABLE"
    NO_EQUIPMENT = "NO_EQUIPMENT"
    STAFF_UNAVAILABLE = "STAFF_UNAVAILABLE"
    OTHER = "OTHER"


class UnassignedAction(StrEnum):
    """`UnassignedOrder.suggested_action`."""

    RESCHEDULE = "RESCHEDULE"
    REASSIGN = "REASSIGN"
    ESCALATE = "ESCALATE"


class AlertType(StrEnum):
    LATE_RISK = "LATE_RISK"
    PGP_DETECTED = "PGP_DETECTED"
    UNASSIGNED = "UNASSIGNED"


class AlertStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class AlertAction(StrEnum):
    """`Alert.suggested_action`."""

    REASSIGN = "REASSIGN"
    RESCHEDULE = "RESCHEDULE"
    TRANSFER_TO_STATION = "TRANSFER_TO_STATION"
    NONE = "NONE"


class PgpAction(StrEnum):
    TRANSFER_TO_STATION = "TRANSFER_TO_STATION"


class EventType(StrEnum):
    NEW_ORDER = "NEW_ORDER"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    VISIT_DELAYED = "VISIT_DELAYED"
    ORDER_TRANSFERRED = "ORDER_TRANSFERRED"
    STAFF_UNAVAILABLE = "STAFF_UNAVAILABLE"
    TIME_TICK = "TIME_TICK"


class CancelReason(StrEnum):
    NOT_CONFIRMED = "NOT_CONFIRMED"
    CUSTOMER_REQUEST = "CUSTOMER_REQUEST"


class TransferTarget(StrEnum):
    STATION_EQUIPMENT = "STATION_EQUIPMENT"
    TRUNK_CABLE = "TRUNK_CABLE"


class UnavailableReason(StrEnum):
    VEHICLE_BREAKDOWN = "VEHICLE_BREAKDOWN"
    ILLNESS = "ILLNESS"
    OTHER = "OTHER"


class OrderOutcome(StrEnum):
    """`OrderFact.outcome` — только в обучающем наборе."""

    COMPLETED = "COMPLETED"
    TRANSFERRED_TO_STATION = "TRANSFERRED_TO_STATION"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    PGP_GROUPED = "PGP_GROUPED"


class ConstraintCode(StrEnum):
    """Жёсткие ограничения H1–H10 из п. 4 спецификации + структурные правила маршрута."""

    H1_QUALIFICATION = "H1"
    H2_SHIFT = "H2"
    H3_BREAK = "H3"
    H4_SLOT = "H4"
    H5_DURATION = "H5"
    H6_TRAVEL = "H6"
    H7_EQUIPMENT = "H7"
    H8_SERVICE_CENTER = "H8"
    H9_ACCOUNTING = "H9"
    H10_FREEZE = "H10"
    STRUCTURE = "STRUCTURE"  # первая/последняя остановка, BREAK на месте предыдущей и т. п.
