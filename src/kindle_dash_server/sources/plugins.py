"""Python plugin sources: every ``/data/plugins/*.py`` is imported; ``Source`` subclasses with a
``type_name`` become source types. A plugin type that no YAML file uses yet gets one, disabled,
so it shows up in the UI (deleting that file later doesn't bring it back).
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .base import SOURCE_TYPES, Source
from .loader import ID_RE

log = logging.getLogger(__name__)

_MODULE_PREFIX = "kds_plugins."
_loaded: dict[str, type[Source]] = {}


@dataclass
class PluginResult:
    types: dict[str, type[Source]] = field(default_factory=dict)  # type_name -> class
    files: dict[str, list[str]] = field(default_factory=dict)  # file -> type names
    errors: dict[str, str] = field(default_factory=dict)  # file -> error


def load_plugins(folder: Path) -> PluginResult:
    """(Re)import every plugin file. Previously loaded plugin types are dropped first."""
    for name in _loaded:
        SOURCE_TYPES.pop(name, None)
    _loaded.clear()
    for mod in [m for m in sys.modules if m.startswith(_MODULE_PREFIX)]:
        del sys.modules[mod]

    res = PluginResult()
    if not folder.is_dir():
        return res
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        builtins = dict(SOURCE_TYPES)
        modname = _MODULE_PREFIX + re.sub(r"\W", "_", path.stem)
        try:
            spec = importlib.util.spec_from_file_location(modname, path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[modname] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(modname, None)
            SOURCE_TYPES.clear()
            SOURCE_TYPES.update(builtins)
            tb = traceback.format_exception_only(type(exc), exc)[-1].strip()
            res.errors[path.name] = tb
            log.warning("plugin %s failed to load: %s", path.name, tb)
            continue
        names = []
        for name, cls in list(SOURCE_TYPES.items()):
            if cls.__module__ != modname:
                continue
            if name in builtins:
                SOURCE_TYPES[name] = builtins[name]
                res.errors[path.name] = f"type {name!r} clashes with an existing source type"
                continue
            res.types[name] = cls
            _loaded[name] = cls
            names.append(name)
        if not names and path.name not in res.errors:
            res.errors[path.name] = "no Source subclass with a type_name found"
        res.files[path.name] = names
    return res


def ensure_plugin_sources(
    sources_dir: Path, types: dict[str, type[Source]], used_types: set[str]
) -> list[str]:
    """Write a disabled YAML file for plugin types no source uses yet (once per type)."""
    marker = sources_dir / ".seeded"
    seeded = set(marker.read_text().split()) if marker.exists() else set()
    created = []
    for name, cls in sorted(types.items()):
        key = f"plugin:{name}"
        if name in used_types or key in seeded:
            continue
        sid = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-") or "plugin"
        if not ID_RE.match(sid):
            sid = "plugin"
        path = sources_dir / f"{sid}.yaml"
        n = 2
        while path.exists():
            path = sources_dir / f"{sid}-{n}.yaml"
            n += 1
        data = {
            "type": name,
            "name": name.replace("_", " ").title(),
            **cls.defaults,
            "enabled": False,
        }
        doc = (cls.__doc__ or "").strip().splitlines()
        header = f"# Created for plugin source type {name!r}. Enable it in the UI.\n"
        if doc:
            header += f"# {doc[0]}\n"
        path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        seeded.add(key)
        created.append(path.stem)
    marker.write_text("\n".join(sorted(seeded)) + "\n")
    return created
