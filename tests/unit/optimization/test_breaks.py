"""D36: разведение обедов смен внутри входных окон."""

from __future__ import annotations

from ado.domain.enums import Profile, Technology
from ado.domain.equipment import EquipmentInventory
from ado.domain.models import Employee, Shift, TimeWindow
from ado.optimization.breaks import stagger_breaks

H = 3600


def emp(
    sid: str, start: int, end: int, bw: tuple[int, int], prof: Profile = Profile.LTP3, dur: int = H
) -> Employee:
    return Employee(
        staff_id=sid,
        service_center_id="SC-01",
        profiles=frozenset({prof}),
        technologies=frozenset({Technology.PON}),
        height_permit=False,
        shift=Shift(start * H, end * H, dur, TimeWindow(bw[0] * H, bw[1] * H)),
        equipment_start=EquipmentInventory(),
        equipment_capacity=EquipmentInventory(1, 1, 1),
    )


def _bw(e: Employee) -> tuple[float, float]:
    return e.shift.break_window.start / H, e.shift.break_window.end / H


def test_case_shifts_get_disjoint_covered_breaks() -> None:
    """Смены кейса: 09–18 (окно начала 12–15) и 12–21 (15–18), обед 60 мин."""
    staff = {
        "A1": emp("A1", 9, 18, (12, 15)),
        "A2": emp("A2", 9, 18, (12, 15), Profile.INSTALL),
        "B1": emp("B1", 12, 21, (15, 18)),
        "B2": emp("B2", 12, 21, (15, 18), Profile.INSTALL),
    }
    out, plan = stagger_breaks(staff)
    assert plan.ok and plan.changed
    # исходно обед A мог идти 15:00–16:00, а B — с 15:00: одновременно; теперь A кончает обед ≤ 15:00
    assert _bw(out["A1"]) == (12, 14) and _bw(out["A2"]) == (12, 14)
    # обед B должен закончиться, пока A ещё на смене (до 18:00)
    assert _bw(out["B1"]) == (15, 17)
    for e in out.values():  # суженное окно внутри исходного, длительность прежняя
        src = staff[e.staff_id].shift
        assert (
            src.break_window.start
            <= e.shift.break_window.start
            <= e.shift.break_window.end
            <= src.break_window.end
        )
        assert e.shift.break_duration == src.break_duration
    g = plan.to_json()["groups"]
    assert [x["break_busy"] for x in g] == ["12:00–15:00", "15:00–18:00"]


def test_identical_windows_are_split() -> None:
    staff = {"A": emp("A", 9, 18, (12, 15)), "B": emp("B", 10, 19, (12, 15))}
    out, plan = stagger_breaks(staff)
    assert plan.ok
    a, b = _bw(out["A"]), _bw(out["B"])
    assert a[1] + 1 <= b[0]  # обед A (≤ 1 ч) закончен до начала обеда B
    assert a[1] - a[0] == b[1] - b[0] == 1


def test_disjoint_and_covered_windows_unchanged() -> None:
    staff = {"A": emp("A", 9, 18, (12, 13)), "B": emp("B", 10, 19, (14, 15))}
    out, plan = stagger_breaks(staff)
    assert plan.ok and not plan.changed and out == staff


def test_single_shift_is_reported_not_changed() -> None:
    staff = {"A": emp("A", 9, 18, (12, 15)), "B": emp("B", 9, 18, (12, 15))}
    out, plan = stagger_breaks(staff)
    assert not plan.changed and out == staff
    assert "одна смена" in plan.issues[0]


def test_impossible_split_is_reported() -> None:
    # обе смены обязаны начать обед ровно в 12:00 — развести нельзя, входные окна сохраняются
    staff = {"A": emp("A", 9, 18, (12, 12)), "B": emp("B", 10, 19, (12, 12))}
    out, plan = stagger_breaks(staff)
    assert any("нельзя развести" in i for i in plan.issues)
    assert _bw(out["A"]) == (12, 12) and _bw(out["B"]) == (12, 12)


def test_no_other_shift_during_break_is_reported() -> None:
    # B начинает в 14:00 — во время обеда A (12–13) никого нет; сузить нельзя
    staff = {"A": emp("A", 9, 18, (12, 12)), "B": emp("B", 14, 22, (17, 18))}
    out, plan = stagger_breaks(staff)
    assert any("нет другой смены" in i and "09:00–18:00" in i for i in plan.issues)
    assert _bw(out["A"]) == (12, 12)


def test_profile_without_cover_is_reported() -> None:
    staff = {"A": emp("A", 9, 18, (12, 13), Profile.INSTALL), "B": emp("B", 12, 21, (15, 16))}
    _, plan = stagger_breaks(staff)
    assert any("профиля INSTALL" in i for i in plan.issues)
