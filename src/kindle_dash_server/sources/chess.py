"""Just enough chess to replay a game in SAN and get the final position (for the Lichess
puzzle source): legal-move disambiguation incl. pins, castling, en passant, promotion.

Written here instead of depending on python-chess (GPL-3) to keep the project MIT.
"""

from __future__ import annotations

import re

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
FILES = "abcdefgh"
_SAN_RE = re.compile(r"^([NBRQK])?([a-h])?([1-8])?(x)?([a-h][1-8])(?:=?([NBRQ]))?$")
_KNIGHT = [(1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2)]
_KING = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
_ROOK_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]
_BISHOP_DIRS = [(1, 1), (1, -1), (-1, 1), (-1, -1)]

Square = tuple[int, int]  # (file 0-7, rank 0-7)


class ChessError(ValueError):
    pass


def sq(name: str) -> Square:
    return FILES.index(name[0]), int(name[1]) - 1


def sq_name(s: Square) -> str:
    return f"{FILES[s[0]]}{s[1] + 1}"


class Board:
    def __init__(self, fen: str = START_FEN):
        parts = fen.split()
        if len(parts) < 4:
            raise ChessError(f"bad FEN {fen!r}")
        self.grid: list[list[str | None]] = [[None] * 8 for _ in range(8)]  # [rank][file]
        for i, row in enumerate(parts[0].split("/")):
            rank, f = 7 - i, 0
            for ch in row:
                if ch.isdigit():
                    f += int(ch)
                else:
                    self.grid[rank][f] = ch
                    f += 1
        self.turn = parts[1]  # "w" / "b"
        self.castling = "" if parts[2] == "-" else parts[2]
        self.ep: Square | None = None if parts[3] == "-" else sq(parts[3])

    # -- queries -------------------------------------------------------------------------------

    def at(self, s: Square) -> str | None:
        return self.grid[s[1]][s[0]]

    def _set(self, s: Square, piece: str | None) -> None:
        self.grid[s[1]][s[0]] = piece

    @staticmethod
    def color(piece: str) -> str:
        return "w" if piece.isupper() else "b"

    def king(self, color: str) -> Square:
        k = "K" if color == "w" else "k"
        for r in range(8):
            for f in range(8):
                if self.grid[r][f] == k:
                    return f, r
        raise ChessError(f"no {color} king")

    def attacked(self, s: Square, by: str) -> bool:
        f, r = s

        def own(p: str | None, kinds: str) -> bool:
            return p is not None and self.color(p) == by and p.upper() in kinds

        for df, dr in _KNIGHT:
            t = (f + df, r + dr)
            if _on(t) and own(self.at(t), "N"):
                return True
        for df, dr in _KING:
            t = (f + df, r + dr)
            if _on(t) and own(self.at(t), "K"):
                return True
        pr = r - 1 if by == "w" else r + 1  # a white pawn attacks upwards
        for df in (-1, 1):
            t = (f + df, pr)
            if _on(t) and own(self.at(t), "P"):
                return True
        for dirs, kinds in ((_ROOK_DIRS, "RQ"), (_BISHOP_DIRS, "BQ")):
            for df, dr in dirs:
                t = (f + df, r + dr)
                while _on(t):
                    p = self.at(t)
                    if p is not None:
                        if own(p, kinds):
                            return True
                        break
                    t = (t[0] + df, t[1] + dr)
        return False

    def _reaches(self, frm: Square, to: Square, kind: str, capture: bool) -> bool:
        """Pseudo-legal: can a ``kind`` piece on frm move to ``to`` (ignoring checks)?"""
        df, dr = to[0] - frm[0], to[1] - frm[1]
        if kind == "N":
            return (df, dr) in _KNIGHT
        if kind == "K":
            return max(abs(df), abs(dr)) == 1
        if kind == "P":
            fwd = 1 if self.turn == "w" else -1
            if capture:
                return abs(df) == 1 and dr == fwd
            start = 1 if self.turn == "w" else 6
            if df != 0:
                return False
            if dr == fwd:
                return True
            mid = (frm[0], frm[1] + fwd)
            return dr == 2 * fwd and frm[1] == start and self.at(mid) is None
        straight = df == 0 or dr == 0
        diagonal = abs(df) == abs(dr)
        if (
            (kind == "R" and not straight)
            or (kind == "B" and not diagonal)
            or (kind == "Q" and not (straight or diagonal))
        ):
            return False
        step = ((df > 0) - (df < 0), (dr > 0) - (dr < 0))
        t = (frm[0] + step[0], frm[1] + step[1])
        while t != to:
            if self.at(t) is not None:
                return False
            t = (t[0] + step[0], t[1] + step[1])
        return True

    def _leaves_king_safe(self, frm: Square, to: Square, ep_capture: Square | None) -> bool:
        moving, captured = self.at(frm), self.at(to)
        ep_piece = self.at(ep_capture) if ep_capture else None
        self._set(to, moving)
        self._set(frm, None)
        if ep_capture:
            self._set(ep_capture, None)
        try:
            return not self.attacked(self.king(self.turn), "b" if self.turn == "w" else "w")
        finally:
            self._set(frm, moving)
            self._set(to, captured)
            if ep_capture:
                self._set(ep_capture, ep_piece)

    # -- moves ---------------------------------------------------------------------------------

    def push_san(self, san: str) -> None:
        san = san.strip().rstrip("+#!?")
        if san in ("O-O", "0-0", "O-O-O", "0-0-0"):
            self._castle(long=san.count("O") + san.count("0") == 3)
            return
        m = _SAN_RE.match(san)
        if not m:
            raise ChessError(f"can't parse move {san!r}")
        kind, ffile, frank, capture, to_s, promo = m.groups()
        kind = kind or "P"
        to = sq(to_s)
        capture = bool(capture)
        target = self.at(to)
        if target is not None and self.color(target) == self.turn:
            raise ChessError(f"{san}: own piece on {to_s}")
        ep_capture = None
        if kind == "P" and capture and target is None:
            if self.ep != to:
                raise ChessError(f"{san}: nothing to capture")
            ep_capture = (to[0], to[1] - (1 if self.turn == "w" else -1))

        letter = kind if self.turn == "w" else kind.lower()
        found = []
        for r in range(8):
            for f in range(8):
                if self.grid[r][f] != letter:
                    continue
                if (ffile and FILES[f] != ffile) or (frank and r != int(frank) - 1):
                    continue
                frm = (f, r)
                if self._reaches(
                    frm, to, kind, capture or target is not None
                ) and self._leaves_king_safe(frm, to, ep_capture):
                    found.append(frm)
        if len(found) != 1:
            raise ChessError(f"{san}: {'ambiguous' if found else 'illegal'} in this position")
        frm = found[0]

        self._set(to, letter if not promo else (promo if self.turn == "w" else promo.lower()))
        self._set(frm, None)
        if ep_capture:
            self._set(ep_capture, None)
        self.ep = None
        if kind == "P" and abs(to[1] - frm[1]) == 2:
            self.ep = (frm[0], (frm[1] + to[1]) // 2)
        self._update_castling(frm, to)
        self.turn = "b" if self.turn == "w" else "w"

    def _castle(self, long: bool) -> None:
        r = 0 if self.turn == "w" else 7
        k, rook = ("K", "R") if self.turn == "w" else ("k", "r")
        right = ("Q" if long else "K") if self.turn == "w" else ("q" if long else "k")
        if right not in self.castling:
            raise ChessError("castling not allowed")
        between = (1, 2, 3) if long else (5, 6)
        if any(self.at((f, r)) is not None for f in between):
            raise ChessError("castling through pieces")
        enemy = "b" if self.turn == "w" else "w"
        king_path = (4, 3, 2) if long else (4, 5, 6)  # not out of, through or into check
        if any(self.attacked((f, r), enemy) for f in king_path):
            raise ChessError("castling through check")
        if long:
            self._set((2, r), k)
            self._set((3, r), rook)
            self._set((0, r), None)
        else:
            self._set((6, r), k)
            self._set((5, r), rook)
            self._set((7, r), None)
        self._set((4, r), None)
        self.castling = "".join(
            c for c in self.castling if c not in ("KQ" if self.turn == "w" else "kq")
        )
        self.ep = None
        self.turn = "b" if self.turn == "w" else "w"

    def _update_castling(self, frm: Square, to: Square) -> None:
        lost = {(4, 0): "KQ", (0, 0): "Q", (7, 0): "K", (4, 7): "kq", (0, 7): "q", (7, 7): "k"}
        for s in (frm, to):
            if s in lost:
                self.castling = "".join(c for c in self.castling if c not in lost[s])

    def placement(self) -> str:
        rows = []
        for r in range(7, -1, -1):
            row, empty = "", 0
            for f in range(8):
                p = self.grid[r][f]
                if p is None:
                    empty += 1
                else:
                    row += (str(empty) if empty else "") + p
                    empty = 0
            rows.append(row + (str(empty) if empty else ""))
        return "/".join(rows)


def _on(s: Square) -> bool:
    return 0 <= s[0] < 8 and 0 <= s[1] < 8


def san_tokens(pgn: str) -> list[str]:
    """Moves from a PGN movetext (tags, comments, move numbers and results removed)."""
    pgn = re.sub(r"\[[^\]]*\]", " ", pgn)
    pgn = re.sub(r"\{[^}]*\}|\([^)]*\)|\$\d+", " ", pgn)
    out = []
    for tok in pgn.split():
        tok = re.sub(r"^\d+\.(\.\.)?", "", tok)
        if tok and tok not in ("1-0", "0-1", "1/2-1/2", "*"):
            out.append(tok)
    return out


def replay(pgn: str, plies: int | None = None) -> Board:
    board = Board()
    for i, move in enumerate(san_tokens(pgn)):
        if plies is not None and i >= plies:
            break
        board.push_san(move)
    return board
