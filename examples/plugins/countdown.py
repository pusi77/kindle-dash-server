"""Example plugin source: days left until a date.

Copy this file to /data/plugins/ and press "Reload files & plugins" on the Sources page
(or restart). A disabled "Countdown" source appears; edit its target date and enable it.
"""

from datetime import date
from typing import ClassVar, Literal

from kindle_dash_server.card import Card
from kindle_dash_server.sources import Source, SourceConfig, SourceError


class CountdownConfig(SourceConfig):
    # Your options, on top of the common ones (name, enabled, layout, refresh, weight, ...).
    type: Literal["countdown"] = "countdown"
    target: date
    event: str = "the big day"
    layout: str = "quote"


class Countdown(Source):
    """Days left until a date."""

    type_name = "countdown"  # used as `type:` in YAML
    Config = CountdownConfig
    # Written into the YAML file created when the plugin is first loaded.
    defaults: ClassVar[dict] = {
        "name": "Countdown",
        "target": "2027-01-01",
        "event": "New Year's Day",
    }

    def cache_key(self) -> str:
        # Cached cards are dropped when this changes: recompute every day.
        return self.ctx.today.isoformat()

    def fetch(self) -> Card:
        days = (self.config.target - self.ctx.today).days
        if days < 0:
            raise SourceError(f"{self.config.event} is in the past")
        text = "Today!" if days == 0 else f"{days} day{'s' if days != 1 else ''}"
        return Card(
            body=text,
            author=f"until {self.config.event}",
            title=self.config.target.strftime("%-d %B %Y"),
            source_name=self.config.name,
        )
