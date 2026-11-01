"""Shared fixtures. No test touches the real network: every HttpClient gets a mock transport."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from PIL import Image

from kindle_dash_server import engine as engine_module
from kindle_dash_server.config import Paths, Settings, SettingsStore
from kindle_dash_server.engine import Engine
from kindle_dash_server.net import HttpClient
from kindle_dash_server.state import State

Handler = Callable[[httpx.Request], httpx.Response]


class MockWeb:
    """Routes requests to handlers by URL prefix; records every request."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, Handler]] = []
        self.requests: list[httpx.Request] = []

    def route(self, prefix: str, handler: Handler | dict | list | bytes | int) -> None:
        if not callable(handler):
            value = handler

            def handler(_req: httpx.Request, value=value) -> httpx.Response:
                if isinstance(value, int):
                    return httpx.Response(value)
                if isinstance(value, bytes):
                    return httpx.Response(200, content=value)
                return httpx.Response(200, json=value)

        self.routes.insert(0, (prefix, handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for prefix, handler in self.routes:
            if url.startswith(prefix):
                return handler(request)
        return httpx.Response(404, text=f"no mock for {url}")

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


@pytest.fixture
def web() -> MockWeb:
    return MockWeb()


@pytest.fixture
def http(tmp_path: Path, web: MockWeb) -> HttpClient:
    return HttpClient(
        tmp_path / "http", transport=httpx.MockTransport(web), retries=0, min_host_interval=0
    )


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch, web: MockWeb) -> None:
    """Engines created without an explicit client (e.g. by the web app) use the mock too."""

    def factory(cache_dir: Path, contact: str = "", **_: object) -> HttpClient:
        return HttpClient(
            cache_dir,
            contact=contact,
            transport=httpx.MockTransport(web),
            retries=0,
            min_host_interval=0,
        )

    monkeypatch.setattr(engine_module, "HttpClient", factory)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def write_settings(data_dir: Path, **sections: object) -> None:
    import yaml

    settings = Settings.model_validate(sections)
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(settings.model_dump(mode="json")))


@pytest.fixture
def make_engine(data_dir: Path, http: HttpClient) -> Callable[..., Engine]:
    """Engine on an isolated data dir. ``seed=False`` skips the bundled default sources."""
    created: list[State] = []

    def make(seed: bool = True, **settings: object) -> Engine:
        if settings:
            write_settings(data_dir, **settings)
        paths = Paths(data_dir)
        state = State(paths.state_db)
        created.append(state)
        eng = Engine(paths, SettingsStore(paths.settings), state, http=http)
        if seed:
            eng.bootstrap()
        else:
            paths.ensure()
            eng.reload_sources()
        return eng

    yield make
    for s in created:
        s.close()


def add_source(data_dir: Path, sid: str, text: str) -> Path:
    path = data_dir / "sources" / f"{sid}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def jpeg(size: tuple[int, int] = (400, 600), color: int = 128) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (color, color, color)).save(buf, "JPEG")
    return buf.getvalue()


def png_info(data: bytes) -> dict[str, int]:
    """Width, height, bit depth and colour type from a PNG's IHDR chunk."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return {
        "width": int.from_bytes(data[16:20], "big"),
        "height": int.from_bytes(data[20:24], "big"),
        "bit_depth": data[24],
        "color_type": data[25],
    }


def dump(obj: object) -> str:
    return json.dumps(obj)
