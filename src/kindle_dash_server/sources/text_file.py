"""``text_file``: fortune-style file, entries separated by a line containing only ``%``.

The last line of an entry may name the author: ``— Seneca, Moral Letters 1``. Text after the
first comma is the work (shown smaller under the author). Entries made only of ``#`` lines are
comments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..card import Card
from .base import Source, SourceConfig, SourceError

_AUTHOR_RE = re.compile(r"^\s*(?:—|–|--|~)\s*(.+?)\s*$")


@dataclass
class Entry:
    text: str
    author: str | None = None
    work: str | None = None


def parse_fortune(content: str) -> list[Entry]:
    entries: list[Entry] = []
    chunks = re.split(r"^[ \t]*%[ \t]*$", content, flags=re.MULTILINE)
    for chunk in chunks:
        lines = [ln.rstrip() for ln in chunk.strip("\n").splitlines()]
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines or all(ln.lstrip().startswith("#") for ln in lines if ln.strip()):
            continue
        author = work = None
        m = _AUTHOR_RE.match(lines[-1])
        if m and len(lines) > 1:
            lines.pop()
            author, _, work = m.group(1).partition(", ")
            work = work or None
        # Join wrapped lines into paragraphs; blank lines separate paragraphs.
        paras, cur = [], []
        for ln in lines:
            if ln.strip():
                cur.append(ln.strip())
            elif cur:
                paras.append(" ".join(cur))
                cur = []
        if cur:
            paras.append(" ".join(cur))
        entries.append(Entry("\n".join(paras), author, work))
    return entries


class TextFileConfig(SourceConfig):
    type: Literal["text_file"] = "text_file"
    path: str
    pick: Literal["random", "daily"] = "random"
    layout: str = "quote"
    # Longer entries are skipped (they'd need a tiny font).
    max_length: int = 600


class TextFileSource(Source):
    type_name = "text_file"
    Config = TextFileConfig
    config: TextFileConfig

    def cache_key(self) -> str:
        return self.ctx.today.isoformat() if self.config.pick == "daily" else ""

    def entries(self) -> list[Entry]:
        path: Path = self.ctx.paths.resolve(self.config.path)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SourceError(f"can't read {path}: {exc.strerror or exc}") from exc
        entries = [e for e in parse_fortune(content) if len(e.text) <= self.config.max_length]
        if not entries:
            raise SourceError(f"{path} has no entries")
        return entries

    def fetch(self) -> Card:
        entries = self.entries()
        e = self.rng(self.config.pick).choice(entries)
        return Card(
            body=e.text, author=e.author, title=e.work, source_name=self.config.name, layout="quote"
        )
