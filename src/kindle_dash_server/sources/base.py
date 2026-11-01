"""Source base class and the options every source shares.

A source type is a ``Source`` subclass with a ``type_name`` and (optionally) its own ``Config``
model. Subclasses register themselves, so a plugin only has to define the class.
"""

from __future__ import annotations

import hashlib
import io
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..card import Card
from ..config import Duration

if TYPE_CHECKING:
    from ..config import Paths, Settings
    from ..net import HttpClient


class SourceError(Exception):
    """A source couldn't produce a card (network error, nothing matched the filters…)."""


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_width: int | None = None
    min_height: int | None = None
    # aspect = width / height. Portrait screen is 1072/1448 = 0.74.
    min_aspect: float | None = None
    max_aspect: float | None = None
    # How many candidates to try before giving up.
    max_attempts: int = Field(8, ge=1, le=50)

    def check_size(self, width: int | None, height: int | None) -> str | None:
        """Reason the size is rejected, or None. Unknown sizes pass."""
        if not width or not height:
            return None
        if self.min_width and width < self.min_width:
            return f"width {width} < {self.min_width}"
        if self.min_height and height < self.min_height:
            return f"height {height} < {self.min_height}"
        aspect = width / height
        if self.min_aspect and aspect < self.min_aspect:
            return f"aspect {aspect:.2f} < {self.min_aspect}"
        if self.max_aspect and aspect > self.max_aspect:
            return f"aspect {aspect:.2f} > {self.max_aspect}"
        return None


class SourceConfig(BaseModel):
    """Options shared by every source. Type-specific options are added by subclasses."""

    model_config = ConfigDict(extra="forbid")

    type: str
    name: str
    description: str | None = None
    enabled: bool = True
    # "auto": the layout the source suggests, else art (image) or quote/article (text).
    layout: str = "auto"
    layout_options: dict[str, Any] = Field(default_factory=dict)
    # How long a fetched card stays valid before the source is asked again.
    refresh: Duration = 3600
    weight: float = Field(1.0, ge=0)
    filters: Filters = Field(default_factory=Filters)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v.strip()


@dataclass
class SourceContext:
    """What a source gets to work with."""

    http: HttpClient
    paths: Paths
    settings: Settings
    now: datetime

    @property
    def today(self) -> date:
        return self.now.date()

    @property
    def canvas_size(self) -> tuple[int, int]:
        return self.settings.output.canvas_size


SOURCE_TYPES: dict[str, type[Source]] = {}


class Source(ABC):
    """Subclass this, set ``type_name``, implement ``fetch()``.

    ``self.config`` is the validated ``Config`` (common options + yours), ``self.ctx`` gives
    access to the HTTP client (``self.ctx.http``), settings and the current time.
    """

    type_name: ClassVar[str]
    Config: ClassVar[type[SourceConfig]] = SourceConfig
    # Defaults used when a plugin is loaded without a YAML file referencing it.
    defaults: ClassVar[dict[str, Any]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        name = getattr(cls, "type_name", None)
        if name and not getattr(cls, "__abstractmethods__", None):
            SOURCE_TYPES[name] = cls

    def __init__(self, source_id: str, config: SourceConfig, ctx: SourceContext):
        self.id = source_id
        self.config = config
        self.ctx = ctx

    @abstractmethod
    def fetch(self) -> Card:
        """Return a fresh Card. Raise SourceError (or anything) on failure."""

    def cache_key(self) -> str:
        """Cached cards are dropped when this changes (e.g. daily picks: the date)."""
        return ""

    # -- helpers for subclasses --------------------------------------------------------------

    def rng(self, mode: str = "random") -> random.Random:
        """``daily``: same sequence all day for this source; otherwise truly random."""
        if mode == "daily":
            seed = hashlib.sha256(f"{self.id}:{self.ctx.today.isoformat()}".encode()).hexdigest()
            return random.Random(seed)
        return random.Random()

    def load_image(self, data: bytes) -> Image.Image:
        """Decode an image; big JPEGs are decoded at a reduced size (still >= 2x the canvas)."""
        try:
            img = Image.open(io.BytesIO(data))
            w, h = self.ctx.canvas_size
            img.draft("RGB", (w * 2, h * 2))
            img.load()
        except Exception as exc:
            raise SourceError(f"not a usable image: {exc}") from exc
        return img

    def check_image(self, img: Image.Image) -> None:
        reason = self.config.filters.check_size(img.width, img.height)
        if reason:
            raise FilteredOut(reason)


class FilteredOut(SourceError):
    pass
