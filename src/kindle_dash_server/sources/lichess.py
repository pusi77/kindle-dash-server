"""``lichess_puzzle``: the Lichess daily puzzle, drawn as a board from the side to move."""

from __future__ import annotations

import re
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFont
from pydantic import Field

from ..card import Card
from ..config import BUNDLED_FONTS_DIR, Duration
from .base import Source, SourceConfig, SourceError
from .chess import FILES, Board, ChessError, replay

SYMBOLS_FONT = BUNDLED_FONTS_DIR / "_symbols" / "NotoSansSymbols2-Regular.ttf"
# Solid glyphs give the silhouette; white pieces are a white silhouette + the outline glyph.
SOLID = {"k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟"}
OUTLINE = {"k": "♔", "q": "♕", "r": "♖", "b": "♗", "n": "♘", "p": "♙"}
LIGHT_SQ, DARK_SQ = 255, 0xAA


def draw_board(
    board: Board,
    size: int,
    white_bottom: bool = True,
    label_font: ImageFont.FreeTypeFont | None = None,
) -> Image.Image:
    border = size // 22 if label_font else 0
    cell = (size - 2 * border) // 8
    size = cell * 8 + 2 * border
    img = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(img)
    piece_font = ImageFont.truetype(str(SYMBOLS_FONT), int(cell * 0.86))
    for r in range(8):
        for f in range(8):
            col = f if white_bottom else 7 - f
            row = 7 - r if white_bottom else r
            x0, y0 = border + col * cell, border + row * cell
            dark = (f + r) % 2 == 0
            d.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=DARK_SQ if dark else LIGHT_SQ)
            p = board.grid[r][f]
            if p:
                centre = (x0 + cell / 2, y0 + cell * 0.54)
                k = p.lower()
                if p.isupper():
                    d.text(centre, SOLID[k], font=piece_font, fill=255, anchor="mm")
                    d.text(centre, OUTLINE[k], font=piece_font, fill=0, anchor="mm")
                else:
                    d.text(centre, SOLID[k], font=piece_font, fill=0, anchor="mm")
    d.rectangle(
        [border - 1, border - 1, border + 8 * cell, border + 8 * cell],
        outline=0,
        width=max(2, cell // 30),
    )
    if label_font:
        for i in range(8):
            fl = FILES[i] if white_bottom else FILES[7 - i]
            rk = str(8 - i) if white_bottom else str(i + 1)
            cx = border + i * cell + cell / 2
            d.text((cx, size - border / 2), fl, font=label_font, fill=0x55, anchor="mm")
            d.text(
                (border / 2, border + i * cell + cell / 2),
                rk,
                font=label_font,
                fill=0x55,
                anchor="mm",
            )
    return img


def humanize(theme: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", theme).lower()


class LichessConfig(SourceConfig):
    type: Literal["lichess_puzzle"] = "lichess_puzzle"
    url: str = "https://lichess.org/api/puzzle/daily"
    layout: str = "info"
    layout_options: dict[str, Any] = Field(default_factory=lambda: {"image_height": 0.66})
    refresh: Duration = 3 * 3600
    max_themes: int = 4


class LichessPuzzleSource(Source):
    """The Lichess daily puzzle as a board, from the side to move."""

    type_name = "lichess_puzzle"
    Config = LichessConfig
    config: LichessConfig

    def fetch(self) -> Card:
        data = self.ctx.http.get_json(
            self.config.url,
            ttl=min(self.config.refresh, 3 * 3600),
            headers={"Accept": "application/json"},
        )
        board = self.position(data)
        puzzle, game = data.get("puzzle", {}), data.get("game", {})
        to_move = "White" if board.turn == "w" else "Black"
        size = min(self.ctx.canvas_size) - 2 * 64 * min(self.ctx.canvas_size) // 1072
        from ..render.fonts import get_font

        label = get_font(self.ctx.settings.font, "regular", max(12, size // 40))
        image = draw_board(board, size, white_bottom=board.turn == "w", label_font=label)

        rows: list[tuple[str, str]] = []
        if puzzle.get("rating"):
            rows.append(("Rating", str(puzzle["rating"])))
        themes = [
            humanize(t)
            for t in puzzle.get("themes", [])
            if t not in ("short", "long", "veryLong", "oneMove")
        ]
        if themes:
            rows.append(("Themes", ", ".join(themes[: self.config.max_themes])))
        players = game.get("players") or []
        if len(players) == 2:
            rows.append(("Game", " – ".join(_player(p) for p in players)))
        pid = puzzle.get("id", "")
        return Card(
            title=f"{to_move} to move",
            image=image,
            credit=f"lichess.org/training/{pid}" if pid else "lichess.org",
            url=f"https://lichess.org/training/{pid}" if pid else None,
            source_name=self.config.name,
            layout="info",
            meta={
                "rows": rows,
                "kicker": "Daily puzzle",
                "raw_image": True,
                "solution": puzzle.get("solution"),
            },
        )

    @staticmethod
    def position(data: dict[str, Any]) -> Board:
        puzzle, game = data.get("puzzle") or {}, data.get("game") or {}
        if puzzle.get("fen"):
            return Board(puzzle["fen"])
        pgn = game.get("pgn")
        if not pgn:
            raise SourceError("the puzzle has neither a FEN nor a PGN")
        ply = puzzle.get("initialPly")
        try:
            # The puzzle starts after move number initialPly (0-based): replay initialPly + 1.
            return replay(pgn, int(ply) + 1 if isinstance(ply, int) else None)
        except ChessError as exc:
            raise SourceError(f"can't replay the puzzle's game: {exc}") from exc


def _player(p: dict[str, Any]) -> str:
    name = p.get("name") or p.get("user", {}).get("name") or "?"
    name = re.sub(r"\s*\(\d+\)$", "", name)
    return f"{name} ({p['rating']})" if p.get("rating") else name
