"""Built-in Python sources: generative art, almanac, Lichess (and its chess replayer)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from kindle_dash_server.config import Paths, Settings
from kindle_dash_server.sources import SOURCE_TYPES, SourceContext, SourceError
from kindle_dash_server.sources.almanac import illumination, next_phase_day, phase_name
from kindle_dash_server.sources.chess import Board, ChessError, replay, san_tokens
from kindle_dash_server.sources.lichess import LichessPuzzleSource, humanize
from kindle_dash_server.sources.loader import parse_source

TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 11, 2, 7, 0, tzinfo=TZ)


def make(text, http, data_dir, now=NOW, sid="s"):
    cfg = parse_source(text)
    ctx = SourceContext(http=http, paths=Paths(data_dir), settings=Settings(), now=now)
    return SOURCE_TYPES[cfg.type](sid, cfg, ctx)


# -- generative ------------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["flow_field", "truchet", "ridgelines"])
def test_generative_styles(style, http, data_dir):
    src = make(f"type: generative\nname: G\nstyle: {style}", http, data_dir)
    card = src.fetch()
    assert card.image.size == (1072, 1448)
    a = np.asarray(card.image)
    assert a.min() < 50 and a.max() > 200  # actual drawing, not a blank canvas
    assert card.meta["style"] == style


def test_generative_daily_seed_is_reproducible(http, data_dir):
    text = "type: generative\nname: G\nstyle: random\nseed: daily"
    a = make(text, http, data_dir).fetch()
    b = make(text, http, data_dir, now=NOW.replace(hour=20)).fetch()
    c = make(text, http, data_dir, now=NOW.replace(day=3)).fetch()
    assert a.meta == b.meta and a.image.tobytes() == b.image.tobytes()
    assert c.meta["seed"] != a.meta["seed"]


# -- almanac ---------------------------------------------------------------------------------


def test_moon_helpers():
    assert phase_name(0.2) == "New moon"
    assert phase_name(14.0) == "Full moon"
    assert phase_name(21.5) == "Last quarter"
    assert illumination(0) == pytest.approx(0)
    assert illumination(14) == pytest.approx(1)
    full = next_phase_day(NOW.date(), 14.0)
    assert 0 < (full - NOW.date()).days <= 30


def test_almanac_card(http, data_dir):
    card = make("type: almanac\nname: A", http, data_dir).fetch()
    rows = dict(card.meta["rows"])
    assert {"Sunrise", "Sunset", "Day length", "Full moon", "New moon"} <= set(rows)
    assert rows["Sunrise"] < rows["Sunset"]
    assert card.layout == "info" and card.image.size[0] == card.image.size[1]


def test_almanac_polar_night(http, data_dir):
    cfg = parse_source("type: almanac\nname: A")
    settings = Settings.model_validate(
        {
            "location": {
                "name": "Svalbard",
                "latitude": 78.2,
                "longitude": 15.6,
                "timezone": "Arctic/Longyearbyen",
            }
        }
    )
    ctx = SourceContext(
        http=http,
        paths=Paths(data_dir),
        settings=settings,
        now=datetime(2026, 12, 21, 12, tzinfo=TZ),
    )
    card = SOURCE_TYPES["almanac"]("a", cfg, ctx).fetch()
    assert dict(card.meta["rows"])["Sun"].startswith("doesn't rise")


# -- chess -----------------------------------------------------------------------------------


def test_replay_ruy_lopez_with_castling():
    b = replay("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O")
    assert b.placement() == "r1bq1rk1/2p1bppp/p1np1n2/1p2p3/4P3/1BP2N2/PP1P1PPP/RNBQR1K1"
    assert b.castling == "" and b.turn == "w"


def test_en_passant_and_promotion():
    b = replay("e4 d5 e5 f5 exf6 Kf7 fxg7 Nf6 gxh8=Q")
    assert b.placement() == "rnbq1b1Q/ppp1pk1p/5n2/3p4/8/8/PPPP1PPP/RNBQKBNR"


def test_pinned_piece_disambiguation():
    b = Board("4k3/8/8/4b3/8/2N3N1/7K/8 w - - 0 1")
    b.push_san("Ne4")  # g3 is pinned by the bishop, so only c3 can go
    assert b.placement() == "4k3/8/8/4b3/4N3/6N1/7K/8"
    with pytest.raises(ChessError, match="ambiguous"):
        Board("4k3/8/8/8/8/2N3N1/7K/8 w - - 0 1").push_san("Ne4")


@pytest.mark.parametrize(
    "pgn",
    [
        "e5",
        "e4 e5 Ke3",
        "e4 e5 Qh5 Qh4 Qxf8",
        "O-O",
        "zz9",
        # castling into check: the queen on g5 covers g1
        "e4 e5 Nf3 d5 Bc4 Bg4 Bxd5 Bxf3 gxf3 Qg5 O-O",
    ],
)
def test_illegal_moves(pgn):
    with pytest.raises(ChessError):
        replay(pgn)


def test_castling_rules():
    b = Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    b.push_san("O-O-O")
    assert b.placement() == "r3k2r/8/8/8/8/8/8/2KR3R"
    b.push_san("O-O")
    assert b.placement() == "r4rk1/8/8/8/8/8/8/2KR3R"
    with pytest.raises(ChessError, match="check"):
        Board("4k3/8/8/8/8/8/6r1/4K2R w K - 0 1").push_san("O-O")  # the rook covers g1


def test_san_tokens_strip_pgn_noise():
    pgn = '[Event "x"]\n1. e4 {best} e5 2. Nf3 (2. f4) Nc6 $1 1-0'
    assert san_tokens(pgn) == ["e4", "e5", "Nf3", "Nc6"]


# -- lichess ---------------------------------------------------------------------------------

PUZZLE = {
    "game": {
        "pgn": "e4 e5 Nf3 Nc6 Bc4 Nd4",
        "players": [
            {"name": "alice", "color": "white", "rating": 1800},
            {"name": "bob", "color": "black", "rating": 1750},
        ],
    },
    "puzzle": {
        "id": "abc12",
        "rating": 1500,
        "initialPly": 4,
        "solution": ["f3e5"],
        "themes": ["mateIn2", "short", "discoveredAttack"],
    },
}


def test_lichess_position_respects_initial_ply():
    b = LichessPuzzleSource.position(PUZZLE)  # replays initialPly + 1 = 5 plies
    assert b.turn == "b"
    assert b.at((2, 3)) == "B"  # Bc4 played, Nd4 not yet
    assert (
        LichessPuzzleSource.position({"puzzle": {"fen": "8/8/8/8/8/8/8/K6k b - - 0 1"}}).turn == "b"
    )
    with pytest.raises(SourceError):
        LichessPuzzleSource.position({"game": {"pgn": "e5"}, "puzzle": {}})


def test_lichess_card(web, http, data_dir):
    web.route("https://lichess.org/api/puzzle/daily", PUZZLE)
    card = make("type: lichess_puzzle\nname: L", http, data_dir).fetch()
    rows = dict(card.meta["rows"])
    assert card.title == "Black to move"
    assert rows["Themes"] == "mate in2, discovered attack"
    assert rows["Game"] == "alice (1800) – bob (1750)"
    assert card.url == "https://lichess.org/training/abc12"
    assert web.requests[0].headers["Accept"] == "application/json"


def test_humanize():
    assert humanize("discoveredAttack") == "discovered attack"
