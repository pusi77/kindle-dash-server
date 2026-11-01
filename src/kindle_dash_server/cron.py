"""5-field cron expressions (``minute hour day-of-month month day-of-week``), as used by
kindle-dash-pw3's REFRESH_SCHEDULE, and the device's fetch timeline derived from them.

Supports ``*``, numbers, ranges ``a-b``, steps ``*/n`` and ``a-b/n``, lists ``a,b``, month and
weekday names (``JAN``, ``MON-FRI``) and Sunday as 0 or 7. As in standard cron, when both
day-of-month and day-of-week are restricted, a day matching either one matches.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}
_DAYS = {d: i for i, d in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}


class CronError(ValueError):
    pass


def _parse_field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    values: set[int] = set()

    def num(s: str) -> int:
        s = s.strip().upper()
        if names and s in names:
            return names[s]
        if not s.isdigit():
            raise CronError(f"invalid value {s!r}")
        return int(s)

    for part in text.split(","):
        if not part:
            raise CronError(f"empty list item in {text!r}")
        rng, _, step_s = part.partition("/")
        step = num(step_s) if step_s else 1
        if step < 1:
            raise CronError("step must be >= 1")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = num(a), num(b)
        else:
            start = num(rng)
            end = hi if step_s else start
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise CronError(f"{part!r} out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_any: bool
    dow_any: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        fields = expr.split()
        if len(fields) != 5:
            raise CronError(f"expected 5 fields (min hour dom month dow), got {len(fields)}")
        m, h, dom, mon, dow = fields
        weekdays = _parse_field(dow, 0, 7, _DAYS)
        if 7 in weekdays:
            weekdays = (weekdays - {7}) | {0}
        return cls(
            expr=" ".join(fields),
            minutes=frozenset(_parse_field(m, 0, 59)),
            hours=frozenset(_parse_field(h, 0, 23)),
            days=frozenset(_parse_field(dom, 1, 31)),
            months=frozenset(_parse_field(mon, 1, 12, _MONTHS)),
            weekdays=frozenset(weekdays),
            dom_any=dom == "*",
            dow_any=dow == "*",
        )

    def _day_ok(self, d: datetime) -> bool:
        dom_ok = d.day in self.days
        dow_ok = (d.isoweekday() % 7) in self.weekdays
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def next_after(self, t: datetime) -> datetime:
        """First matching minute strictly after ``t`` (aware datetime, wall-clock matching)."""
        tz = t.tzinfo
        c = t.replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
        limit = c + timedelta(days=366 * 5)
        while c < limit:
            if c.month not in self.months:
                c = (c.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(c):
                c = c.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if c.hour not in self.hours:
                c = c.replace(minute=0) + timedelta(hours=1)
                continue
            if c.minute not in self.minutes:
                c += timedelta(minutes=1)
                continue
            result = c.replace(tzinfo=tz)
            if tz is not None and _nonexistent(result):
                c += timedelta(minutes=1)  # skipped by a DST jump
                continue
            return result
        raise CronError(f"{self.expr!r} never matches")


def _nonexistent(t: datetime) -> bool:
    utc = t.astimezone(ZoneInfo("UTC"))
    return utc.astimezone(t.tzinfo).replace(tzinfo=None) != t.replace(tzinfo=None)


def typical_interval(cron: Cron, start: datetime, samples: int = 200) -> float:
    """Most common gap between consecutive slots, in seconds."""
    gaps: Counter[float] = Counter()
    t = cron.next_after(start)
    for _ in range(samples):
        n = cron.next_after(t)
        gaps[n.timestamp() - t.timestamp()] += 1
        t = n
    return gaps.most_common(1)[0][0]


@dataclass(frozen=True)
class DeviceSchedule:
    """The device's refresh timeline, mirroring kindle-dash-pw3's dash.sh main loop.

    The Kindle wakes at every cron slot. If the slot after that one is more than
    ``sleep_after`` seconds away it shows the sleep screen instead of fetching (that's how
    ACTIVE_HOURS works), otherwise it fetches the image.
    """

    cron: Cron
    timezone: str
    sleep_after: float

    @classmethod
    def create(
        cls, schedule: str, timezone: str, sleep_after: float | None = None
    ) -> DeviceSchedule:
        cron = Cron.parse(schedule)
        tz = ZoneInfo(timezone)
        if not sleep_after:
            # dash.sh default: INTERVAL_MINUTES * 60 + 60
            sleep_after = typical_interval(cron, datetime.now(tz)) + 60
        schedule_ = cls(cron, timezone, float(sleep_after))
        schedule_.next_fetch(datetime.now(tz))  # raises if the device would never fetch
        return schedule_

    def wakes(self, after: datetime) -> Iterator[datetime]:
        t = after.astimezone(ZoneInfo(self.timezone))
        while True:
            t = self.cron.next_after(t)
            yield t

    def fetches(self, after: datetime) -> Iterator[datetime]:
        """Wake-ups at which the device fetches a new image (not the sleep screen).

        Raises CronError if a whole week passes without one: then every wake-up is followed
        by a gap longer than ``sleep_after`` and the Kindle only ever shows its sleep screen
        (e.g. REFRESH_SCHEDULE every 30 min with SLEEP_SCREEN_INTERVAL=960).
        """
        last_fetch = after.timestamp()
        for wake in self.wakes(after):
            following = self.cron.next_after(wake)
            if following.timestamp() - wake.timestamp() <= self.sleep_after:
                last_fetch = wake.timestamp()
                yield wake
            elif wake.timestamp() - last_fetch > 8 * 86400:
                raise CronError(
                    f"with {self.cron.expr!r} and a sleep screen after {self.sleep_after:g} s "
                    "the Kindle never fetches: every refresh is followed by a longer gap"
                )

    def next_fetch(self, after: datetime) -> datetime:
        return next(self.fetches(after))
