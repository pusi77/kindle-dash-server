"""Text wrapping and fitting helpers on top of Pillow."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import ImageDraw, ImageFont

from .fonts import Style, get_font


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word wrap. Paragraph breaks (newlines) are kept; over-long words are split."""
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        words = para.split()
        if not words:
            lines.append("")
            continue
        line = ""
        for word in words:
            candidate = f"{line} {word}" if line else word
            if font.getlength(candidate) <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
            # Split a word that alone doesn't fit.
            while font.getlength(word) > max_width and len(word) > 1:
                cut = len(word)
                while cut > 1 and font.getlength(word[:cut] + "-") > max_width:
                    cut -= 1
                lines.append(word[:cut] + "-")
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def line_height(font: ImageFont.FreeTypeFont, spacing: float = 1.25) -> int:
    """Baseline-to-baseline distance as a multiple of the font size."""
    return round(font.size * spacing)


@dataclass
class TextBlock:
    lines: list[str]
    font: ImageFont.FreeTypeFont
    line_h: int

    @property
    def height(self) -> int:
        if not self.lines:
            return 0
        ascent, descent = self.font.getmetrics()
        return self.line_h * (len(self.lines) - 1) + ascent + descent

    @property
    def width(self) -> int:
        return max((round(self.font.getlength(line)) for line in self.lines), default=0)


def layout_text(
    text: str, family: str, style: Style, size: int, max_width: int, spacing: float = 1.25
) -> TextBlock:
    font = get_font(family, style, size)
    return TextBlock(wrap(text, font, max_width), font, line_height(font, spacing))


def fit_text(
    text: str,
    family: str,
    style: Style,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int,
    spacing: float = 1.25,
) -> TextBlock:
    """Largest font size (binary search) at which the wrapped text fits the box.

    At ``min_size`` the text is truncated with an ellipsis if it still doesn't fit.
    """
    lo, hi = min_size, max_size
    best: TextBlock | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        block = layout_text(text, family, style, mid, max_width, spacing)
        if block.height <= max_height and block.width <= max_width:
            best, lo = block, mid + 1
        else:
            hi = mid - 1
    if best is not None:
        return best
    block = layout_text(text, family, style, min_size, max_width, spacing)
    return truncate(block, max_height, max_width)


def truncate(block: TextBlock, max_height: int, max_width: int) -> TextBlock:
    ascent, descent = block.font.getmetrics()
    max_lines = max(1, (max_height - ascent - descent) // block.line_h + 1)
    if len(block.lines) <= max_lines:
        return block
    lines = block.lines[:max_lines]
    last = lines[-1].rstrip(" ,;:.-")
    while last and block.font.getlength(last + "…") > max_width:
        last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
    lines[-1] = last + "…"
    return TextBlock(lines, block.font, block.line_h)


def draw_block(
    draw: ImageDraw.ImageDraw,
    block: TextBlock,
    x: int,
    y: int,
    width: int,
    align: str = "left",
    fill: int = 0,
) -> int:
    """Draw at (x, y) (top of the first line). Returns the y just below the block."""
    ascent, _ = block.font.getmetrics()
    for i, line in enumerate(block.lines):
        w = block.font.getlength(line)
        if align == "center":
            lx = x + (width - w) / 2
        elif align == "right":
            lx = x + width - w
        else:
            lx = x
        draw.text(
            (lx, y + i * block.line_h + ascent), line, font=block.font, fill=fill, anchor="ls"
        )
    return y + block.height


def ellipsize(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return text.rstrip() + "…"
