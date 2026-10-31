"""The Card: what a Source produces and a Layout draws."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image


@dataclass
class Card:
    title: str | None = None
    body: str | None = None
    author: str | None = None
    # Small attribution line ("Rembrandt, 1654 · Art Institute of Chicago · CC0").
    credit: str | None = None
    image: Image.Image | None = None
    source_name: str = ""
    url: str | None = None
    license: str | None = None
    # A source can suggest a layout; the source config's ``layout`` wins.
    layout: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "image"}

    def save(self, json_path: Path) -> None:
        """Card as JSON, image (if any) as a lossless PNG next to it."""
        json_path.parent.mkdir(parents=True, exist_ok=True)
        img_path = json_path.with_suffix(".img.png")
        if self.image is not None:
            buf = io.BytesIO()
            self.image.save(buf, "PNG")
            img_path.write_bytes(buf.getvalue())
        elif img_path.exists():
            img_path.unlink()
        tmp = json_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), ensure_ascii=False, default=str))
        tmp.replace(json_path)

    @classmethod
    def load(cls, json_path: Path) -> Card:
        data = json.loads(json_path.read_text())
        img_path = json_path.with_suffix(".img.png")
        image = None
        if img_path.exists():
            with Image.open(img_path) as im:
                image = im.copy()
        return cls(**data, image=image)
