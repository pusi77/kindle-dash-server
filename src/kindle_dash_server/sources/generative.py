"""``generative``: line art seeded by the date (or random). No network.

Styles: ``flow_field`` (streamlines through a smooth noise field), ``truchet`` (quarter-circle
or diagonal tiles), ``ridgelines`` (stacked noisy horizons with occlusion), or ``random``.
Drawn at 2x and downsampled for clean anti-aliased lines.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw

from ..card import Card
from ..config import Duration
from .base import Source, SourceConfig

SS = 2  # supersampling factor
Style = Literal["flow_field", "truchet", "ridgelines"]


def smooth_noise(
    rng: np.random.Generator, size: tuple[int, int], scale: float, octaves: int = 3
) -> np.ndarray:
    """Value noise in [0, 1], shape (h, w). ``scale`` = pixels per noise cell (largest octave)."""
    w, h = size
    total = np.zeros((h, w), dtype=np.float32)
    amp, norm = 1.0, 0.0
    for o in range(octaves):
        cell = max(2.0, scale / (2**o))
        gw, gh = max(2, int(w / cell) + 2), max(2, int(h / cell) + 2)
        grid = Image.fromarray(rng.random((gh, gw), dtype=np.float32), "F")
        total += amp * np.asarray(grid.resize((w, h), Image.Resampling.BICUBIC))
        norm += amp
        amp *= 0.5
    total /= norm
    lo, hi = float(total.min()), float(total.max())
    return (total - lo) / (hi - lo + 1e-9)


def flow_field(size: tuple[int, int], rng: random.Random) -> Image.Image:
    W, H = size[0] * SS, size[1] * SS
    nrng = np.random.default_rng(rng.getrandbits(64))
    step = 8 * SS
    gw, gh = W // step + 1, H // step + 1
    angles = smooth_noise(nrng, (gw, gh), scale=rng.uniform(12, 28)) * math.tau * rng.uniform(1, 2)
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)
    n_lines = rng.randint(900, 1600)
    seg = 3.0 * SS
    length = rng.randint(60, 160)
    width = rng.choice([1, 2, 2, 3]) * SS
    greys = [0, 0, 0, 0x44, 0x88]
    for _ in range(n_lines):
        x, y = rng.uniform(0, W), rng.uniform(0, H)
        pts = [(x, y)]
        for _ in range(length):
            a = float(angles[int(y) // step, int(x) // step])
            x += math.cos(a) * seg
            y += math.sin(a) * seg
            if not (0 <= x < W and 0 <= y < H):
                break
            pts.append((x, y))
        if len(pts) > 4:
            draw.line(pts, fill=rng.choice(greys), width=width, joint="curve")
    return img.resize(size, Image.Resampling.LANCZOS)


def _stroke(draw: ImageDraw.ImageDraw, pts: list[tuple[float, float]], width: int) -> None:
    """Polyline with round joints and round caps."""
    draw.line(pts, fill=0, width=width, joint="curve")
    r = width / 2
    for px, py in (pts[0], pts[-1]):
        draw.ellipse([px - r, py - r, px + r, py + r], fill=0)


def truchet(size: tuple[int, int], rng: random.Random) -> Image.Image:
    W, H = size[0] * SS, size[1] * SS
    cols = rng.choice([6, 8, 10, 12, 14])
    t = W / cols
    rows = math.ceil(H / t)
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)
    lw = max(SS, int(t * rng.uniform(0.08, 0.2)))
    arcs = rng.random() < 0.7
    for r in range(rows):
        for c in range(cols):
            x, y = c * t, r * t
            flip = rng.random() < 0.5
            if arcs:
                h = t / 2
                if flip:
                    centres = [((x, y), 0, 90), ((x + t, y + t), 180, 270)]
                else:
                    centres = [((x + t, y), 90, 180), ((x, y + t), 270, 360)]
                for (cx, cy), a0, a1 in centres:
                    # Polyline + round caps: arcs of neighbouring tiles join without steps.
                    pts = [
                        (cx + h * math.cos(math.radians(a)), cy + h * math.sin(math.radians(a)))
                        for a in np.linspace(a0, a1, 24)
                    ]
                    _stroke(draw, pts, lw)
            else:
                a, b = ((x, y), (x + t, y + t)) if flip else ((x + t, y), (x, y + t))
                _stroke(draw, [a, b], lw)
    return img.resize(size, Image.Resampling.LANCZOS)


def ridgelines(size: tuple[int, int], rng: random.Random) -> Image.Image:
    W, H = size[0] * SS, size[1] * SS
    nrng = np.random.default_rng(rng.getrandbits(64))
    n = rng.randint(38, 56)
    margin_x, top, bottom = W * 0.1, H * 0.12, H * 0.92
    gap = (bottom - top) / n
    xs = np.linspace(margin_x, W - margin_x, 220)
    u = (xs - margin_x) / (W - 2 * margin_x)
    centre = rng.uniform(0.4, 0.6)
    envelope = np.exp(-((u - centre) ** 2) / (2 * rng.uniform(0.08, 0.16) ** 2))
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)
    noise = smooth_noise(nrng, (len(xs), n), scale=rng.uniform(10, 25), octaves=3)
    amp = gap * rng.uniform(7, 13)
    lw = int(2.5 * SS)
    for i in range(n):
        base = top + (i + 1) * gap
        ys = base - (noise[i] ** 2) * amp * envelope - (noise[i] * gap * 0.6)
        pts = list(zip(xs.tolist(), ys.tolist(), strict=True))
        draw.polygon([*pts, (pts[-1][0], H), (pts[0][0], H)], fill=255)
        draw.line(pts, fill=0, width=lw, joint="curve")
    return img.resize(size, Image.Resampling.LANCZOS)


STYLES: dict[str, tuple[str, Callable[[tuple[int, int], random.Random], Image.Image]]] = {
    "flow_field": ("Flow field", flow_field),
    "truchet": ("Truchet tiles", truchet),
    "ridgelines": ("Ridgelines", ridgelines),
}


class GenerativeConfig(SourceConfig):
    type: Literal["generative"] = "generative"
    style: Literal["flow_field", "truchet", "ridgelines", "random"] = "random"
    # "daily": same picture all day; "random": new one at every refresh; or a fixed number.
    seed: Literal["daily", "random"] | int = "daily"
    layout: str = "art"
    refresh: Duration = 86400


class GenerativeSource(Source):
    type_name = "generative"
    Config = GenerativeConfig
    config: GenerativeConfig

    def cache_key(self) -> str:
        return self.ctx.today.isoformat() if self.config.seed == "daily" else ""

    def seed(self) -> int:
        s = self.config.seed
        if isinstance(s, int):
            return s
        if s == "daily":
            return int(self.ctx.today.strftime("%Y%m%d"))
        return random.randrange(10**8)

    def fetch(self) -> Card:
        seed = self.seed()
        mix = int(hashlib.sha256(f"{self.id}:{seed}".encode()).hexdigest()[:16], 16)
        rng = random.Random(mix)
        style = self.config.style
        if style == "random":
            style = rng.choice(sorted(STYLES))
        title, fn = STYLES[style]
        img = fn(self.ctx.canvas_size, rng)
        return Card(
            image=img,
            title=title,
            credit=f"Generative art · seed {seed}",
            source_name=self.config.name,
            layout="art",
            license="CC0",
            meta={"style": style, "seed": seed},
        )
