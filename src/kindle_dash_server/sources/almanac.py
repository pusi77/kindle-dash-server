"""``almanac``: moon phase, sunrise/sunset and moonrise/moonset for the configured location
(``astral``, no network). The moon is drawn with its current phase."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
from astral import LocationInfo
from astral import moon as amoon
from astral.sun import sun
from PIL import Image

from ..card import Card
from .base import Source, SourceConfig

CYCLE = 28.0  # astral's moon.phase() runs 0..27.99
PHASES = [
    (1.0, "New moon"),
    (6.0, "Waxing crescent"),
    (8.0, "First quarter"),
    (13.0, "Waxing gibbous"),
    (15.0, "Full moon"),
    (20.0, "Waning gibbous"),
    (22.0, "Last quarter"),
    (27.0, "Waning crescent"),
    (28.0, "New moon"),
]


def phase_name(p: float) -> str:
    return next(name for limit, name in PHASES if p < limit)


def illumination(p: float) -> float:
    return (1 - math.cos(2 * math.pi * p / CYCLE)) / 2


def draw_moon(size: int, p: float, southern: bool = False) -> Image.Image:
    """The moon at phase ``p``: lit part light with soft maria, dark part dark grey."""
    ss = 2
    n = size * ss
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    x = (xx - n / 2 + 0.5) / (n / 2 - ss * 6)
    y = (yy - n / 2 + 0.5) / (n / 2 - ss * 6)
    r2 = x * x + y * y
    inside = r2 <= 1.0
    half = np.sqrt(np.clip(1 - y * y, 0, 1))
    c = math.cos(2 * math.pi * p / CYCLE)
    # Waxing: lit on the right (northern hemisphere); waning: on the left.
    lit = x > c * half if p < CYCLE / 2 else x < -c * half
    if southern:
        lit = lit[:, ::-1]
    # Maria: a few soft dark blotches, fixed so the moon always looks the same.
    rng = np.random.default_rng(7)
    maria = np.zeros_like(x)
    for _ in range(9):
        cx, cy = rng.uniform(-0.6, 0.6, 2)
        rad = rng.uniform(0.12, 0.3)
        maria += np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * rad * rad)) * rng.uniform(0.3, 0.6)
    limb = np.clip(1 - r2, 0, 1) ** 0.25  # slight darkening towards the edge
    lit_v = 255 - np.clip(maria, 0, 1) * 70 - (1 - limb) * 40
    out = np.full((n, n), 255.0, dtype=np.float32)
    out[inside] = np.where(lit[inside], lit_v[inside], 0x33)
    ring = (r2 > 0.985) & (r2 <= 1.0)
    out[ring] = 0
    img = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "L")
    return img.resize((size, size), Image.Resampling.LANCZOS)


def next_phase_day(start: date, target: float) -> date:
    """First day from ``start`` on which the phase passes ``target`` (0 = new, 14 = full)."""
    prev = amoon.phase(start)
    for i in range(1, 32):
        d = start + timedelta(days=i)
        cur = amoon.phase(d)
        crossed = (prev < target <= cur) if target > 0 else cur < prev  # new: wraps to 0
        if crossed:
            return d
        prev = cur
    return start


class AlmanacConfig(SourceConfig):
    type: Literal["almanac"] = "almanac"
    layout: str = "info"
    time_format: str = "%H:%M"
    date_format: str = "%a %-d %b"


class AlmanacSource(Source):
    """Moon phase, sunrise and sunset for the location in Settings."""

    type_name = "almanac"
    Config = AlmanacConfig
    config: AlmanacConfig

    def cache_key(self) -> str:
        return self.ctx.today.isoformat()

    def fetch(self) -> Card:
        loc = self.ctx.settings.location
        tz = ZoneInfo(loc.timezone)
        d = self.ctx.now.astimezone(tz).date()
        obs = LocationInfo(loc.name, "", loc.timezone, loc.latitude, loc.longitude).observer
        tf, df = self.config.time_format, self.config.date_format

        rows: list[tuple[str, str]] = []
        try:
            s = sun(obs, date=d, tzinfo=tz)
            rows += [("Sunrise", s["sunrise"].strftime(tf)), ("Sunset", s["sunset"].strftime(tf))]
            length = s["sunset"] - s["sunrise"]
            h, rem = divmod(int(length.total_seconds()) // 60, 60)
            rows.append(("Day length", f"{h} h {rem:02d} min"))
        except ValueError:  # polar day or night
            rows.append(("Sun", "doesn't rise or set today"))
        for label, fn in (("Moonrise", amoon.moonrise), ("Moonset", amoon.moonset)):
            try:
                t = fn(obs, d, tz)
                if t:
                    rows.append((label, t.strftime(tf)))
            except ValueError:
                pass

        p = amoon.phase(d)
        name = phase_name(p)
        full = next_phase_day(d, 14.0)
        new = next_phase_day(d, 0.0)
        for label, day in sorted((("Full moon", full), ("New moon", new)), key=lambda x: x[1]):
            rows.append((label, _when(day, d, df)))

        size = min(self.ctx.canvas_size) * 2 // 3
        return Card(
            title=name,
            body=f"{illumination(p):.0%} illuminated · {loc.name}",
            image=draw_moon(size, p, southern=loc.latitude < 0),
            credit=f"{loc.latitude:.2f}, {loc.longitude:.2f} · computed with astral",
            source_name=self.config.name,
            layout="info",
            meta={"rows": rows, "kicker": "Almanac", "phase": round(p, 2)},
        )


def _when(day: date, today: date, fmt: str) -> str:
    n = (day - today).days
    if n <= 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return f"{datetime.combine(day, datetime.min.time()).strftime(fmt)} (in {n} days)"
