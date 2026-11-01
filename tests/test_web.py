"""HTTP: the device endpoint contract, previews, health, auth and the UI forms."""

from __future__ import annotations

import base64
import time

import pytest
from conftest import add_source, png_info, write_settings
from fastapi.testclient import TestClient

from kindle_dash_server.config import Env
from kindle_dash_server.web.app import create_app

QUOTES = "type: text_file\nname: Quotes\npath: q.txt\nlayout: quote\n"


@pytest.fixture
def setup(data_dir):
    (data_dir / "q.txt").write_text("A quote.\n— Someone\n%\nAnother.\n— Else\n")
    add_source(data_dir, "quotes", QUOTES)
    # Only the local source, so startup renders don't depend on anything else.
    (data_dir / "sources" / ".seeded").write_text(
        "\n".join(
            p.name
            for p in __import__("kindle_dash_server.config", fromlist=["x"])
            .DEFAULTS_DIR.joinpath("sources")
            .glob("*.yaml")
        )
    )
    return data_dir


def client(data_dir, basic_auth=None, **settings):
    if settings:
        write_settings(data_dir, **settings)
    return TestClient(create_app(Env(data_dir=data_dir, basic_auth=basic_auth)))


def wait_for_render(c, timeout=10.0):
    """The scheduler renders for the next fetch at startup, in the background."""
    end = time.time() + timeout
    while time.time() < end:
        if c.get("/healthz").json()["source_id"]:
            return
        time.sleep(0.05)
    raise AssertionError("no render")


def test_device_endpoint_contract(setup):
    with client(setup) as c:
        wait_for_render(c)
        r = c.get("/dash.png", headers={"X-Kindle-Battery": "77"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.headers["cache-control"] == "no-store"
        assert png_info(r.content) == {
            "width": 1072,
            "height": 1448,
            "bit_depth": 8,
            "color_type": 0,
        }
        assert c.head("/dash.png").status_code == 200
        assert c.get("/other.png").status_code == 404
        assert ">77%<" in c.get("/status").text


def test_device_schedule_headers_are_picked_up(setup):
    with client(setup) as c:
        c.get(
            "/dash.png",
            headers={
                "X-Kindle-Schedule": "*/20 6-20 * * *",
                "X-Kindle-Timezone": "Europe/Rome",
                "X-Kindle-Sleep-After": "1260",
            },
        )
        status = c.get("/status").text
        assert "*/20 6-20 * * *" in status and "from the Kindle" in status
        assert "reported by the Kindle" in c.get("/settings").text


def test_device_token(setup):
    with client(setup, device={"path": "/k/img.png", "token": "s3cret"}) as c:
        assert c.get("/dash.png").status_code == 404  # path is configurable
        assert c.get("/k/img.png").status_code == 401
        assert c.get("/k/img.png", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/k/img.png", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_basic_auth_protects_the_ui_but_not_the_device(setup):
    with client(setup, basic_auth="admin:pw") as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("admin", "nope")).status_code == 401
        assert c.get("/", auth=("admin", "pw")).status_code == 200
        assert c.get("/healthz").status_code == 200
        assert c.get("/dash.png").status_code == 200
        bad = base64.b64encode(b"\xff\xfe").decode()
        assert c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_pages_render(setup):
    with client(setup) as c:
        for path in (
            "/",
            "/sources",
            "/display",
            "/settings",
            "/sources/new",
            "/sources/quotes/edit",
            "/sources/new?src=quotes",
            "/display/slot-row",
        ):
            r = c.get(path)
            assert r.status_code == 200, path
        assert c.get("/sources/nope/edit").status_code == 404


def test_preview_and_render_now(setup):
    with client(setup) as c:
        r = c.get("/preview/quotes.png")
        assert r.status_code == 200 and png_info(r.content)["color_type"] == 0
        assert c.get("/preview/nope.png").status_code == 404
        assert c.get("/thumb/quotes.png").status_code == 200
        r = c.post("/render-now")
        assert r.json()["ok"] is True and r.json()["source_id"] == "quotes"
        r = c.post("/render-now", headers={"HX-Request": "true"})
        assert "Rendered in" in r.text


def test_preview_of_a_failing_source_is_an_image_with_the_error(setup):
    add_source(
        setup, "down", "type: json_api\nname: Down\nurl: https://down.example/\nfields: {}\n"
    )
    with client(setup) as c:
        r = c.get("/preview/down.png")
        assert r.status_code == 502 and r.headers["content-type"] == "image/png"


def test_editor_validate_preview_save_delete(setup):
    with client(setup) as c:
        r = c.post("/sources/validate", data={"yaml_text": "type: text_file\nname: X"})
        assert "path: Field required" in r.text
        good = "type: text_file\nname: Second\npath: q.txt\nlayout: quote\n"
        assert "Valid text_file" in c.post("/sources/validate", data={"yaml_text": good}).text
        r = c.post("/sources/preview", data={"yaml_text": good, "source_id": "second"})
        assert 'src="data:image/png;base64,' in r.text

        r = c.post("/sources/save", data={"yaml_text": good, "source_id": "Bad Id"})
        assert r.status_code == 422
        r = c.post("/sources/save", data={"yaml_text": good, "source_id": "quotes"})
        assert r.status_code == 422 and "already exists" in r.text
        r = c.post(
            "/sources/save", data={"yaml_text": good, "source_id": "second"}, follow_redirects=False
        )
        assert r.status_code == 303
        assert (setup / "sources" / "second.yaml").read_text() == good
        assert "Second" in c.get("/sources").text

        # Rename via the editor.
        c.post(
            "/sources/save",
            data={"yaml_text": good, "source_id": "renamed", "original_id": "second"},
        )
        assert not (setup / "sources" / "second.yaml").exists()
        assert (setup / "sources" / "renamed.yaml").exists()

        assert c.post("/sources/renamed/delete", follow_redirects=False).status_code == 303
        assert not (setup / "sources" / "renamed.yaml").exists()


def test_toggle_and_weight_keep_comments(setup):
    path = setup / "sources" / "quotes.yaml"
    path.write_text("# my comment\n" + QUOTES)
    with client(setup) as c:
        r = c.post("/sources/quotes/toggle")
        assert "Enable" in r.text
        c.post("/sources/quotes/weight", data={"weight": "2.5"})
        text = path.read_text()
        assert text.startswith("# my comment") and "enabled: false" in text
        assert "weight: 2.5" in text
        assert c.post("/sources/quotes/weight", data={"weight": "-1"}).status_code == 422


def test_display_form(setup):
    with client(setup) as c:
        r = c.post(
            "/display",
            data={
                "mode": "schedule",
                "strategy": "round_robin",
                "rotation_sources": ["quotes"],
                "slot_days": ["mon-fri", "*"],
                "slot_start": ["07:00", ""],
                "slot_end": ["09:00", ""],
                "slot_source": ["quotes", ""],
            },
        )
        assert "Saved" in r.text
        settings = (setup / "settings.yaml").read_text()
        assert "mode: schedule" in settings and "MON-FRI" in settings
        r = c.post(
            "/display",
            data={
                "mode": "schedule",
                "slot_days": ["*"],
                "slot_start": ["25:00"],
                "slot_end": ["26:00"],
                "slot_source": [""],
            },
        )
        assert r.status_code == 422 and "HH:MM" in r.text


def test_pin_and_back_to_rotation(setup):
    with client(setup) as c:
        c.post("/sources/quotes/pin")
        assert "pinned: <strong>Quotes</strong>" in c.get("/status").text
        c.post("/selection/rotation")
        assert "rotation (" in c.get("/status").text


def test_settings_form(setup):
    with client(setup) as c:
        page = c.get("/settings").text
        assert 'name="output.width" value="1072"' in page
        r = c.post(
            "/settings",
            data={
                "output.rotation": "90",
                "output.autocontrast": ["false"],
                "battery.show_below": "30",
                "device.token": "",
                "scheduler.timezone": "",
            },
        )
        assert "Saved" in r.text
        saved = (setup / "settings.yaml").read_text()
        assert "rotation: 90" in saved and "autocontrast: false" in saved
        assert "show_below: 30" in saved
        # The device image is now rendered landscape and rotated back to portrait.
        assert png_info(c.get("/dash.png").content)["width"] == 1072

        r = c.post("/settings", data={"location.timezone": "Mars/Base"})
        assert r.status_code == 422 and "unknown timezone" in r.text
        r = c.post(
            "/settings", data={"scheduler.schedule": "*/30 * * * *", "scheduler.sleep_after": "960"}
        )
        assert r.status_code == 422 and "never fetches" in r.text
        r = c.post("/settings", data={"device.path": "/sources"})
        assert r.status_code == 422


def test_healthz(setup):
    with client(setup) as c:
        wait_for_render(c)
        h = c.get("/healthz").json()
        assert h["status"] == "ok" and h["has_image"] is True
        assert h["rendered_for"]  # rendered for a specific upcoming fetch
