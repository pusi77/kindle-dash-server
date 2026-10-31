"""``local_folder``: a random image or text file from a folder on the data volume."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from ..card import Card
from .base import FilteredOut, Source, SourceConfig, SourceError

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
TEXT_EXT = {".txt", ".md"}


class LocalFolderConfig(SourceConfig):
    type: Literal["local_folder"] = "local_folder"
    path: str
    pick: Literal["random", "daily"] = "random"
    recursive: bool = True
    kinds: list[Literal["image", "text"]] = Field(default_factory=lambda: ["image", "text"])
    # Use the file name (without extension, "_" -> " ") as the title.
    title_from_filename: bool = True
    credit: str | None = None


class LocalFolderSource(Source):
    type_name = "local_folder"
    Config = LocalFolderConfig
    config: LocalFolderConfig

    def cache_key(self) -> str:
        return self.ctx.today.isoformat() if self.config.pick == "daily" else ""

    def files(self) -> list[Path]:
        root = self.ctx.paths.resolve(self.config.path)
        if not root.is_dir():
            raise SourceError(f"folder {root} doesn't exist")
        exts: set[str] = set()
        if "image" in self.config.kinds:
            exts |= IMAGE_EXT
        if "text" in self.config.kinds:
            exts |= TEXT_EXT
        it = root.rglob("*") if self.config.recursive else root.iterdir()
        files = sorted(
            p for p in it if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(".")
        )
        if not files:
            raise SourceError(f"no {'/'.join(self.config.kinds)} files in {root}")
        return files

    def fetch(self) -> Card:
        files = self.files()
        rng = self.rng(self.config.pick)
        rng.shuffle(files)
        reasons = []
        for path in files[: self.config.filters.max_attempts]:
            title = path.stem.replace("_", " ").strip() if self.config.title_from_filename else None
            if path.suffix.lower() in TEXT_EXT:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if text.startswith("# "):
                    first, _, text = text.partition("\n")
                    title = first[2:].strip()
                    text = text.strip()
                return Card(
                    title=title,
                    body=text,
                    credit=self.config.credit,
                    source_name=self.config.name,
                    layout="article" if title and len(text) > 300 else "quote",
                )
            try:
                img = self.load_image(path.read_bytes())
                self.check_image(img)
            except FilteredOut as exc:
                reasons.append(f"{path.name}: {exc}")
                continue
            return Card(
                image=img,
                title=title,
                credit=self.config.credit,
                source_name=self.config.name,
                layout="art",
                meta={"file": str(path)},
            )
        raise SourceError("no file passed the filters: " + "; ".join(reasons[:5]))
