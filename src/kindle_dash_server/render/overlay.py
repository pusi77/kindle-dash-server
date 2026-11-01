"""Overlays drawn on top of any layout: the low-battery badge."""

from __future__ import annotations

from PIL import Image, ImageDraw

from .fonts import get_font
from .layouts import RenderContext


def badge_geometry(level: int, ctx: RenderContext, position: str) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the battery badge."""
    px = ctx.px
    font = get_font(ctx.font, "bold", px(30))
    ascent, descent = font.getmetrics()
    w = round(px(16) + px(58) + px(6) + px(14) + font.getlength(f"{level}%") + px(16))
    h = round(max(px(30), ascent + descent) + 2 * px(12))
    margin = px(22)
    x = margin if "left" in position else ctx.width - margin - w
    y = margin if "top" in position else ctx.height - margin - h
    return x, y, w, h


def reserve_badge(level: int, ctx: RenderContext, position: str) -> None:
    """Tell the layout to keep text out of the badge's corner."""
    _, _, w, _ = badge_geometry(level, ctx, position)
    ctx.reserved[position] = w + ctx.px(22) + ctx.px(18)


def battery_badge(
    img: Image.Image, level: int, ctx: RenderContext, position: str = "top-right"
) -> Image.Image:
    """Small white badge with a battery icon and the percentage, in a corner."""
    px = ctx.px
    draw = ImageDraw.Draw(img)
    font = get_font(ctx.font, "bold", px(30))
    text = f"{level}%"
    pad, gap = px(16), px(14)
    bw, bh, nub = px(58), px(30), px(6)  # battery body, terminal nub
    x, y, w, h = badge_geometry(level, ctx, position)

    line = px(3)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=px(12), fill=255, outline=0, width=line)
    cx, cy = x + pad, y + (h - bh) // 2
    draw.rounded_rectangle([cx, cy, cx + bw, cy + bh], radius=px(4), outline=0, width=line)
    draw.rectangle([cx + bw, cy + bh // 3, cx + bw + nub, cy + bh - bh // 3], fill=0)
    inner = bw - 4 * line
    fill_w = round(inner * max(0, min(100, level)) / 100)
    if fill_w > 0:
        draw.rectangle(
            [cx + 2 * line, cy + 2 * line, cx + 2 * line + fill_w, cy + bh - 2 * line], fill=0
        )
    draw.text((cx + bw + nub + gap, y + h / 2), text, font=font, fill=0, anchor="lm")
    return img
