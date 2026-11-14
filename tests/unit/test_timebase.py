from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ado.domain.timebase import TimeBase, minutes, parse_local_time


def test_roundtrip_and_format(tb: TimeBase) -> None:
    moment = datetime.fromisoformat("2026-10-26T09:18:30+05:00")
    sec = tb.to_seconds(moment)
    assert sec == 9 * 3600 + 18 * 60 + 30
    assert tb.format(sec) == "2026-10-26T09:18:30+05:00"


def test_other_offset_is_normalised(tb: TimeBase) -> None:
    # 04:00 UTC == 09:00 +05:00 — сервис принимает любое корректное смещение
    assert tb.to_seconds(datetime(2026, 10, 26, 4, 0, tzinfo=UTC)) == 9 * 3600


def test_previous_day_is_negative(tb: TimeBase) -> None:
    assert tb.to_seconds(datetime.fromisoformat("2026-10-25T18:40:00+05:00")) == -(5 * 3600 + 20 * 60)


def test_naive_datetime_rejected(tb: TimeBase) -> None:
    with pytest.raises(ValueError):
        tb.to_seconds(datetime(2026, 10, 26, 9, 0))


@pytest.mark.parametrize(("value", "expected"), [("00:00", 0), ("09:00", 32_400), ("23:59", 86_340)])
def test_parse_local_time(value: str, expected: int) -> None:
    assert parse_local_time(value) == expected


@pytest.mark.parametrize("value", ["9:00", "24:00", "12:60", "12-00", "", "ab:cd"])
def test_parse_local_time_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        parse_local_time(value)


def test_minutes() -> None:
    assert minutes(90) == 5400


def test_seconds_precision_truncates_fraction(tb: TimeBase) -> None:
    moment = tb.origin + timedelta(seconds=100, milliseconds=900)
    assert tb.to_seconds(moment) == 100
