"""Renders ahead of time, on clock-aligned slots, so the device endpoint only serves a file.

With ``interval_minutes: 15`` and ``lead_seconds: 60`` it renders at :14, :29, :44, :59,
ready for a Kindle that wakes at :00, :15, :30, :45 (INTERVAL_MINUTES=15 in env.sh).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta

from .engine import Engine

log = logging.getLogger(__name__)


def next_slot(now: datetime, interval_minutes: int, lead_seconds: int) -> datetime:
    """Next render time: a multiple of the interval (from local midnight) minus the lead."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    step = timedelta(minutes=interval_minutes)
    lead = timedelta(seconds=lead_seconds)
    n = int((now - midnight + lead) / step) + 1
    slot = midnight + n * step - lead
    while slot <= now:
        slot += step
    return slot


class Scheduler:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.next_run: datetime | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="render-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def reschedule(self) -> None:
        """Settings changed: recompute the next slot."""
        self._wake.set()

    async def render_now(self, reason: str = "manual"):
        return await asyncio.to_thread(self.engine.render_display, reason)

    async def _run(self) -> None:
        current = self.engine.state.get("current") or {}
        sched = self.engine.settings.scheduler
        age = time.time() - current.get("rendered_at", 0)
        if current.get("reason") == "placeholder" or age > sched.interval_minutes * 60:
            await self.render_now("startup")
        last_prune = 0.0
        while True:
            sched = self.engine.settings.scheduler
            self.next_run = next_slot(self.engine.now(), sched.interval_minutes, sched.lead_seconds)
            delay = (self.next_run - self.engine.now()).total_seconds()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, delay))
                continue  # woken up early: settings changed, recompute
            except TimeoutError:
                pass
            try:
                await self.render_now("schedule")
            except Exception:
                log.exception("scheduled render crashed")
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                await asyncio.to_thread(self.engine.http.prune)
