"""Layouts turn a Card into a greyscale canvas. Sources never draw text themselves.

A layout is ``fn(card, ctx) -> Image (mode L, ctx.size)``, registered with ``@layout("name")``.
Layout options come from the source config's ``layout_options``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from ..card import Card
from ..config import OutputSettings
from .fonts import get_font
from .pipeline import fit_contain, fit_cover, tone
from .text import draw_block, ellipsize, fit_text, layout_text

WHITE, BLACK, GREY, LIGHT = 255, 0, 0x55, 0x99


@dataclass
class RenderContext:
    size: tuple[int, int]
    font: str = "literata"
    output: OutputSettings = field(default_factory=OutputSettings)
    now: datetime = field(default_factory=datetime.now)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def width(self) -> int:
        return self.size[0]

    @property
    def height(self) -> int:
        return self.size[1]

    def px(self, v: float) -> int:
        """Scale a size designed for the 1072 px-wide portrait canvas."""
        return max(1, round(v * min(self.size) / 1072))


LayoutFn = Callable[[Card, RenderContext], Image.Image]
LAYOUTS: dict[str, LayoutFn] = {}


def layout(name: str) -> Callable[[LayoutFn], LayoutFn]:
    def deco(fn: LayoutFn) -> LayoutFn:
        LAYOUTS[name] = fn
        return fn

    return deco


def render_card(card: Card, layout_name: str, ctx: RenderContext) -> Image.Image:
    try:
        fn = LAYOUTS[layout_name]
    except KeyError:
        raise ValueError(f"unknown layout {layout_name!r} (known: {', '.join(LAYOUTS)})") from None
    img = fn(card, ctx)
    if img.size != ctx.size or img.mode != "L":
        raise RuntimeError(f"layout {layout_name} returned {img.mode} {img.size}")
    return img


def _canvas(ctx: RenderContext) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("L", ctx.size, WHITE)
    return img, ImageDraw.Draw(img)


def _join(*parts: str | None, sep: str = " · ") -> str:
    return sep.join(p.strip() for p in parts if p and p.strip())


# --------------------------------------------------------------------------------------------
# art: full-bleed image with a small caption at the bottom
# --------------------------------------------------------------------------------------------


@layout("art")
def art(card: Card, ctx: RenderContext) -> Image.Image:
    img, draw = _canvas(ctx)
    W, H = ctx.size
    opt = ctx.options
    show_caption = opt.get("caption", True) and bool(card.title or card.author or card.credit)
    pad = ctx.px(opt.get("caption_padding", 30))

    band = 0
    title_font = get_font(ctx.font, "italic", ctx.px(opt.get("title_size", 32)))
    small_font = get_font(ctx.font, "regular", ctx.px(opt.get("credit_size", 22)))
    lines: list[tuple[str, Any, int]] = []
    if show_caption:
        maxw = W - 2 * pad
        if card.title:
            lines.append((ellipsize(card.title, title_font, maxw), title_font, BLACK))
        sub = _join(card.author, card.credit)
        if sub:
            lines.append((ellipsize(sub, small_font, maxw), small_font, GREY))
        gap = ctx.px(8)
        band = 2 * pad + sum(sum(f.getmetrics()) for _, f, _ in lines) + gap * (len(lines) - 1)

    area = (W, H - band)
    if card.image is not None:
        src = tone(card.image, ctx.output, ctx.options)
        fit = opt.get("fit", "auto")
        if fit == "auto":
            ar_img = src.width / src.height
            ar_area = area[0] / area[1]
            fit = (
                "cover"
                if abs(ar_img / ar_area - 1) <= opt.get("cover_tolerance", 0.15)
                else ("contain")
            )
        if fit == "cover":
            placed = fit_cover(src, area)
            img.paste(placed, (0, 0))
        else:
            m = ctx.px(opt.get("margin", 36))
            inner = (area[0] - 2 * m, area[1] - 2 * m)
            placed = fit_contain(src, inner)
            img.paste(placed, (m, m))

    if lines:
        y = H - band + pad
        if card.image is not None:
            draw.line([(pad, H - band), (W - pad, H - band)], fill=LIGHT, width=ctx.px(2))
        for text, font, fill in lines:
            ascent, descent = font.getmetrics()
            draw.text((pad, y + ascent), text, font=font, fill=fill, anchor="ls")
            y += ascent + descent + ctx.px(8)
    return img


# --------------------------------------------------------------------------------------------
# quote: large centred serif text, author underneath
# --------------------------------------------------------------------------------------------


@layout("quote")
def quote(card: Card, ctx: RenderContext) -> Image.Image:
    img, draw = _canvas(ctx)
    W, H = ctx.size
    opt = ctx.options
    margin = ctx.px(opt.get("margin", 100))
    text = (card.body or card.title or "").strip()
    width = W - 2 * margin

    author_block = None
    if card.author:
        author_block = layout_text(
            f"— {card.author}", ctx.font, "italic", ctx.px(opt.get("author_size", 40)), width
        )
    work_block = None
    if card.title and card.body:
        work_block = layout_text(card.title, ctx.font, "regular", ctx.px(30), width)

    gap = ctx.px(56)
    below = 0
    if author_block:
        below += gap + author_block.height
    if work_block:
        below += ctx.px(14) + work_block.height
    mark_h = ctx.px(140)
    max_text_h = H - 2 * margin - below - mark_h - ctx.px(120)

    body = fit_text(
        text,
        ctx.font,
        "regular",
        width,
        max_text_h,
        max_size=ctx.px(opt.get("max_size", 92)),
        min_size=ctx.px(28),
        spacing=opt.get("line_spacing", 1.3),
    )
    total = mark_h + body.height + below
    y = (H - total) // 2

    mark_font = get_font(ctx.font, "regular", ctx.px(200))
    draw.text((W / 2, y + mark_h), "“", font=mark_font, fill=LIGHT, anchor="ms")
    y += mark_h
    y = draw_block(draw, body, margin, y, width, align="center")
    if author_block:
        y += gap
        rule = ctx.px(60)
        draw.line(
            [(W / 2 - rule, y - gap // 2), (W / 2 + rule, y - gap // 2)],
            fill=LIGHT,
            width=ctx.px(2),
        )
        y = draw_block(draw, author_block, margin, y, width, align="center")
    if work_block:
        y += ctx.px(14)
        draw_block(draw, work_block, margin, y, width, align="center", fill=GREY)

    _footer(draw, ctx, card.credit, center=True)
    return img


def _footer(
    draw: ImageDraw.ImageDraw, ctx: RenderContext, text: str | None, center: bool = False
) -> None:
    if not text:
        return
    W, H = ctx.size
    font = get_font(ctx.font, "regular", ctx.px(20))
    pad = ctx.px(40)
    text = ellipsize(text, font, W - 2 * pad)
    if center:
        draw.text((W / 2, H - pad), text, font=font, fill=GREY, anchor="ms")
    else:
        draw.text((pad, H - pad), text, font=font, fill=GREY, anchor="ls")


# --------------------------------------------------------------------------------------------
# article: title + short text, optional thumbnail
# --------------------------------------------------------------------------------------------


@layout("article")
def article(card: Card, ctx: RenderContext) -> Image.Image:
    img, draw = _canvas(ctx)
    W, H = ctx.size
    opt = ctx.options
    m = ctx.px(opt.get("margin", 64))
    width = W - 2 * m

    # Header: kicker (e.g. "ON THIS DAY") left, date right.
    kicker = (opt.get("kicker") or card.meta.get("kicker") or card.source_name or "").upper()
    head_font = get_font(ctx.font, "bold", ctx.px(22))
    ascent, descent = head_font.getmetrics()
    y = m
    if kicker or opt.get("date", True):
        date_s = ctx.now.strftime(opt.get("date_format", "%A %-d %B %Y"))
        date_w = head_font.getlength(date_s) if opt.get("date", True) else 0
        kicker = ellipsize(kicker, head_font, int(width - date_w - ctx.px(24)))
        draw.text((m, y + ascent), kicker, font=head_font, fill=BLACK, anchor="ls")
        if date_w:
            draw.text((W - m, y + ascent), date_s, font=head_font, fill=GREY, anchor="rs")
        y += ascent + descent + ctx.px(16)
        draw.line([(m, y), (W - m, y)], fill=BLACK, width=ctx.px(3))
        y += ctx.px(40)

    footer_h = ctx.px(70) if card.credit else ctx.px(30)
    bottom = H - m - footer_h

    if card.image is not None:
        max_h = int((H - 2 * m) * opt.get("image_height", 0.42))
        src = tone(card.image, ctx.output, ctx.options)
        if opt.get("image_fit", "contain") == "cover":
            placed = fit_cover(src, (width, max_h))
        else:
            placed = ImageOps.contain(src, (width, max_h), Image.Resampling.LANCZOS)
        img.paste(placed, (m + (width - placed.width) // 2, y))
        y += placed.height + ctx.px(44)

    if card.title:
        title = fit_text(
            card.title,
            ctx.font,
            "bold",
            width,
            ctx.px(opt.get("title_height", 240)),
            max_size=ctx.px(68),
            min_size=ctx.px(34),
            spacing=1.15,
        )
        y = draw_block(draw, title, m, y, width)
        y += ctx.px(32)
    if card.author:
        by = layout_text(card.author, ctx.font, "italic", ctx.px(30), width)
        y = draw_block(draw, by, m, y, width, fill=GREY) + ctx.px(28)
    if card.body and bottom - y > ctx.px(40):
        body = fit_text(
            card.body,
            ctx.font,
            "regular",
            width,
            bottom - y,
            max_size=ctx.px(opt.get("body_max_size", 42)),
            min_size=ctx.px(26),
            spacing=1.35,
        )
        draw_block(draw, body, m, y, width)

    _footer(draw, ctx, card.credit)
    return img
