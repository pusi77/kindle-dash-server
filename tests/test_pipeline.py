"""Final pipeline: the output must be exactly what kindle-dash-pw3 draws with eips."""

from __future__ import annotations

import numpy as np
import pytest
from conftest import png_info
from PIL import Image

from kindle_dash_server.card import Card
from kindle_dash_server.config import OutputSettings
from kindle_dash_server.render.fonts import get_font
from kindle_dash_server.render.layouts import LAYOUTS, RenderContext, render_card
from kindle_dash_server.render.pipeline import (
    encode_png,
    finalize,
    fit_contain,
    fit_cover,
    levels,
    paper_level,
    quantize,
    to_gray,
    tone,
)
from kindle_dash_server.render.text import fit_text, wrap

PW3 = (1072, 1448)
PANEL_LEVELS = {i * 0x11 for i in range(16)}


def gradient(size=(256, 64)) -> Image.Image:
    a = np.tile(np.linspace(0, 255, size[0]), (size[1], 1)).astype(np.uint8)
    return Image.fromarray(a, "L")


def values(img: Image.Image) -> set[int]:
    return set(np.unique(np.asarray(img)).tolist())


def test_levels_are_the_panel_greys():
    assert set(levels(16)) == PANEL_LEVELS
    assert levels(2) == [0, 255]


@pytest.mark.parametrize("method", ["floyd_steinberg", "atkinson", "ordered", "none"])
def test_quantize_only_uses_panel_levels(method):
    out = quantize(gradient(), 16, method)
    assert out.mode == "L"
    assert out.size == (256, 64)
    assert values(out) <= PANEL_LEVELS
    # A full gradient should use (nearly) every level.
    assert len(values(out)) >= 14


# Atkinson diffuses only 6/8 of the error by design (lighter, crisper): midtones drift more.
@pytest.mark.parametrize(
    "method,tolerance", [("floyd_steinberg", 3), ("ordered", 6), ("atkinson", 12)]
)
def test_dithering_preserves_average_tone(method, tolerance):
    flat = Image.new("L", (128, 128), 100)
    out = np.asarray(quantize(flat, 4, method), dtype=float)  # levels 0, 85, 170, 255
    assert abs(out.mean() - 100) < tolerance
    assert len(values(Image.fromarray(out.astype(np.uint8)))) >= 2  # actually dithered


def test_quantize_none_rounds_to_nearest():
    img = Image.new("L", (4, 1))
    img.putdata([0, 8, 9, 255])
    assert list(np.asarray(quantize(img, 16, "none"))[0]) == [0, 0, 17, 255]


def test_unknown_dither_method():
    with pytest.raises(ValueError):
        quantize(gradient(), 16, "magic")


def test_to_gray_flattens_alpha_on_white():
    rgba = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    assert values(to_gray(rgba)) == {255}


def test_fit_cover_and_contain_sizes():
    wide = Image.new("L", (300, 100), 0)
    assert fit_cover(wide, (100, 100)).size == (100, 100)
    contained = fit_contain(wide, (100, 100))
    assert contained.size == (100, 100)
    a = np.asarray(contained)
    assert a[0, 50] == 255 and a[50, 50] == 0  # letterboxed on white


@pytest.mark.parametrize("rotation,size", [(0, PW3), (90, PW3), (180, PW3), (270, PW3)])
def test_finalize_always_device_size(rotation, size):
    out = OutputSettings(rotation=rotation)
    canvas = Image.new("L", out.canvas_size, 200)
    img = finalize(canvas, out)
    assert img.size == size
    assert values(img) <= PANEL_LEVELS


def test_landscape_canvas_and_rotation_direction():
    out = OutputSettings(rotation=90)
    assert out.canvas_size == (1448, 1072)
    canvas = Image.new("L", out.canvas_size, 255)
    canvas.paste(0, (0, 0, 100, 100))  # black square top-left of the landscape canvas
    a = np.asarray(finalize(canvas, out))
    # 90° clockwise: the top-left corner ends up top-right.
    assert a[10, -10] == 0 and a[10, 10] == 255


def test_finalize_fits_wrong_size_input():
    img = finalize(Image.new("RGB", (500, 500), (255, 0, 0)), OutputSettings())
    assert img.size == PW3 and img.mode == "L"


def test_png_matches_client_contract():
    img = finalize(gradient(PW3), OutputSettings())
    data = encode_png(img)
    assert png_info(data) == {"width": 1072, "height": 1448, "bit_depth": 8, "color_type": 0}


def test_encode_png_rejects_non_greyscale():
    with pytest.raises(ValueError):
        encode_png(Image.new("RGB", (4, 4)))


def test_paper_level_detects_prints_but_not_photos():
    paper = Image.new("L", (200, 200), 205)
    paper.paste(30, (50, 50, 80, 150))
    assert paper_level(paper.histogram()) == 205
    assert paper_level(gradient().histogram()) is None


def test_tone_turns_yellowed_paper_white():
    img = Image.new("RGB", (200, 200), (215, 200, 170))  # old paper
    img.paste((40, 30, 20), (50, 50, 150, 150))  # ink
    out = np.asarray(tone(img, OutputSettings()))
    assert out[0, 0] == 255
    assert out[100, 100] < 20


def test_tone_overrides_per_source():
    img = Image.new("L", (10, 10), 128)
    img.paste(0, (0, 0, 5, 10))
    plain = tone(img, OutputSettings(), {"autocontrast": False})
    assert np.asarray(plain)[0, 9] == 128
    lighter = tone(img, OutputSettings(), {"autocontrast": False, "gamma": 2.0})
    assert np.asarray(lighter)[0, 9] > 128


# -- layouts ---------------------------------------------------------------------------------

CARDS = {
    "art": Card(image=gradient((800, 1000)), title="A title", author="Someone", credit="1650"),
    "quote": Card(body="A short quote.", author="Seneca", title="Letters 1"),
    "article": Card(
        title="Headline", body="Body text " * 40, image=gradient((600, 400)), credit="CC BY-SA"
    ),
    "info": Card(
        title="Waxing",
        body="50%",
        image=gradient((300, 300)),
        meta={"rows": [("Sunrise", "07:00"), ("Sunset", "17:00")]},
    ),
}


@pytest.mark.parametrize("name", sorted(LAYOUTS))
@pytest.mark.parametrize("size", [PW3, (1448, 1072)])
def test_every_layout_fills_the_canvas(name, size):
    card = CARDS.get(name, CARDS["article"])
    img = render_card(card, name, RenderContext(size=size))
    assert img.size == size and img.mode == "L"
    assert len(values(img)) > 2  # something was drawn


@pytest.mark.parametrize("name", sorted(LAYOUTS))
def test_layouts_cope_with_empty_and_huge_cards(name):
    ctx = RenderContext(size=PW3)
    render_card(Card(), name, ctx)
    render_card(
        Card(title="T" * 500, body="word " * 3000, author="A" * 300, credit="C" * 400), name, ctx
    )


def test_unknown_layout():
    with pytest.raises(ValueError, match="unknown layout"):
        render_card(Card(), "nope", RenderContext(size=PW3))


def test_wrap_respects_width_and_paragraphs():
    font = get_font("literata", "regular", 40)
    lines = wrap("one two three four five six seven\n\nnext paragraph", font, 300)
    assert "" in lines
    assert all(font.getlength(line) <= 300 for line in lines)
    long = wrap("x" * 200, font, 300)
    assert all(font.getlength(line) <= 300 for line in long)


def test_fit_text_shrinks_then_truncates():
    short = fit_text("Hi", "literata", "regular", 800, 400, max_size=120, min_size=20)
    assert short.font.size == 120
    longer = fit_text("word " * 60, "literata", "regular", 800, 400, max_size=120, min_size=20)
    assert 20 <= longer.font.size < 120
    assert longer.height <= 400
    huge = fit_text("word " * 5000, "literata", "regular", 800, 400, max_size=120, min_size=20)
    assert huge.height <= 400
    assert huge.lines[-1].endswith("…")
