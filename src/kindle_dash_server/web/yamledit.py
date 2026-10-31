"""Minimal edits to source YAML files that keep the user's comments and formatting."""

from __future__ import annotations

import re


def set_top_level_key(text: str, key: str, value: str) -> str:
    """Set ``key: value`` at the top level: replace the existing line, or add one after
    ``name:`` (or at the end)."""
    pattern = re.compile(rf"^{re.escape(key)}\s*:.*$", re.MULTILINE)
    line = f"{key}: {value}"
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    name = re.search(r"^name\s*:.*$", text, re.MULTILINE)
    if name:
        return text[: name.end()] + "\n" + line + text[name.end() :]
    return text.rstrip("\n") + "\n" + line + "\n"
