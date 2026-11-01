"""The engine: sources -> cards (cached) -> layouts -> PNG, and what the device is shown.

Every render is *for* a moment, ``at``: normally the Kindle's next fetch. Daily picks, dates,
the moon phase and schedule slots are all evaluated at that moment.
"""

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
from .cron import DeviceSchedule
from .fsutil import atomic_write
from .net import HttpClient
from .render.fonts import add_font_dir
from .render.layouts import RenderContext, render_card
from .render.overlay import battery_badge, reserve_badge
from .render.pipeline import encode_png, finalize
from .sources import FilteredOut, Source, SourceConfig, SourceContext
from .sources.base import SOURCE_TYPES
from .sources.loader import load_sources, seed_defaults
from .sources.plugins import ensure_plugin_sources, load_plugins
from .state import State

log = logging.getLogger(__name__)


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
        self.plugin_errors: dict[str, str] = {}
        self.plugin_files: dict[str, list[str]] = {}
        self.source_files: dict[str, Path] = {}
        self._render_lock = threading.Lock()
        self._source_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._schedule_cache: tuple[Any, DeviceSchedule, str] | None = None
        self._current: bytes | None = None
        if paths.current_png.exists():
            self._current = paths.current_png.read_bytes()

    @property
    def settings(self) -> Settings:
        return self.store.settings

    def save_settings(self, settings: Settings) -> None:
        self.store.save(settings)
        self.http.set_contact(settings.contact)
        self._schedule_cache = None

    # -- setup -------------------------------------------------------------------------------

    def bootstrap(self) -> None:
        copied = seed_defaults(self.paths.sources)
        if copied:
            log.info("installed default sources: %s", ", ".join(copied))
        self.reload_sources()
        if self._current is None:
            self._write_current(self.placeholder(), source_id=None, reason="placeholder")

    def reload_sources(self) -> None:
        """Re-import plugins and re-read every source file."""
        plugins = load_plugins(self.paths.plugins)
        self.plugin_errors, self.plugin_files = plugins.errors, plugins.files
        res = load_sources(self.paths.sources)
        if plugins.types:
            used = {c.type for c in res.configs.values()}
            used |= {t for t in plugins.types if _file_mentions_type(res.files.values(), t)}
            created = ensure_plugin_sources(self.paths.sources, plugins.types, used)
            if created:
                log.info("created source files for plugins: %s", ", ".join(created))
                res = load_sources(self.paths.sources)
        self.sources = {
            sid: SOURCE_TYPES[cfg.type](sid, cfg, self.source_context())
            for sid, cfg in res.configs.items()
        }
        self.load_errors = res.errors
        self.source_files = res.files

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.location.timezone))

    def _local(self, at: datetime | None) -> datetime:
        return at.astimezone(ZoneInfo(self.settings.location.timezone)) if at else self.now()

    def source_context(self, at: datetime | None = None) -> SourceContext:
        return SourceContext(
            http=self.http, paths=self.paths, settings=self.settings, now=self._local(at)
        )

    # -- the device's schedule -------------------------------------------------------------------

    def device_schedule(self) -> tuple[DeviceSchedule, str]:
        """(schedule, origin): origin is "device" when reported by the Kindle, else "settings"."""
        sch = self.settings.scheduler
        reported = self.state.get("device_schedule") if sch.follow_device else None
        key = (
            reported
            and (reported.get("schedule"), reported.get("timezone"), reported.get("sleep_after")),
            sch.schedule,
            sch.timezone,
            sch.sleep_after,
            self.settings.location.timezone,
        )
        if self._schedule_cache and self._schedule_cache[0] == key:
            return self._schedule_cache[1], self._schedule_cache[2]
        schedule, origin = None, "settings"
        if reported:
            try:
                schedule = DeviceSchedule.create(
                    reported["schedule"], reported["timezone"], reported.get("sleep_after")
                )
                origin = "device"
            except Exception as exc:
                log.warning("ignoring the schedule reported by the device: %s", exc)
        if schedule is None:
            schedule = DeviceSchedule.create(
                sch.schedule, sch.timezone or self.settings.location.timezone, sch.sleep_after
            )
        self._schedule_cache = (key, schedule, origin)
        return schedule, origin

    def next_fetch(self, after: datetime | None = None) -> datetime:
        schedule, _ = self.device_schedule()
        return schedule.next_fetch(after or self.now())

    def record_device_request(self, headers: dict[str, str], ip: str | None) -> bool:
        """Remember battery level and the reported schedule. True if the schedule changed."""

        def h(name: str) -> str | None:
            v = headers.get(name)
            return v.strip() if v and v.strip() else None

        battery = h("x-kindle-battery")
        self.state.set(
            "device",
            {
                "last_seen": time.time(),
                "battery": int(battery) if battery and battery.isdigit() else None,
                "ip": ip,
                "user_agent": h("user-agent"),
            },
        )
        schedule, tz = h("x-kindle-schedule"), h("x-kindle-timezone")
        if not (schedule and tz):
            return False
        sleep_after = h("x-kindle-sleep-after")
        reported = {
            "schedule": " ".join(schedule.split()),
            "timezone": tz,
            "sleep_after": int(sleep_after) if sleep_after and sleep_after.isdigit() else None,
        }
        old = self.state.get("device_schedule") or {}
        if {k: old.get(k) for k in reported} == reported:
            return False
        try:
            DeviceSchedule.create(reported["schedule"], tz, reported["sleep_after"])
        except Exception as exc:
            self.state.event(
                "warning",
                f"device sent an unusable schedule {reported['schedule']!r} / {tz!r}: {exc}",
            )
            return False
        self.state.set("device_schedule", {**reported, "received_at": time.time()})
        self.state.event(
            "info",
            f"device schedule: {reported['schedule']} ({tz}, sleep screen "
            f"after {reported['sleep_after'] or 'default'} s)",
        )
        self._schedule_cache = None
        return True

    def battery_level(self) -> int | None:
        return (self.state.get("device") or {}).get("battery")

    # -- cards -------------------------------------------------------------------------------

    def _card_path(self, sid: str) -> Path:
        return self.paths.cards / f"{sid}.json"

    def _fetch_card(self, src: Source) -> Card:
        """src.fetch(), retried while the result is rejected by the size/aspect filters."""
        last: Exception | None = None
        for _ in range(src.config.filters.max_attempts):
            try:
                card = src.fetch()
                if card.image is not None:
                    src.check_image(card.image)
            except FilteredOut as exc:
                last = exc
                continue
            if not card.source_name:
                card.source_name = src.config.name
            return card
        raise FilteredOut(f"nothing passed the filters ({last})")

    def get_card(self, sid: str, force: bool = False, at: datetime | None = None) -> Card:
        """Cached card if still fresh, otherwise a new one from the source."""
        src = self.sources[sid]
        src.ctx = self.source_context(at)
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
            try:
                card = self._fetch_card(src)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                self.state.source_error(sid, msg)
                self.state.event("error", f"fetch failed: {msg}", sid)
                raise
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

    def render_ctx(
        self, options: dict[str, Any] | None = None, at: datetime | None = None
    ) -> RenderContext:
        s = self.settings
        return RenderContext(
            size=s.output.canvas_size,
            font=s.font,
            output=s.output,
            now=self._local(at),
            options=options or {},
        )

    def render_png(self, card: Card, config: SourceConfig, at: datetime | None = None) -> bytes:
        ctx = self.render_ctx(config.layout_options, at)
        badge = self._badge_level()
        if badge is not None:
            reserve_badge(badge, ctx, self.settings.battery.position)
        img = render_card(card, resolve_layout(config, card), ctx)
        if badge is not None:
            battery_badge(img, badge, ctx, self.settings.battery.position)
        return encode_png(finalize(img, self.settings.output))

    def _badge_level(self) -> int | None:
        """Battery level to show, or None when it's fine (or unknown)."""
        b = self.settings.battery
        level = self.battery_level()
        if b.show_below and level is not None and level <= b.show_below:
            return level
        return None

    def render_source(self, sid: str, force: bool = False, at: datetime | None = None) -> bytes:
        card = self.get_card(sid, force=force, at=at)
        png = self.render_png(card, self.sources[sid].config, at)
        out = self.paths.previews / f"{sid}.png"
        atomic_write(out, png)
        return png

    def render_config(self, sid: str, config: SourceConfig) -> bytes:
        """Render an unsaved source definition (editor preview). Nothing is cached."""
        src = SOURCE_TYPES[config.type](sid, config, self.source_context())
        return self.render_png(self._fetch_card(src), config)

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

    def _write_current(
        self, png: bytes, source_id: str | None, reason: str, at: datetime | None = None
    ) -> None:
        atomic_write(self.paths.current_png, png)
        self._current = png
        self.state.set(
            "current",
            {
                "source_id": source_id,
                "rendered_at": time.time(),
                "reason": reason,
                "mode": self.settings.selection.mode,
                "target": at.isoformat() if at else None,
            },
        )

    def enabled_ids(self) -> list[str]:
        return [sid for sid, s in self.sources.items() if s.config.enabled]

    def candidates(self, at: datetime | None = None) -> list[str]:
        """Sources to try, in order: the chosen one first, then fallbacks."""
        sel = self.settings.selection
        enabled = self.enabled_ids()
        pool = [s for s in sel.rotation_sources if s in enabled] or enabled
        if sel.mode == "pinned" and sel.pinned in self.sources:
            return [sel.pinned]  # pinned: no fallback to other sources, keep the last image
        if sel.mode == "schedule":
            local = self._local(at)
            for slot in sel.slots:
                if slot.matches(local):
                    if slot.source in self.sources:
                        rest = [
                            s for s in self._rotation_order(pool, sel.strategy) if s != slot.source
                        ]
                        return [slot.source, *rest]
                    break  # slot without a source: the rotation
        return self._rotation_order(pool, sel.strategy)

    def active_slot(self, at: datetime | None = None) -> int | None:
        sel = self.settings.selection
        if sel.mode != "schedule":
            return None
        local = self._local(at)
        return next((i for i, s in enumerate(sel.slots) if s.matches(local)), None)

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

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Wait for a render in progress to finish (used at shutdown). False on timeout."""
        if self._render_lock.acquire(timeout=timeout):
            self._render_lock.release()
            return True
        return False

    def _fallback_order(self, candidates: list[str]) -> list[str]:
        """The chosen source first, then the others: healthy ones (no error since their last
        success) before failing ones, so a dead API doesn't delay the fallback."""
        if len(candidates) <= 1:
            return candidates
        status = self.state.source_status()

        def failing(sid: str) -> bool:
            return bool(status.get(sid, {}).get("last_error"))

        rest = candidates[1:]
        return [
            candidates[0],
            *[s for s in rest if not failing(s)],
            *[s for s in rest if failing(s)],
        ]

    def render_display(self, reason: str = "schedule", at: datetime | None = None) -> RenderResult:
        """Render the image for the device's fetch at ``at`` (default: the next one).
        On failure the last good image stays."""
        t0 = time.monotonic()
        if at is None:
            at = self.next_fetch()
        with self._render_lock:
            candidates = self.candidates(at)
            if not candidates:
                msg = "no enabled source"
                self.state.event("warning", msg)
                return RenderResult(False, None, msg)
            errors = []
            for sid in self._fallback_order(candidates):
                try:
                    png = self.render_source(sid, at=at)
                except Exception as exc:
                    log.warning("render %s failed: %s", sid, exc)
                    errors.append(f"{sid}: {exc}")
                    continue
                self._write_current(png, sid, reason, at)
                self.state.source_shown(sid)
                if self.settings.selection.strategy == "round_robin":
                    self.state.set("rr_last", sid)
                dt = time.monotonic() - t0
                self.state.event(
                    "info", f"rendered ({reason}) for the {at:%H:%M} fetch in {dt:.1f}s", sid
                )
                return RenderResult(True, sid, "ok", dt)
            msg = "all candidates failed, keeping the last image: " + "; ".join(errors)
            self.state.event("error", msg[:1000])
            return RenderResult(False, None, msg, time.monotonic() - t0)


def _file_mentions_type(paths: Any, type_name: str) -> bool:
    """A source file of this type exists, even if it currently fails validation."""
    import re

    pat = re.compile(rf"^type\s*:\s*['\"]?{re.escape(type_name)}['\"]?\s*$", re.MULTILINE)
    for p in paths:
        try:
            if pat.search(Path(p).read_text(encoding="utf-8")):
                return True
        except OSError:
            pass
    return False
