"""Configuration.

Two layers:

* ``Env``: bootstrap options read from environment variables (``KDS_*``): where the data volume
  is, optional basic auth. These can't be changed from the UI.
* ``Settings``: runtime settings stored in ``/data/settings.yaml`` and editable from the web UI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    rotation: Literal[0, 90, 180, 270] = 0
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
        from zoneinfo import ZoneInfo

        ZoneInfo(v)  # raises if unknown
        return v


class SchedulerSettings(StrictModel):
    # Render on clock-aligned slots (like the device's INTERVAL_MINUTES), lead_seconds early so
    # the image is ready when the Kindle wakes up.
    interval_minutes: int = Field(15, ge=1, le=1440)
    lead_seconds: int = Field(60, ge=0, le=3600)


class SelectionSettings(StrictModel):
    mode: Literal["pinned", "rotation", "schedule"] = "rotation"
    pinned: str | None = None
    strategy: Literal["weighted_random", "round_robin"] = "weighted_random"
    # Empty = every enabled source.
    rotation_sources: list[str] = Field(default_factory=list)


class Settings(StrictModel):
    output: OutputSettings = Field(default_factory=OutputSettings)
    device: DeviceSettings = Field(default_factory=DeviceSettings)
    location: LocationSettings = Field(default_factory=LocationSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    selection: SelectionSettings = Field(default_factory=SelectionSettings)
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
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False))
        tmp.replace(self.path)
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
