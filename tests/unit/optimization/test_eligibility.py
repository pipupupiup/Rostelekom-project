from __future__ import annotations

from ado.domain.enums import Profile, Technology, UnassignedReasonCode, WorkTypeCode
from ado.domain.equipment import EquipmentInventory as Eq
from ado.domain.models import TimeWindow
from ado.optimization.eligibility import (
    AvailabilityState,
    EligibilityIndex,
    find_eligible_staff,
)
from ado.optimization.eligibility import (
    ExclusionReason as X,
)
from tests import dom as D

LTP = D.employee("ST-001", techs=(Technology.PON, Technology.FTTB))
LTP_X = D.employee("ST-002", techs=(Technology.XDSL,))
INST = D.installer("ST-101", techs=(Technology.PON,))
INST_H = D.installer("ST-102", techs=(Technology.PON,), height=True)
MIXED = D.employee(
    "ST-201", profiles=(Profile.INSTALL, Profile.LTP3), techs=(Technology.DOCSIS, Technology.PON)
)
ALL = [LTP, LTP_X, INST, INST_H, MIXED]
REF = D.reference(ALL, buildings=["B-00001"])


def check(order, state=None, employees=ALL, travel=None):
    return EligibilityIndex(REF, travel).find_eligible_staff(order, employees, state)


def test_profile_compatibility() -> None:
    r = check(D.order(wt=WorkTypeCode.REPAIR))
    assert r.staff_ids == ["ST-001", "ST-201"]
    assert r.excluded["ST-101"] == (X.PROFILE,)
    assert r.excluded["ST-002"] == (X.TECHNOLOGY,)


def test_technology_and_mixed_qualification() -> None:
    docsis = D.order(wt=WorkTypeCode.REPAIR, tech=Technology.DOCSIS)
    assert check(docsis).staff_ids == ["ST-201"]
    survey = D.order(wt=WorkTypeCode.SURVEY_PRIVATE, tech=Technology.PON)  # INSTALL | LTP3
    assert check(survey).staff_ids == ["ST-001", "ST-101", "ST-102", "ST-201"]


def test_height_permit_for_overhead_line() -> None:
    vls = D.order(wt=WorkTypeCode.INST_PRIVATE_VLS, slot=(D.hm(10), D.hm(12)), height=True)
    r = check(vls)
    assert r.staff_ids == ["ST-102"]
    assert r.excluded["ST-101"] == (X.HEIGHT,)
    assert set(r.excluded["ST-001"]) == {X.PROFILE, X.HEIGHT}


def test_absent_and_temporary_unavailability() -> None:
    o = D.order(slot=(D.hm(10), D.hm(12)))
    state = AvailabilityState(absent=frozenset({"ST-001"}))
    assert check(o, state).excluded["ST-001"] == (X.ABSENT,)
    busy = AvailabilityState(unavailable={"ST-201": (TimeWindow(D.hm(9), D.hm(13)),)})
    assert check(o, busy).excluded["ST-201"] == (X.UNAVAILABLE,)
    partial = AvailabilityState(unavailable={"ST-201": (TimeWindow(D.hm(9), D.hm(11)),)})
    assert "ST-201" in check(o, partial).staff_ids  # после 11:00 можно начать в слоте


def test_shift_does_not_cover_slot() -> None:
    late = D.employee("ST-003", shift=(D.hm(12), D.hm(21)))
    o = D.order(slot=(D.hm(9), D.hm(11)))
    assert check(o, employees=[late]).excluded["ST-003"] == (X.SHIFT,)
    # с дорогой 40 мин обратно: последний старт ремонта (60 мин) — 18:00 − 100 мин = 16:20
    travel = D.matrix(["SC-01", "B-00001"], default=40 * 60)
    evening = D.order(slot=(D.hm(16, 30), D.hm(17, 30)))
    assert check(evening, employees=[LTP], travel=travel).excluded["ST-001"] == (X.SHIFT,)
    assert check(evening, employees=[LTP]).staff_ids == ["ST-001"]  # без дороги — укладывается
    fits = D.order(slot=(D.hm(16), D.hm(17)))
    assert check(fits, employees=[LTP], travel=travel).staff_ids == ["ST-001"]


def test_equipment_restock_vs_capacity() -> None:
    need = Eq(4, 0, 0)  # в рюкзаке 3, вместимость 6 → можно с пополнением
    o = D.order(equipment=need)
    c = {c.staff_id: c for c in check(o).eligible}
    assert c["ST-001"].needs_restock
    too_much = D.order(equipment=Eq(7, 0, 0))
    r = check(too_much)
    assert r.eligible == () and r.excluded["ST-001"] == (X.EQUIPMENT_CAPACITY,)
    reason = EligibilityIndex(REF).explain(too_much, r)
    assert reason.code is UnassignedReasonCode.NO_EQUIPMENT


def test_no_suitable_staff_reason() -> None:
    o = D.order(wt=WorkTypeCode.REPAIR, tech=Technology.DOCSIS)
    r = check(o, employees=[LTP, INST])
    reason = EligibilityIndex(REF).explain(o, r, since=D.hm(8, 30))
    assert reason.code is UnassignedReasonCode.NO_QUALIFIED_STAFF
    assert "DOCSIS" in reason.text and "технология" in reason.text and reason.since == D.hm(8, 30)
    state = AvailabilityState(absent=frozenset({"ST-201"}))
    r2 = check(o, state)
    assert EligibilityIndex(REF).explain(o, r2).code is UnassignedReasonCode.STAFF_UNAVAILABLE


def test_static_cache_reused() -> None:
    idx = EligibilityIndex(REF)
    for i in range(5):
        idx.find_eligible_staff(D.order(f"O{i}"), ALL)
    assert idx.static_cache_hits == 4
    assert find_eligible_staff(D.order(), ALL, None, REF).staff_ids == ["ST-001", "ST-201"]
