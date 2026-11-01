"""Configuration.

Two layers:

* ``Env``: bootstrap options read from environment variables (``KDS_*``): where the data volume
  is, optional basic auth. These can't be changed from the UI.
* ``Settings``: runtime settings stored in ``/data/settings.yaml`` and editable from the web UI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from .fsutil import atomic_write

PACKAGE_DIR = Path(__file__).resolve().parent
BUNDLED_FONTS_DIR = PACKAGE_DIR / "fonts"
DEFAULTS_DIR = PACKAGE_DIR / "defaults"

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: object) -> float:
    """``90``, ``"90s"``, ``"15m"``, ``"6h"``, ``"1d"`` -> seconds."""
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        m = _DURATION_RE.match(value)
        if m:
            return float(m.group(1)) * _UNITS[m.group(2)]
    raise ValueError(f"invalid duration {value!r} (use e.g. 90, '90s', '15m', '6h', '1d')")


Duration = Annotated[float, BeforeValidator(parse_duration)]


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KDS_")

    data_dir: Path = Path("/data")
    # "user:password" enables HTTP basic auth on the web UI (the device endpoint and /healthz
    # stay open; protect the device endpoint with a token instead).
    basic_auth: str | None = None
    log_level: str = "INFO"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputSettings(StrictModel):
    # kindle-dash-pw3 draws the PNG 1:1 from the top-left corner: it must be exactly this size.
    width: int = Field(1072, ge=16, le=4096)
    height: int = Field(1448, ge=16, le=4096)
    # Clockwise rotation applied last. 90/270 make the layouts render a landscape canvas, for a
    # device mounted sideways.
    rotation: Annotated[Literal[0, 90, 180, 270], BeforeValidator(int)] = 0
    gray_levels: int = Field(16, ge=2, le=256)
    dither: Literal["floyd_steinberg", "atkinson", "ordered", "none"] = "floyd_steinberg"
    # Tone adjustments for photographic content (card images), not for text.
    autocontrast: bool = True
    # % of darkest / lightest pixels clipped to black / white.
    black_cutoff: float = Field(0.5, ge=0, le=40)
    white_cutoff: float = Field(1.0, ge=0, le=40)
    # Detect the paper tone of prints/drawings and map it to white.
    paper_white: bool = True
    gamma: float = Field(1.0, gt=0.1, le=5)

    @property
    def canvas_size(self) -> tuple[int, int]:
        """Size the layouts draw at, before rotation."""
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height


class DeviceSettings(StrictModel):
    path: str = "/dash.png"
    # If set, the device must send "Authorization: Bearer <token>" (DASH_TOKEN in env.sh).
    token: str | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("/") or len(v) < 2:
            raise ValueError("device path must start with '/' and not be '/'")
        reserved = ("/static", "/preview", "/sources", "/settings", "/healthz", "/render-now")
        if v.startswith(reserved) or v in ("/current.png",):
            raise ValueError(f"device path {v!r} clashes with a built-in route")
        return v

    @field_validator("token")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class LocationSettings(StrictModel):
    name: str = "Rome"
    latitude: float = Field(41.9028, ge=-90, le=90)
    longitude: float = Field(12.4964, ge=-180, le=180)
    timezone: str = "Europe/Rome"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _check_tz(v.strip())


def _check_tz(v: str | None) -> str | None:
    if v:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"unknown timezone {v!r} (e.g. Europe/Rome)") from None
    return v


class SchedulerSettings(StrictModel):
    """When to render: shortly before each time the Kindle will fetch.

    With ``follow_device`` the schedule comes from the Kindle itself (kindle-dash-pw3 sends
    X-Kindle-Schedule / X-Kindle-Timezone / X-Kindle-Sleep-After). Until it has been seen, or
    with ``follow_device: false``, the values below are used: copy them from env.sh.
    """

    follow_device: bool = True
    # Same syntax as REFRESH_SCHEDULE: "*/INTERVAL_MINUTES ACTIVE_HOURS * * *".
    schedule: str = "*/15 7-22 * * *"
    # TIMEZONE from env.sh. Empty: location.timezone.
    timezone: str | None = None
    # SLEEP_SCREEN_INTERVAL: a wake-up whose next slot is further away shows the sleep screen
    # instead of fetching. Empty: the schedule's usual interval + 60 s (dash.sh's default).
    sleep_after: int | None = Field(None, ge=60)
    # How long before each fetch to render.
    lead_seconds: int = Field(60, ge=5, le=3600)

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "interval_minutes"}
        return data

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, v: str) -> str:
        from .cron import Cron

        return Cron.parse(v).expr

    @model_validator(mode="after")
    def _fetches(self) -> SchedulerSettings:
        from .cron import DeviceSchedule

        DeviceSchedule.create(self.schedule, self.timezone or "UTC", self.sleep_after)
        return self

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str | None) -> str | None:
        return _check_tz(v.strip() if v else None)


_HHMM = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")


class ScheduleSlot(StrictModel):
    """In schedule mode: during ``start``-``end`` on ``days``, show ``source`` (empty: rotation).
    The first matching slot wins; times outside every slot use the rotation."""

    days: str = "*"  # cron day-of-week syntax: "*", "MON-FRI", "SAT,SUN"
    start: str = "00:00"
    end: str = "24:00"
    source: str | None = None

    @field_validator("days")
    @classmethod
    def _days(cls, v: str) -> str:
        from .cron import Cron

        v = v.strip() or "*"
        Cron.parse(f"* * * * {v}")
        return v.upper() if v != "*" else v

    @field_validator("start", "end")
    @classmethod
    def _time(cls, v: str) -> str:
        m = _HHMM.match(v.strip())
        if not m or (m.group(1) == "24" and m.group(2) != "00"):
            raise ValueError("use HH:MM, 00:00-24:00")
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    @field_validator("source")
    @classmethod
    def _source(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None

    def matches(self, t: Any) -> bool:
        from .cron import Cron

        if not Cron.parse(f"* * * * {self.days}")._day_ok(t):
            return False
        minute = t.hour * 60 + t.minute
        s = int(self.start[:2]) * 60 + int(self.start[3:])
        e = int(self.end[:2]) * 60 + int(self.end[3:])
        if s <= e:
            return s <= minute < e
        return minute >= s or minute < e  # crosses midnight


class SelectionSettings(StrictModel):
    mode: Literal["pinned", "rotation", "schedule"] = "rotation"
    pinned: str | None = None
    strategy: Literal["weighted_random", "round_robin"] = "weighted_random"
    # Empty = every enabled source.
    rotation_sources: list[str] = Field(default_factory=list)
    slots: list[ScheduleSlot] = Field(default_factory=list)


class BatterySettings(StrictModel):
    # Draw a battery badge when the level the Kindle reports (X-Kindle-Battery) is at or below
    # this. 0 disables it.
    show_below: int = Field(20, ge=0, le=100)
    position: Literal["top-right", "top-left", "bottom-right", "bottom-left"] = "top-right"


class Settings(StrictModel):
    output: OutputSettings = Field(default_factory=OutputSettings)
    device: DeviceSettings = Field(default_factory=DeviceSettings)
    location: LocationSettings = Field(default_factory=LocationSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    selection: SelectionSettings = Field(default_factory=SelectionSettings)
    battery: BatterySettings = Field(default_factory=BatterySettings)
    font: str = "literata"
    # Added to the User-Agent sent to public APIs, so operators can reach you (e.g. an email).
    contact: str = ""


class SettingsStore:
    """settings.yaml on the data volume."""

    def __init__(self, path: Path):
        self.path = path
        self._settings = self._load()

    def _load(self) -> Settings:
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text()) or {}
            return Settings.model_validate(data)
        return Settings()

    @property
    def settings(self) -> Settings:
        return self._settings

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False))
        self._settings = settings

    def update(self, **sections: object) -> Settings:
        data = self._settings.model_dump()
        data.update(sections)
        new = Settings.model_validate(data)
        self.save(new)
        return new


class Paths:
    """Layout of the /data volume."""

    def __init__(self, root: Path):
        self.root = root
        self.sources = root / "sources"
        self.plugins = root / "plugins"
        self.fonts = root / "fonts"
        self.cache = root / "cache"
        self.http_cache = self.cache / "http"
        self.cards = self.cache / "cards"
        self.previews = self.cache / "previews"
        self.current_png = self.cache / "current.png"
        self.settings = root / "settings.yaml"
        self.state_db = root / "state.sqlite3"

    def ensure(self) -> None:
        for d in (
            self.sources,
            self.plugins,
            self.fonts,
            self.http_cache,
            self.cards,
            self.previews,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str) -> Path:
        """Resolve a path from a source config: ``builtin:x`` is bundled, relative is in /data."""
        if path.startswith("builtin:"):
            return DEFAULTS_DIR / path.removeprefix("builtin:")
        p = Path(path)
        return p if p.is_absolute() else self.root / p
