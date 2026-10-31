"""Runtime state in SQLite: what's on screen, per-source status, recent events."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_status (
    source_id TEXT PRIMARY KEY,
    last_ok_at REAL,
    last_error TEXT,
    last_error_at REAL,
    last_shown_at REAL,
    shown_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    source_id TEXT,
    message TEXT NOT NULL
);
"""

MAX_EVENTS = 300


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    # -- kv ----------------------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, default=str)),
            )

    # -- sources -----------------------------------------------------------------------------

    def _ensure(self, sid: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO source_status(source_id) VALUES(?)", (sid,))

    def source_ok(self, sid: str) -> None:
        with self._lock:
            self._ensure(sid)
            self._db.execute(
                "UPDATE source_status SET last_ok_at=?, last_error=NULL WHERE source_id=?",
                (time.time(), sid),
            )

    def source_error(self, sid: str, message: str) -> None:
        with self._lock:
            self._ensure(sid)
            self._db.execute(
                "UPDATE source_status SET last_error=?, last_error_at=? WHERE source_id=?",
                (message, time.time(), sid),
            )

    def source_shown(self, sid: str) -> None:
        with self._lock:
            self._ensure(sid)
            self._db.execute(
                "UPDATE source_status SET last_shown_at=?, "
                "shown_count=shown_count+1 WHERE source_id=?",
                (time.time(), sid),
            )

    def source_status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM source_status").fetchall()
        return {r["source_id"]: dict(r) for r in rows}

    def forget_source(self, sid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM source_status WHERE source_id=?", (sid,))

    # -- events ------------------------------------------------------------------------------

    def event(self, level: str, message: str, source_id: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events(ts, level, source_id, message) VALUES(?, ?, ?, ?)",
                (time.time(), level, source_id, message),
            )
            self._db.execute(
                "DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (MAX_EVENTS,)
            )

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
