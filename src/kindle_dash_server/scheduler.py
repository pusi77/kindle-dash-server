"""Renders once before each fetch of the Kindle, and never in between.

The fetch times come from the device's own schedule (see ``Engine.device_schedule``): with
kindle-dash-pw3's ``*/15 7-22 * * *`` the image is rendered at 06:59, 07:14 … 22:29, and not at
all during the night (the Kindle shows its sleep screen at 22:45 and fetches again at 07:00).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta

from .engine import Engine, RenderResult

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.next_run: datetime | None = None
        self.next_fetch: datetime | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="render-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def reschedule(self) -> None:
        """Schedule or settings changed: recompute the next render."""
        self._wake.set()

    async def render_now(self, reason: str = "manual", at: datetime | None = None) -> RenderResult:
        return await asyncio.to_thread(self.engine.render_display, reason, at)

    def _plan(self) -> tuple[datetime, datetime]:
        """(render time, fetch it's for): the first fetch we can still render ahead of."""
        lead = timedelta(seconds=self.engine.settings.scheduler.lead_seconds)
        fetch = self.engine.next_fetch(self.engine.now() + lead)
        return fetch - lead, fetch

    async def _run(self) -> None:
        # Startup: make sure the image for the very next fetch is ready.
        try:
            upcoming = self.engine.next_fetch()
            current = self.engine.state.get("current") or {}
            if current.get("target") != upcoming.isoformat():
                await self.render_now("startup", upcoming)
        except Exception:
            log.exception("startup render failed")
        last_prune = 0.0
        while True:
            try:
                self.next_run, self.next_fetch = self._plan()
            except Exception:
                log.exception("can't compute the next fetch; retrying in 5 min")
                self.next_run = self.next_fetch = None
                await asyncio.sleep(300)
                continue
            delay = (self.next_run - self.engine.now()).total_seconds()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, delay))
                continue  # woken up early: schedule changed, recompute
            except TimeoutError:
                pass
            try:
                await self.render_now("schedule", self.next_fetch)
            except Exception:
                log.exception("scheduled render crashed")
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                await asyncio.to_thread(self.engine.http.prune)
