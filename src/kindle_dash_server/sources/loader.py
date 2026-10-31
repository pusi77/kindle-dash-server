"""Load source definitions from /data/sources/*.yaml (id = file name without extension)."""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from ..config import DEFAULTS_DIR
from .base import SOURCE_TYPES, SourceConfig

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SourceDefinitionError(ValueError):
    pass


def format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    return "\n".join(parts)


def parse_source(text: str) -> SourceConfig:
    """YAML text -> validated config of the right type. Raises SourceDefinitionError."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SourceDefinitionError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceDefinitionError("a source must be a YAML mapping (key: value)")
    type_name = data.get("type")
    if not type_name:
        raise SourceDefinitionError(f"missing 'type' (one of: {', '.join(sorted(SOURCE_TYPES))})")
    cls = SOURCE_TYPES.get(type_name)
    if cls is None:
        raise SourceDefinitionError(
            f"unknown type {type_name!r} (known: {', '.join(sorted(SOURCE_TYPES))})"
        )
    try:
        return cls.Config.model_validate(data)
    except ValidationError as exc:
        raise SourceDefinitionError(format_validation_error(exc)) from exc


@dataclass
class LoadResult:
    configs: dict[str, SourceConfig] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    files: dict[str, Path] = field(default_factory=dict)


def source_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("*.y*ml") if p.suffix in (".yaml", ".yml"))


def load_sources(folder: Path) -> LoadResult:
    res = LoadResult()
    for path in source_files(folder):
        sid = path.stem
        res.files[sid] = path
        if not ID_RE.match(sid):
            res.errors[sid] = "file name must be lowercase letters, digits, '-' or '_'"
            continue
        try:
            res.configs[sid] = parse_source(path.read_text(encoding="utf-8"))
        except (SourceDefinitionError, OSError) as exc:
            res.errors[sid] = str(exc)
            log.warning("source %s: %s", sid, exc)
    return res


def seed_defaults(folder: Path) -> list[str]:
    """Copy bundled default sources that were never copied before.

    Remembered in ``.seeded``, so deleting a default source doesn't bring it back.
    """
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / ".seeded"
    seeded = set(marker.read_text().split()) if marker.exists() else set()
    copied = []
    for src in sorted((DEFAULTS_DIR / "sources").glob("*.yaml")):
        if src.name in seeded:
            continue
        dest = folder / src.name
        if not dest.exists():
            shutil.copyfile(src, dest)
            copied.append(src.stem)
        seeded.add(src.name)
    marker.write_text("\n".join(sorted(seeded)) + "\n")
    return copied
