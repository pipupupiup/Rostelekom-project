from __future__ import annotations

from datetime import date, datetime

import pytest

from ado.data.calendar import DEFAULT_CALENDAR, ProductionCalendar, compute_deadline
from ado.data.prepare import prepare_reference
from ado.domain.enums import WorkTypeCode
from tests import factories as F

TZ = "+05:00"


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s + TZ)


@pytest.fixture(scope="module")
def wts():
    return prepare_reference(F.reference()).work_types


def test_spec_example_three_working_days(wts) -> None:
    # пример спецификации: регистрация чт 22.10 14:12 → срок вт 27.10 14:12
    wt = wts[WorkTypeCode.INST_PON_MKD]
    assert compute_deadline(wt, dt("2026-10-22T14:12:00"), dt("2026-10-26T13:10:00"), False) == dt(
        "2026-10-27T14:12:00"
    )


def test_working_days_skip_holiday_nov4(wts) -> None:
    # пн 2.11 + 3 р. д.: вт 3, (ср 4 — праздник), чт 5, пт 6
    wt = wts[WorkTypeCode.INST_FTTB_MKD]
    assert compute_deadline(wt, dt("2026-11-02T10:00:00"), dt("2026-11-02T10:00:00"), False) == dt(
        "2026-11-06T10:00:00"
    )


def test_working_days_across_month_and_weekend(wts) -> None:
    wt = wts[WorkTypeCode.INST_PRIVATE_VLS]  # 5 р. д.
    # пт 30.10 → пн 2, вт 3, чт 5, пт 6, пн 9
    assert compute_deadline(wt, dt("2026-10-30T17:00:00"), dt("2026-10-30T17:00:00"), False) == dt(
        "2026-11-09T17:00:00"
    )


def test_registration_on_weekend(wts) -> None:
    wt = wts[WorkTypeCode.SURVEY_PRIVATE]  # 2 р. д.
    assert compute_deadline(wt, dt("2026-10-24T11:00:00"), dt("2026-10-24T11:00:00"), False) == dt(
        "2026-10-27T11:00:00"
    )


def test_hours_12_24_48_are_distinct(wts) -> None:
    reg = dt("2026-10-26T10:05:00")
    repair = wts[WorkTypeCode.REPAIR]
    degradation = wts[WorkTypeCode.DEGRADATION]
    assert compute_deadline(repair, reg, reg, False) == dt("2026-10-27T10:05:00")  # 24 ч
    assert compute_deadline(repair, reg, reg, True) == dt("2026-10-26T22:05:00")  # повторное — 12 ч
    assert compute_deadline(degradation, reg, reg, False) == dt("2026-10-28T10:05:00")  # 48 ч
    assert compute_deadline(degradation, reg, reg, True) == dt("2026-10-28T10:05:00")  # нет repeat-срока


def test_hours_cross_midnight_and_slot_kind(wts) -> None:
    repair = wts[WorkTypeCode.REPAIR]
    assert compute_deadline(repair, dt("2026-10-31T23:30:00"), dt("2026-11-01T10:00:00"), True) == dt(
        "2026-11-01T11:30:00"
    )
    add = wts[WorkTypeCode.ADD_SERVICE]  # SLOT: latest_start + норматив
    assert compute_deadline(add, dt("2026-10-20T10:00:00"), dt("2026-10-26T13:10:00"), False) == dt(
        "2026-10-26T13:50:00"
    )


def test_calendar_basics() -> None:
    assert not DEFAULT_CALENDAR.is_working_day(date(2026, 11, 4))
    assert not DEFAULT_CALENDAR.is_working_day(date(2026, 1, 9))
    assert DEFAULT_CALENDAR.is_working_day(date(2026, 10, 26))
    custom = ProductionCalendar.from_dates([], working_weekends=[date(2026, 10, 24)])
    assert custom.is_working_day(date(2026, 10, 24))
    assert DEFAULT_CALENDAR.next_working_day(date(2026, 11, 3)) == date(2026, 11, 5)
    with pytest.raises(ValueError):
        DEFAULT_CALENDAR.add_working_days(dt("2026-10-26T10:00:00"), -1)
