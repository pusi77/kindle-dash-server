"""Engine: what the device is shown, fallbacks, caching, device sync, battery badge, plugins."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from conftest import add_source, png_info
from PIL import Image

from kindle_dash_server.cron import DeviceSchedule

TZ = ZoneInfo("Europe/Rome")
QUOTES = "type: text_file\nname: {name}\npath: q.txt\nlayout: quote\nweight: {w}\n"
BROKEN = "type: json_api\nname: Broken\nurl: https://down.example/\nfields: {{}}\nweight: {w}\n"


def setup_sources(data_dir: Path, **specs: str) -> None:
    (data_dir / "q.txt").write_text("A quote.\n— Someone\n%\nAnother.\n— Else\n")
    for sid, text in specs.items():
        add_source(data_dir, sid, text)


def at(*args) -> datetime:
    return datetime(*args, tzinfo=TZ)


def test_bootstrap_installs_defaults_and_a_placeholder(make_engine):
    eng = make_engine()
    assert {"aphorisms", "generative", "aic-prints", "almanac"} <= set(eng.sources)
    assert eng.load_errors == {}
    assert png_info(eng.current_png)["width"] == 1072
    assert eng.state.get("current")["reason"] == "placeholder"


def test_render_display_writes_current_and_state(make_engine, data_dir):
    setup_sources(data_dir, quotes=QUOTES.format(name="Q", w=1))
    eng = make_engine(seed=False)
    target = at(2026, 11, 2, 7, 0)
    res = eng.render_display("test", at=target)
    assert res.ok and res.source_id == "quotes"
    cur = eng.state.get("current")
    assert cur["target"] == target.isoformat() and cur["reason"] == "test"
    assert (data_dir / "cache" / "current.png").read_bytes() == eng.current_png
    assert eng.state.source_status()["quotes"]["shown_count"] == 1


def test_failure_keeps_the_last_good_image(make_engine, data_dir):
    setup_sources(data_dir, quotes=QUOTES.format(name="Q", w=1))
    eng = make_engine(seed=False)
    assert eng.render_display().ok
    good = eng.current_png
    (data_dir / "sources" / "quotes.yaml").write_text(BROKEN.format(w=1))
    eng.reload_sources()
    res = eng.render_display()
    assert not res.ok and "keeping the last image" in res.message
    assert eng.current_png == good
    assert "HTTP 404" in eng.state.source_status()["quotes"]["last_error"]
    assert eng.state.events(5)[0]["level"] == "error"


def test_rotation_falls_back_to_a_working_source(make_engine, data_dir):
    setup_sources(data_dir, broken=BROKEN.format(w=1000), quotes=QUOTES.format(name="Q", w=1))
    eng = make_engine(seed=False)
    res = eng.render_display()
    assert res.ok and res.source_id == "quotes"


def test_fallback_prefers_healthy_sources(make_engine, data_dir):
    setup_sources(
        data_dir,
        a=QUOTES.format(name="A", w=1),
        b=QUOTES.format(name="B", w=1),
        c=QUOTES.format(name="C", w=1),
    )
    eng = make_engine(seed=False)
    eng.state.source_error("b", "down")
    assert eng._fallback_order(["a", "b", "c"]) == ["a", "c", "b"]
    assert eng._fallback_order(["b", "a", "c"]) == ["b", "a", "c"]  # the choice stays first


def test_pinned_has_no_fallback(make_engine, data_dir):
    setup_sources(data_dir, broken=BROKEN.format(w=1), quotes=QUOTES.format(name="Q", w=1))
    eng = make_engine(seed=False, selection={"mode": "pinned", "pinned": "broken"})
    assert eng.candidates() == ["broken"]
    assert not eng.render_display().ok


def test_weighted_rotation_and_disabled_sources(make_engine, data_dir):
    setup_sources(
        data_dir,
        heavy=QUOTES.format(name="H", w=50),
        light=QUOTES.format(name="L", w=1),
        off=QUOTES.format(name="Off", w=1000) + "enabled: false\n",
    )
    eng = make_engine(seed=False)
    firsts = [eng.candidates()[0] for _ in range(200)]
    assert "off" not in firsts
    assert firsts.count("heavy") > 150
    assert sorted(eng.candidates()) == ["heavy", "light"]


def test_round_robin(make_engine, data_dir):
    setup_sources(data_dir, a=QUOTES.format(name="A", w=1), b=QUOTES.format(name="B", w=1))
    eng = make_engine(seed=False, selection={"strategy": "round_robin"})
    shown = [eng.render_display().source_id for _ in range(4)]
    assert shown == ["a", "b", "a", "b"]


def test_rotation_subset(make_engine, data_dir):
    setup_sources(data_dir, a=QUOTES.format(name="A", w=1), b=QUOTES.format(name="B", w=1))
    eng = make_engine(seed=False, selection={"rotation_sources": ["b", "ghost"]})
    assert eng.candidates() == ["b"]


def test_schedule_slots(make_engine, data_dir):
    setup_sources(
        data_dir, morning=QUOTES.format(name="M", w=1), other=QUOTES.format(name="O", w=1)
    )
    eng = make_engine(
        seed=False,
        selection={
            "mode": "schedule",
            "slots": [
                {"days": "MON-FRI", "start": "07:00", "end": "09:00", "source": "morning"},
                {"days": "*", "start": "22:00", "end": "02:00", "source": "other"},
            ],
        },
    )
    assert eng.candidates(at(2026, 11, 2, 7, 30))[0] == "morning"  # Monday
    assert eng.candidates(at(2026, 11, 2, 23, 30))[0] == "other"  # crosses midnight
    assert eng.candidates(at(2026, 11, 3, 1, 59))[0] == "other"
    assert eng.active_slot(at(2026, 11, 1, 7, 30)) is None  # Sunday: rotation
    assert eng.active_slot(at(2026, 11, 2, 9, 0)) is None  # end is excluded


def test_card_cache_respects_refresh_and_daily_key(make_engine, data_dir, web):
    calls = []
    web.route("https://api.example/x", lambda r: (calls.append(1), _json({"t": len(calls)}))[1])
    add_source(
        data_dir,
        "api",
        "type: json_api\nname: A\nurl: https://api.example/x\n"
        "fields: {title: t}\nlayout: quote\nrefresh: 1h\nresponse_ttl: 0\npick: daily\n",
    )
    eng = make_engine(seed=False)
    day = at(2026, 11, 2, 7, 0)
    assert eng.get_card("api", at=day).title == "1"
    assert eng.get_card("api", at=day.replace(hour=9)).title == "1"  # cached
    assert eng.get_card("api", at=day.replace(day=3)).title == "2"  # new day, new pick
    assert eng.get_card("api", force=True, at=day.replace(day=3)).title == "3"
    # Editing the source invalidates its cache.
    path = data_dir / "sources" / "api.yaml"
    path.write_text(path.read_text().replace("name: A", "name: B"))
    eng.reload_sources()
    assert eng.get_card("api", at=day.replace(day=3)).title == "4"


def _json(obj):
    import httpx

    return httpx.Response(200, json=obj)


def test_render_is_for_the_fetch_time(make_engine):
    eng = make_engine()
    png = eng.render_source("almanac", at=at(2027, 1, 1, 7, 0))
    assert eng.get_card("almanac", at=at(2027, 1, 1, 7, 0)).meta["phase"] is not None
    assert png_info(png)["height"] == 1448


def test_battery_badge_only_when_low(make_engine, data_dir):
    setup_sources(data_dir, quotes=QUOTES.format(name="Q", w=1))
    eng = make_engine(seed=False, battery={"show_below": 20, "position": "top-right"})

    def corner():
        img = Image.open(__import__("io").BytesIO(eng.render_source("quotes")))
        return np.asarray(img)[20:90, -230:-20]

    assert corner().min() == 255  # no battery info yet: nothing drawn
    eng.record_device_request({"x-kindle-battery": "80"}, "1.2.3.4")
    assert corner().min() == 255
    eng.record_device_request({"x-kindle-battery": "15"}, "1.2.3.4")
    assert corner().min() == 0  # badge drawn
    assert eng.battery_level() == 15


def test_device_reports_its_schedule(make_engine):
    eng = make_engine()
    _, origin = eng.device_schedule()
    assert origin == "settings"
    headers = {
        "x-kindle-battery": "50",
        "x-kindle-schedule": "*/30  6-21 * * *",
        "x-kindle-timezone": "Europe/Rome",
        "x-kindle-sleep-after": "1860",
    }
    assert eng.record_device_request(headers, "1.2.3.4") is True
    assert eng.record_device_request(headers, "1.2.3.4") is False  # unchanged
    sched, origin = eng.device_schedule()
    assert origin == "device" and sched.cron.expr == "*/30 6-21 * * *"
    assert eng.next_fetch(at(2026, 11, 2, 21, 10)) == at(2026, 11, 3, 6, 0)


def test_device_schedule_rejected_when_unusable(make_engine):
    eng = make_engine()
    bad = {
        "x-kindle-schedule": "*/30 * * * *",
        "x-kindle-timezone": "Europe/Rome",
        "x-kindle-sleep-after": "960",
    }
    assert eng.record_device_request(bad, None) is False
    assert eng.device_schedule()[1] == "settings"
    assert "unusable schedule" in eng.state.events(1)[0]["message"]


def test_manual_mode_uses_settings(make_engine):
    eng = make_engine(scheduler={"mode": "manual", "schedule": "0 8 * * *"})
    eng.record_device_request(
        {"x-kindle-schedule": "*/15 * * * *", "x-kindle-timezone": "Europe/Rome"}, None
    )
    sched, origin = eng.device_schedule()
    assert origin == "settings" and sched.cron.expr == "0 8 * * *"
    assert isinstance(sched, DeviceSchedule)
    assert eng.schedule_headers() == {}  # nothing is sent to the Kindle


def test_legacy_follow_device_setting_is_migrated(make_engine):
    eng = make_engine(scheduler={"follow_device": False})
    assert eng.settings.scheduler.mode == "manual"


KINDLE_OLD = {
    "x-kindle-schedule": "*/15 7-22 * * *",
    "x-kindle-timezone": "Europe/Rome",
    "x-kindle-sleep-after": "960",
}
KINDLE_NEW = {**KINDLE_OLD, "x-kindle-schedule": "*/30 6-21 * * *", "x-kindle-sleep-after": "1860"}


def test_server_mode_sends_the_schedule_and_tracks_it(make_engine, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    eng = make_engine(scheduler={"mode": "server", "schedule": "*/30 6-21 * * *"})
    eng.record_device_request(KINDLE_OLD, None)

    # Not sent yet: the Kindle still follows its own schedule.
    assert eng.schedule_status() == "pending"
    assert eng.device_schedule()[0].cron.expr == "*/15 7-22 * * *"

    # The next image carries it.
    assert eng.schedule_headers() == {
        "X-Dash-Schedule": "*/30 6-21 * * *",
        "X-Dash-Sleep-After": "1860",
    }
    assert eng.schedule_status() == "sent"
    sched, origin = eng.device_schedule()
    assert origin == "server" and sched.cron.expr == "*/30 6-21 * * *"
    # Planned with the new schedule: every 30 min; the 21:30 wake-up shows the sleep screen
    # (next slot 06:00), so after 21:00 the next fetch is 06:00.
    assert eng.next_fetch(at(2026, 11, 2, 20, 10)) == at(2026, 11, 2, 20, 30)
    assert eng.next_fetch(at(2026, 11, 2, 21, 10)) == at(2026, 11, 3, 6, 0)

    # Sending again changes nothing; the Kindle confirms on its next request.
    eng.schedule_headers()
    clock[0] += 1800
    eng.record_device_request(KINDLE_NEW, None)
    assert eng.schedule_status() == "applied"
    assert eng.device_schedule()[1] == "server"


def test_server_mode_detects_a_kindle_that_ignores_it(make_engine, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    eng = make_engine(scheduler={"mode": "server", "schedule": "*/30 6-21 * * *"})
    eng.record_device_request(KINDLE_OLD, None)
    eng.schedule_headers()
    clock[0] += 900
    eng.record_device_request(KINDLE_OLD, None)  # fetched again, still the old schedule
    assert eng.schedule_status() == "rejected"
    sched, origin = eng.device_schedule()
    assert origin == "device" and sched.cron.expr == "*/15 7-22 * * *"  # plan with reality


def test_leaving_server_mode_sends_default_once(make_engine):
    eng = make_engine(scheduler={"mode": "server", "schedule": "*/30 6-21 * * *"})
    eng.schedule_headers()
    eng.save_settings(
        eng.settings.model_copy(
            update={"scheduler": eng.settings.scheduler.model_copy(update={"mode": "device"})}
        )
    )
    assert eng.schedule_headers() == {"X-Dash-Schedule": "default"}
    assert eng.schedule_headers() == {}


def test_server_mode_refuses_what_the_kindle_would_refuse():
    import pytest
    from pydantic import ValidationError

    from kindle_dash_server.config import SchedulerSettings
    from kindle_dash_server.cron import device_accepts

    with pytest.raises(ValidationError, match="at most one refresh every 5 min"):
        SchedulerSettings(mode="server", schedule="*/2 * * * *", sleep_after=180)
    SchedulerSettings(mode="device", schedule="*/2 * * * *", sleep_after=180)  # fine to follow
    assert device_accepts("0,30 7-22 * * MON-FRI") is None
    assert device_accepts("0-59/10 * * * *")  # ranges in the minute field aren't accepted
    assert device_accepts("0,1,2,3,4,5,6,7,8,9,10,11,12 * * * *")
    assert device_accepts("*/10 * * * *", min_minutes=15)


PLUGIN = '''
from typing import ClassVar, Literal
from kindle_dash_server.card import Card
from kindle_dash_server.sources import Source, SourceConfig

class HelloConfig(SourceConfig):
    type: Literal["hello"] = "hello"
    who: str = "world"
    layout: str = "quote"

class Hello(Source):
    """Says hello."""
    type_name = "hello"
    Config = HelloConfig
    defaults: ClassVar[dict] = {"name": "Hello", "who": "Kindle"}

    def fetch(self):
        return Card(body=f"Hello, {self.config.who}!")
'''


def test_plugins_load_and_get_a_disabled_source_once(make_engine, data_dir):
    plugins = data_dir / "plugins"
    plugins.mkdir()
    (plugins / "hello.py").write_text(PLUGIN)
    (plugins / "broken.py").write_text("import does_not_exist\n")
    (plugins / "_private.py").write_text("raise SystemExit")
    eng = make_engine(seed=False)
    assert eng.plugin_files == {"hello.py": ["hello"]}
    assert "does_not_exist" in eng.plugin_errors["broken.py"]
    src = eng.sources["hello"]
    assert src.config.enabled is False and src.config.who == "Kindle"
    assert eng.get_card("hello").body == "Hello, Kindle!"
    # Reloading doesn't duplicate; deleting the YAML doesn't bring it back.
    eng.reload_sources()
    assert sorted(p.name for p in (data_dir / "sources").glob("*.yaml")) == ["hello.yaml"]
    (data_dir / "sources" / "hello.yaml").unlink()
    eng.reload_sources()
    assert "hello" not in eng.sources


def test_plugin_cannot_replace_a_builtin_type(make_engine, data_dir):
    plugins = data_dir / "plugins"
    plugins.mkdir()
    (plugins / "evil.py").write_text(PLUGIN.replace('"hello"', '"text_file"'))
    eng = make_engine(seed=False)
    assert "clashes" in eng.plugin_errors["evil.py"]
    from kindle_dash_server.sources import SOURCE_TYPES

    assert SOURCE_TYPES["text_file"].__module__ == "kindle_dash_server.sources.text_file"


def test_example_plugin_from_the_repo(make_engine, data_dir):
    plugins = data_dir / "plugins"
    plugins.mkdir()
    example = Path(__file__).parent.parent / "examples" / "plugins" / "countdown.py"
    (plugins / "countdown.py").write_text(example.read_text())
    eng = make_engine(seed=False)
    card = eng.get_card("countdown", at=at(2026, 12, 30, 9, 0))
    assert card.body == "2 days"


def test_events_are_capped(make_engine):
    eng = make_engine(seed=False)
    for i in range(400):
        eng.state.event("info", f"e{i}")
    events = eng.state.events(1000)
    assert len(events) <= 301 and events[0]["message"] == "e399"
    assert time.time() - events[0]["ts"] < 60


def test_state_close_is_safe_with_concurrent_writers(make_engine):
    """Regression: closing the db while a render thread wrote to it segfaulted."""
    import threading

    eng = make_engine(seed=False)
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            eng.state.source_shown("x")
            eng.state.event("info", "tick")

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    eng.state.close()  # must not crash; later calls are no-ops
    time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join()
    assert eng.state.get("anything", "default") == "default"
    assert eng.state.events() == []


def test_wait_idle(make_engine):
    eng = make_engine(seed=False)
    assert eng.wait_idle(0.1) is True
    eng._render_lock.acquire()
    try:
        assert eng.wait_idle(0.1) is False
    finally:
        eng._render_lock.release()


def test_concurrent_renders_of_the_same_source(make_engine, data_dir):
    """Regression: a scheduled render and a preview of the same source shared a temp file."""
    from concurrent.futures import ThreadPoolExecutor

    setup_sources(data_dir, quotes=QUOTES.format(name="Q", w=1))
    eng = make_engine(seed=False)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: eng.render_source("quotes", force=True), range(16)))
    assert all(png_info(r)["width"] == 1072 for r in results)
    assert not list((data_dir / "cache").rglob("*.tmp"))
