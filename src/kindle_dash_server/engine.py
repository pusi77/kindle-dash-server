"""The engine: sources -> cards (cached) -> layouts -> PNG, and what the device is shown."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .card import Card
from .config import Paths, Settings, SettingsStore
from .net import HttpClient
from .render.fonts import add_font_dir
from .render.layouts import RenderContext, render_card
from .render.pipeline import encode_png, finalize
from .sources import FilteredOut, Source, SourceConfig, SourceContext
from .sources.base import SOURCE_TYPES
from .sources.loader import load_sources, seed_defaults
from .state import State

log = logging.getLogger(__name__)

MAX_FALLBACKS = 3


@dataclass
class RenderResult:
    ok: bool
    source_id: str | None
    message: str
    duration: float = 0.0


def resolve_layout(config: SourceConfig, card: Card) -> str:
    if config.layout != "auto":
        return config.layout
    if card.layout:
        return card.layout
    if card.image is not None:
        return "art"
    if card.title and card.body:
        return "article"
    return "quote"


class Engine:
    def __init__(
        self, paths: Paths, store: SettingsStore, state: State, http: HttpClient | None = None
    ):
        self.paths = paths
        self.store = store
        self.state = state
        paths.ensure()
        add_font_dir(paths.fonts)
        self.http = http or HttpClient(paths.http_cache, contact=store.settings.contact)
        self.sources: dict[str, Source] = {}
        self.load_errors: dict[str, str] = {}
        self.source_files: dict[str, Path] = {}
        self._render_lock = threading.Lock()
        self._source_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._current: bytes | None = None
        if paths.current_png.exists():
            self._current = paths.current_png.read_bytes()

    @property
    def settings(self) -> Settings:
        return self.store.settings

    # -- setup -------------------------------------------------------------------------------

    def bootstrap(self) -> None:
        copied = seed_defaults(self.paths.sources)
        if copied:
            log.info("installed default sources: %s", ", ".join(copied))
        self.reload_sources()
        if self._current is None:
            self._write_current(self.placeholder(), source_id=None, reason="placeholder")

    def reload_sources(self) -> None:
        res = load_sources(self.paths.sources)
        ctx = self.source_context()
        self.sources = {
            sid: SOURCE_TYPES[cfg.type](sid, cfg, ctx) for sid, cfg in res.configs.items()
        }
        self.load_errors = res.errors
        self.source_files = res.files

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.location.timezone))

    def source_context(self) -> SourceContext:
        return SourceContext(
            http=self.http, paths=self.paths, settings=self.settings, now=self.now()
        )

    # -- cards -------------------------------------------------------------------------------

    def _card_path(self, sid: str) -> Path:
        return self.paths.cards / f"{sid}.json"

    def get_card(self, sid: str, force: bool = False) -> Card:
        """Cached card if still fresh, otherwise fetch (retrying candidates the filters reject)."""
        src = self.sources[sid]
        src.ctx = self.source_context()
        meta_key = f"card:{sid}"
        with self._source_locks[sid]:
            path = self._card_path(sid)
            meta = self.state.get(meta_key) or {}
            fresh = (
                path.exists()
                and not force
                and time.time() - meta.get("fetched_at", 0) < src.config.refresh
                and meta.get("cache_key") == src.cache_key()
                and meta.get("config") == src.config.model_dump_json()
            )
            if fresh:
                try:
                    return Card.load(path)
                except Exception as exc:  # corrupt cache: refetch
                    log.warning("card cache for %s unreadable: %s", sid, exc)
            card = None
            last: Exception | None = None
            for _ in range(src.config.filters.max_attempts):
                try:
                    card = src.fetch()
                    if card.image is not None:
                        src.check_image(card.image)
                    break
                except FilteredOut as exc:
                    last = exc
                    continue
                except Exception as exc:
                    self.state.source_error(sid, f"{type(exc).__name__}: {exc}")
                    self.state.event("error", f"fetch failed: {exc}", sid)
                    raise
            if card is None:
                msg = f"nothing passed the filters ({last})"
                self.state.source_error(sid, msg)
                self.state.event("error", msg, sid)
                raise FilteredOut(msg)
            if not card.source_name:
                card.source_name = src.config.name
            card.save(path)
            self.state.set(
                meta_key,
                {
                    "fetched_at": time.time(),
                    "cache_key": src.cache_key(),
                    "config": src.config.model_dump_json(),
                },
            )
            self.state.source_ok(sid)
            return card

    # -- rendering ---------------------------------------------------------------------------

    def render_ctx(self, options: dict[str, Any] | None = None) -> RenderContext:
        s = self.settings
        return RenderContext(
            size=s.output.canvas_size,
            font=s.font,
            output=s.output,
            now=self.now(),
            options=options or {},
        )

    def render_png(self, card: Card, config: SourceConfig) -> bytes:
        ctx = self.render_ctx(config.layout_options)
        img = render_card(card, resolve_layout(config, card), ctx)
        return encode_png(finalize(img, self.settings.output))

    def render_source(self, sid: str, force: bool = False) -> bytes:
        card = self.get_card(sid, force=force)
        png = self.render_png(card, self.sources[sid].config)
        out = self.paths.previews / f"{sid}.png"
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(out)
        return png

    def placeholder(self, message: str | None = None) -> bytes:
        card = Card(
            title="kindle-dash-server",
            body=message
            or "Waiting for the first render. Open the web UI to choose what this screen shows.",
            credit="kindle-dash-server",
            source_name="Status",
        )
        img = render_card(card, "article", self.render_ctx())
        return encode_png(finalize(img, self.settings.output))

    # -- what the device shows -----------------------------------------------------------------

    @property
    def current_png(self) -> bytes | None:
        return self._current

    def _write_current(self, png: bytes, source_id: str | None, reason: str) -> None:
        tmp = self.paths.current_png.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(self.paths.current_png)
        self._current = png
        self.state.set(
            "current",
            {
                "source_id": source_id,
                "rendered_at": time.time(),
                "reason": reason,
                "mode": self.settings.selection.mode,
            },
        )

    def enabled_ids(self) -> list[str]:
        return [sid for sid, s in self.sources.items() if s.config.enabled]

    def candidates(self) -> list[str]:
        """Sources to try, in order: the chosen one first, then fallbacks."""
        sel = self.settings.selection
        enabled = self.enabled_ids()
        if sel.mode == "pinned" and sel.pinned in self.sources:
            return [sel.pinned]  # pinned: no fallback to other sources, keep the last image
        pool = [s for s in sel.rotation_sources if s in enabled] or enabled
        return self._rotation_order(pool, sel.strategy)

    def _rotation_order(self, pool: list[str], strategy: str) -> list[str]:
        if not pool:
            return []
        if strategy == "round_robin":
            last = self.state.get("rr_last")
            start = (pool.index(last) + 1) % len(pool) if last in pool else 0
            return pool[start:] + pool[:start]
        # weighted random without replacement; avoid repeating the previous source
        current = (self.state.get("current") or {}).get("source_id")
        remaining = list(pool)
        order = []
        rng = random.Random()
        while remaining:
            weights = [max(self.sources[s].config.weight, 0.0) for s in remaining]
            if current in remaining and len(remaining) > 1 and not order:
                weights[remaining.index(current)] *= 0.1
            if sum(weights) <= 0:
                weights = [1.0] * len(remaining)
            pick = rng.choices(remaining, weights)[0]
            order.append(pick)
            remaining.remove(pick)
        return order

    def render_display(self, reason: str = "schedule") -> RenderResult:
        """Render the next image for the device. On failure, the last good image stays."""
        t0 = time.monotonic()
        with self._render_lock:
            candidates = self.candidates()
            if not candidates:
                msg = "no enabled source"
                self.state.event("warning", msg)
                return RenderResult(False, None, msg)
            errors = []
            for sid in candidates[:MAX_FALLBACKS]:
                try:
                    png = self.render_source(sid)
                except Exception as exc:
                    log.warning("render %s failed: %s", sid, exc)
                    errors.append(f"{sid}: {exc}")
                    continue
                self._write_current(png, sid, reason)
                self.state.source_shown(sid)
                if self.settings.selection.strategy == "round_robin":
                    self.state.set("rr_last", sid)
                dt = time.monotonic() - t0
                self.state.event("info", f"rendered ({reason}) in {dt:.1f}s", sid)
                return RenderResult(True, sid, "ok", dt)
            msg = "all candidates failed, keeping the last image: " + "; ".join(errors)
            self.state.event("error", msg[:1000])
            return RenderResult(False, None, msg, time.monotonic() - t0)
