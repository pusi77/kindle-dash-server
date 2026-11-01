"""Cron parsing and the Kindle's fetch timeline (mirrors kindle-dash-pw3's dash.sh loop)."""

from __future__ import annotations

from datetime import datetime
from itertools import islice
from zoneinfo import ZoneInfo

import pytest

from kindle_dash_server.cron import Cron, CronError, DeviceSchedule

TZ = ZoneInfo("Europe/Rome")


def t(*args) -> datetime:
    return datetime(*args, tzinfo=TZ)


def hm(ts) -> list[str]:
    return [x.strftime("%a %H:%M") for x in ts]


@pytest.mark.parametrize(
    "expr",
    [
        "* *",
        "61 * * * *",
        "* 24 * * *",
        "* * * * FUNDAY",
        "*/0 * * * *",
        "5-1 * * * *",
        "a b c d e",
        ",1 * * * *",
    ],
)
def test_invalid(expr):
    with pytest.raises(CronError):
        Cron.parse(expr)


def test_fields():
    c = Cron.parse("*/15 7-22 * * MON-FRI")
    assert c.minutes == {0, 15, 30, 45}
    assert c.hours == set(range(7, 23))
    assert c.weekdays == {1, 2, 3, 4, 5}
    assert Cron.parse("0 9 * jan,DEC 7").weekdays == {0}  # 7 = Sunday
    assert Cron.parse("5/20 * * * *").minutes == {5, 25, 45}


def test_next_after_is_strictly_after():
    c = Cron.parse("*/15 * * * *")
    assert c.next_after(t(2026, 11, 2, 7, 0)) == t(2026, 11, 2, 7, 15)
    assert c.next_after(t(2026, 11, 2, 7, 14, 59)) == t(2026, 11, 2, 7, 15)
    assert c.next_after(t(2026, 12, 31, 23, 50)) == t(2027, 1, 1, 0, 0)


def test_day_of_month_or_day_of_week():
    # Standard cron: both restricted -> either matches.
    c = Cron.parse("0 12 1 * MON")
    nxt = list(islice(DeviceSchedule(c, "Europe/Rome", 10**9).wakes(t(2026, 11, 1, 13)), 3))
    assert [x.day for x in nxt] == [2, 9, 16]  # Mondays; Dec 1 comes later
    assert Cron.parse("0 0 31 2 *").expr  # parses; never matches
    with pytest.raises(CronError, match="never"):
        Cron.parse("0 0 31 2 *").next_after(t(2026, 1, 1))


def test_dst_gap_is_skipped():
    c = Cron.parse("30 * * * *")
    # Rome, 2027-03-28: 02:00 jumps to 03:00, so 02:30 doesn't exist.
    assert c.next_after(t(2027, 3, 28, 1, 40)).isoformat() == "2027-03-28T03:30:00+02:00"


def test_device_skips_the_sleep_screen_slot():
    # INTERVAL_MINUTES=15, ACTIVE_HOURS=7-22: the 22:45 wake-up shows sleeping.png.
    s = DeviceSchedule.create("*/15 7-22 * * *", "Europe/Rome")
    assert s.sleep_after == 15 * 60 + 60
    assert hm(islice(s.fetches(t(2026, 11, 2, 22, 10)), 4)) == [
        "Mon 22:15",
        "Mon 22:30",
        "Tue 07:00",
        "Tue 07:15",
    ]


def test_device_weekdays_only():
    s = DeviceSchedule.create("*/30 7-22 * * MON-FRI", "Europe/Rome")
    # Fri 22:30's next slot is Monday: sleep screen, no fetch.
    assert hm(islice(s.fetches(t(2026, 10, 30, 21, 50)), 2)) == ["Fri 22:00", "Mon 07:00"]


def test_device_hourly_and_explicit_sleep_after():
    s = DeviceSchedule.create("0 * * * *", "Europe/Rome")
    assert s.sleep_after == 3660
    assert hm(islice(s.fetches(t(2026, 11, 2, 23, 30)), 2)) == ["Tue 00:00", "Tue 01:00"]
    # A SLEEP_SCREEN_INTERVAL shorter than the interval: the Kindle never fetches.
    with pytest.raises(CronError, match="never fetches"):
        DeviceSchedule.create("*/30 * * * *", "Europe/Rome", sleep_after=960)


def test_device_timezone_differs_from_server():
    s = DeviceSchedule.create("0 8 * * *", "America/New_York")
    nxt = s.next_fetch(t(2026, 11, 2, 12, 0))  # 12:00 in Rome = 06:00 in New York
    assert nxt.astimezone(ZoneInfo("America/New_York")).hour == 8
    assert nxt.astimezone(TZ).hour == 14
