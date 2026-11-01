"""Final pipeline: exact size -> rotation -> greyscale -> N grey levels (dithered) -> PNG.

Output matches what kindle-dash-pw3 draws with ``eips -g``: 8-bit greyscale PNG (colour type 0),
no alpha, exactly ``width x height``, values on the panel's grey levels (0x00, 0x11 ... 0xFF).
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageOps

from ..config import OutputSettings

_ROTATE = {
    90: Image.Transpose.ROTATE_270,  # Pillow's ROTATE_* are counter-clockwise
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def levels(n: int) -> list[int]:
    """n evenly spaced grey values; for 16: 0, 17, 34 ... 255."""
    return [round(i * 255 / (n - 1)) for i in range(n)]


def fit_cover(
    img: Image.Image, size: tuple[int, int], centering: tuple[float, float] = (0.5, 0.5)
) -> Image.Image:
    if img.size == size:
        return img
    return ImageOps.fit(img, size, Image.Resampling.LANCZOS, centering=centering)


def fit_contain(img: Image.Image, size: tuple[int, int], background: int = 255) -> Image.Image:
    """Scale to fit inside ``size`` and centre on a ``background`` canvas."""
    scaled = ImageOps.contain(img, size, Image.Resampling.LANCZOS)
    canvas = Image.new(img.mode, size, background if img.mode == "L" else (background,) * 3)
    canvas.paste(scaled, ((size[0] - scaled.width) // 2, (size[1] - scaled.height) // 2))
    return canvas


def to_gray(img: Image.Image) -> Image.Image:
    """Greyscale; transparency is flattened onto white."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba)
    if img.mode == "I;16" or img.mode == "I":
        img = img.point(lambda v: v / 256).convert("L")
    return img if img.mode == "L" else img.convert("L")


def paper_level(hist: list[int], min_share: float = 0.25) -> int | None:
    """Grey value of the paper, if one bright level (+-4) holds at least ``min_share`` of the
    pixels, as in scanned prints and drawings. None for ordinary photos."""
    total = sum(hist) or 1
    best, best_n = None, 0
    for v in range(128, 256):
        n = sum(hist[max(0, v - 4) : v + 5])
        if n > best_n:
            best, best_n = v, n
    if best is None or best_n / total < min_share:
        return None
    # The most common value inside the densest window, not the window's first centre.
    lo = max(0, best - 4)
    window = hist[lo : best + 5]
    return lo + window.index(max(window))


def tone(img: Image.Image, out: OutputSettings, overrides: dict | None = None) -> Image.Image:
    """Autocontrast and gamma, for photographic content.

    The white point is the paper's grey level when the image is mostly paper (old prints are
    yellowed: without this the paper becomes a dithered grey), otherwise the ``white_cutoff``
    percentile. ``overrides`` (a source's ``layout_options``) may set autocontrast,
    black_cutoff, white_cutoff, paper_white and gamma for that source only.
    """
    o = overrides or {}
    img = to_gray(img)
    lut = list(range(256))
    if o.get("autocontrast", out.autocontrast):
        hist = img.histogram()
        lo = _percentile(hist, float(o.get("black_cutoff", out.black_cutoff)))
        hi = 255 - _percentile(hist[::-1], float(o.get("white_cutoff", out.white_cutoff)))
        if o.get("paper_white", out.paper_white):
            paper = paper_level(hist)
            if paper is not None:
                hi = paper - 2  # a little JPEG noise around the paper level still goes white
        if hi - lo >= 16:
            lut = [min(255, max(0, round((v - lo) * 255 / (hi - lo)))) for v in range(256)]
    gamma = float(o.get("gamma", out.gamma))
    if abs(gamma - 1.0) > 1e-3:
        lut = [round(255 * (v / 255) ** (1.0 / gamma)) for v in lut]
    return img.point(lut)


def _percentile(hist: list[int], percent: float) -> int:
    """Lowest value v such that more than ``percent``% of pixels are <= v."""
    limit = sum(hist) * percent / 100
    acc = 0
    for v, n in enumerate(hist):
        acc += n
        if acc > limit:
            return v
    return 255


def quantize(img: Image.Image, n_levels: int = 16, method: str = "floyd_steinberg") -> Image.Image:
    """Reduce an L image to ``n_levels`` evenly spaced greys."""
    img = to_gray(img)
    lv = levels(n_levels)
    if n_levels >= 256:
        return img
    if method == "none":
        return img.point([lv[round(v * (n_levels - 1) / 255)] for v in range(256)])
    if method == "floyd_steinberg":
        pal = Image.new("P", (1, 1))
        flat = [c for v in lv for c in (v, v, v)]
        pal.putpalette(flat + [0] * (768 - len(flat)))
        q = img.convert("RGB").quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG)
        return q.convert("L")
    if method == "ordered":
        return _ordered(img, n_levels)
    if method == "atkinson":
        return _atkinson(img, n_levels)
    raise ValueError(f"unknown dithering method {method!r}")


def _bayer(n: int) -> np.ndarray:
    m = np.array([[0, 2], [3, 1]], dtype=np.float64)
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / m.size


def _ordered(img: Image.Image, n_levels: int) -> Image.Image:
    a = np.asarray(img, dtype=np.float64) / 255 * (n_levels - 1)
    b = _bayer(8)
    h, w = a.shape
    t = np.tile(b, (h // 8 + 1, w // 8 + 1))[:h, :w]
    q = np.clip(np.floor(a + t), 0, n_levels - 1)
    out = np.rint(q * 255 / (n_levels - 1)).astype(np.uint8)
    return Image.fromarray(out, "L")


def _atkinson(img: Image.Image, n_levels: int) -> Image.Image:
    """Atkinson error diffusion (diffuses 6/8 of the error: lighter, crisper than F-S).

    Pure Python on rows; a few seconds for a full frame, fine for ahead-of-time rendering.
    """
    w, h = img.size
    step = 255 / (n_levels - 1)
    data = np.asarray(img, dtype=np.float32).copy()
    rows = [list(map(float, r)) for r in data]
    for y in range(h):
        r0 = rows[y]
        r1 = rows[y + 1] if y + 1 < h else None
        r2 = rows[y + 2] if y + 2 < h else None
        for x in range(w):
            old = r0[x]
            new = round(old / step) * step
            new = 0.0 if new < 0 else 255.0 if new > 255 else new
            r0[x] = new
            err = (old - new) / 8
            if err == 0:
                continue
            if x + 1 < w:
                r0[x + 1] += err
            if x + 2 < w:
                r0[x + 2] += err
            if r1 is not None:
                if x > 0:
                    r1[x - 1] += err
                r1[x] += err
                if x + 1 < w:
                    r1[x + 1] += err
            if r2 is not None:
                r2[x] += err
    out = np.rint(np.clip(np.array(rows), 0, 255)).astype(np.uint8)
    return Image.fromarray(out, "L")


def finalize(img: Image.Image, out: OutputSettings) -> Image.Image:
    """Layout output (canvas_size) -> device-ready L image (width x height, n grey levels)."""
    img = to_gray(img)
    img = fit_cover(img, out.canvas_size)
    if out.rotation:
        img = img.transpose(_ROTATE[out.rotation])
    assert img.size == (out.width, out.height)
    return quantize(img, out.gray_levels, out.dither)


def encode_png(img: Image.Image) -> bytes:
    """8-bit greyscale PNG, colour type 0, no alpha."""
    if img.mode != "L":
        raise ValueError(f"expected mode L, got {img.mode}")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
