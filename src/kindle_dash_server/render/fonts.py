"""Font families: bundled ones (OFL) plus any dropped in /data/fonts/<family>/.

A family is a folder with ``regular.ttf`` and optionally ``italic.ttf`` and ``bold.ttf``
(``.otf`` works too). Missing styles fall back to regular.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import ImageFont

from ..config import BUNDLED_FONTS_DIR

Style = Literal["regular", "italic", "bold"]
_extra_dirs: list[Path] = []


def add_font_dir(path: Path) -> None:
    if path not in _extra_dirs:
        _extra_dirs.insert(0, path)
        available_families.cache_clear()
        _font_file.cache_clear()


def _family_dirs() -> list[Path]:
    return [*_extra_dirs, BUNDLED_FONTS_DIR]


@lru_cache
def available_families() -> list[str]:
    names: set[str] = set()
    for base in _family_dirs():
        if base.is_dir():
            for d in base.iterdir():
                if d.is_dir() and _find(d, "regular"):
                    names.add(d.name)
    return sorted(names)


def _find(folder: Path, style: str) -> Path | None:
    for ext in (".ttf", ".otf"):
        p = folder / f"{style}{ext}"
        if p.exists():
            return p
    return None


@lru_cache
def _font_file(family: str, style: Style) -> Path:
    for base in _family_dirs():
        folder = base / family
        if folder.is_dir():
            p = _find(folder, style) or _find(folder, "regular")
            if p:
                return p
    if family != "literata":
        return _font_file("literata", style)
    raise FileNotFoundError("bundled font 'literata' is missing")


@lru_cache(maxsize=512)
def get_font(family: str, style: Style, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_font_file(family, style)), max(1, int(size)))
