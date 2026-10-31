"""FastAPI app: device endpoint, previews, health, and the HTMX web UI."""

from __future__ import annotations

import base64
import binascii
import hmac
import io
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..config import Env, Paths, SettingsStore
from ..engine import Engine
from ..scheduler import Scheduler
from ..state import State
from .yamledit import set_top_level_key

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=HERE / "templates")
NO_STORE = {"Cache-Control": "no-store"}


def _fmt_time(ts: float | None, tz: str) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S")


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 5:
        return "just now"
    for unit, secs in (("d", 86400), ("h", 3600), ("min", 60)):
        if d >= secs:
            return f"{int(d // secs)} {unit} ago"
    return f"{int(d)} s ago"


def create_app(env: Env | None = None) -> FastAPI:
    env = env or Env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        paths = Paths(env.data_dir)
        paths.ensure()
        state = State(paths.state_db)
        engine = Engine(paths, SettingsStore(paths.settings), state)
        await run_in_threadpool(engine.bootstrap)
        scheduler = Scheduler(engine)
        app.state.engine, app.state.scheduler = engine, scheduler
        scheduler.start()
        log.info(
            "kindle-dash-server %s, data in %s, device endpoint %s",
            __version__,
            paths.root,
            engine.settings.device.path,
        )
        yield
        await scheduler.stop()
        engine.http.close()
        state.close()

    app = FastAPI(
        title="kindle-dash-server",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def engine_of(request: Request) -> Engine:
        return request.app.state.engine

    templates.env.globals["version"] = __version__
    templates.env.filters["ago"] = _ago

    # -- basic auth (optional) ---------------------------------------------------------------

    auth_user, _, auth_pass = (env.basic_auth or "").partition(":")

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if env.basic_auth:
            engine: Engine | None = getattr(request.app.state, "engine", None)
            open_paths = {"/healthz"}
            if engine:
                open_paths.add(engine.settings.device.path)
            if request.url.path not in open_paths and not _check_basic(
                request.headers.get("authorization", ""), auth_user, auth_pass
            ):
                return Response(
                    "authentication required",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="kindle-dash-server"'},
                )
        return await call_next(request)

    # -- API ---------------------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        engine = engine_of(request)
        cur = engine.state.get("current") or {}
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "has_image": engine.current_png is not None,
                "rendered_at": cur.get("rendered_at"),
                "source_id": cur.get("source_id"),
            },
            headers=NO_STORE,
        )

    @app.post("/render-now")
    async def render_now(request: Request):
        result = await request.app.state.scheduler.render_now("manual")
        if request.headers.get("hx-request"):
            return _status_partial(request, flash=result)
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/", status_code=303)
        return JSONResponse(
            {
                "ok": result.ok,
                "source_id": result.source_id,
                "message": result.message,
                "seconds": round(result.duration, 2),
            },
            status_code=200 if result.ok else 502,
        )

    @app.get("/preview/{sid}.png")
    async def preview(request: Request, sid: str, refresh: bool = False) -> Response:
        engine = engine_of(request)
        if sid not in engine.sources:
            raise HTTPException(404, f"unknown source {sid!r}")
        try:
            png = await run_in_threadpool(engine.render_source, sid, refresh)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            png = await run_in_threadpool(engine.placeholder, f"Preview of {sid} failed.\n\n{msg}")
            return Response(png, media_type="image/png", status_code=502, headers=NO_STORE)
        return Response(png, media_type="image/png", headers=NO_STORE)

    @app.get("/current.png")
    async def current(request: Request) -> Response:
        png = engine_of(request).current_png
        if png is None:
            raise HTTPException(503, "nothing rendered yet")
        return Response(png, media_type="image/png", headers=NO_STORE)

    @app.get("/thumb/{name}.png")
    async def thumb(request: Request, name: str) -> Response:
        engine = engine_of(request)
        path = (
            engine.paths.current_png
            if name == "_current"
            else engine.paths.previews / f"{name}.png"
        )
        if name != "_current" and name not in engine.sources:
            raise HTTPException(404)
        if not path.exists():
            if name == "_current":
                raise HTTPException(404)
            try:
                await run_in_threadpool(engine.render_source, name)
            except Exception:
                return Response(status_code=204)
        return Response(_thumbnail(path), media_type="image/png", headers=NO_STORE)

    # -- UI ----------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", _dashboard_ctx(request))

    @app.get("/status", response_class=HTMLResponse)
    async def status(request: Request):
        return _status_partial(request)

    @app.get("/sources", response_class=HTMLResponse)
    async def sources_page(request: Request):
        engine = engine_of(request)
        return templates.TemplateResponse(
            request,
            "sources.html",
            {
                "rows": _source_rows(engine),
                "load_errors": engine.load_errors,
                "settings": engine.settings,
            },
        )

    @app.post("/sources/{sid}/toggle", response_class=HTMLResponse)
    async def toggle(request: Request, sid: str):
        engine = engine_of(request)
        src = _require(engine, sid)
        _edit_source_file(engine, sid, "enabled", "false" if src.config.enabled else "true")
        return _row_partial(request, sid)

    @app.post("/sources/{sid}/weight", response_class=HTMLResponse)
    async def weight(request: Request, sid: str, weight: float = Form(...)):
        engine = engine_of(request)
        _require(engine, sid)
        if weight < 0 or weight > 1000:
            raise HTTPException(422, "weight must be between 0 and 1000")
        _edit_source_file(engine, sid, "weight", f"{weight:g}")
        return _row_partial(request, sid)

    @app.post("/sources/{sid}/pin")
    async def pin(request: Request, sid: str):
        engine = engine_of(request)
        _require(engine, sid)
        sel = engine.settings.selection.model_copy(update={"mode": "pinned", "pinned": sid})
        engine.store.update(selection=sel.model_dump())
        await request.app.state.scheduler.render_now("pinned")
        return _redirect_or_htmx(request, "/")

    @app.post("/selection/rotation")
    async def rotation(request: Request):
        engine = engine_of(request)
        sel = engine.settings.selection.model_copy(update={"mode": "rotation"})
        engine.store.update(selection=sel.model_dump())
        request.app.state.scheduler.reschedule()
        return _redirect_or_htmx(request, "/")

    # -- device endpoint (last: catch-all, path configurable) ----------------------------------

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def device(request: Request, path: str) -> Response:
        engine = engine_of(request)
        dev = engine.settings.device
        if "/" + path != dev.path:
            raise HTTPException(404)
        if dev.token:
            auth = request.headers.get("authorization", "")
            supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(supplied.encode(), dev.token.encode()):
                engine.state.event(
                    "warning",
                    f"device request with a bad token from "
                    f"{request.client.host if request.client else '?'}",
                )
                return Response("unauthorized", status_code=401)
        png = engine.current_png
        if png is None:
            return Response("nothing rendered yet", status_code=503)
        if request.method == "GET":
            engine.state.set(
                "device",
                {
                    "last_seen": time.time(),
                    "battery": request.headers.get("x-kindle-battery"),
                    "ip": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent"),
                },
            )
        cur = engine.state.get("current") or {}
        headers = {**NO_STORE, "X-Rendered-At": str(int(cur.get("rendered_at") or 0))}
        return Response(png, media_type="image/png", headers=headers)

    return app


# -- helpers ----------------------------------------------------------------------------------


def _check_basic(header: str, user: str, password: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode()
    except (binascii.Error, UnicodeDecodeError):
        return False
    u, _, p = raw.partition(":")
    return hmac.compare_digest(u.encode(), user.encode()) & hmac.compare_digest(
        p.encode(), password.encode()
    )


_thumb_cache: dict[Path, tuple[float, bytes]] = {}


def _thumbnail(path: Path, width: int = 268) -> bytes:
    mtime = path.stat().st_mtime
    hit = _thumb_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    with Image.open(path) as im:
        im.thumbnail((width, width * 2), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
    _thumb_cache[path] = (mtime, buf.getvalue())
    return buf.getvalue()


def _require(engine: Engine, sid: str):
    if sid not in engine.sources:
        raise HTTPException(404, f"unknown source {sid!r}")
    return engine.sources[sid]


def _edit_source_file(engine: Engine, sid: str, key: str, value: str) -> None:
    path = engine.source_files[sid]
    path.write_text(
        set_top_level_key(path.read_text(encoding="utf-8"), key, value), encoding="utf-8"
    )
    engine.reload_sources()


def _redirect_or_htmx(request: Request, url: str) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _source_rows(engine: Engine) -> list[dict[str, Any]]:
    status = engine.state.source_status()
    sel = engine.settings.selection
    tz = engine.settings.location.timezone
    rows = []
    for sid, src in sorted(engine.sources.items(), key=lambda kv: kv[1].config.name.lower()):
        st = status.get(sid, {})
        rows.append(
            {
                "id": sid,
                "config": src.config,
                "pinned": sel.mode == "pinned" and sel.pinned == sid,
                "last_ok": _fmt_time(st.get("last_ok_at"), tz),
                "last_error": st.get("last_error"),
                "last_error_at": st.get("last_error_at"),
                "shown_count": st.get("shown_count", 0),
                "has_preview": (engine.paths.previews / f"{sid}.png").exists(),
            }
        )
    return rows


def _row_partial(request: Request, sid: str) -> HTMLResponse:
    engine: Engine = request.app.state.engine
    row = next(r for r in _source_rows(engine) if r["id"] == sid)
    return templates.TemplateResponse(request, "_source_row.html", {"r": row})


def _dashboard_ctx(request: Request, flash: Any = None) -> dict[str, Any]:
    engine: Engine = request.app.state.engine
    s = engine.settings
    cur = engine.state.get("current") or {}
    dev = engine.state.get("device") or {}
    sid = cur.get("source_id")
    scheduler: Scheduler = request.app.state.scheduler
    return {
        "settings": s,
        "current": cur,
        "current_name": engine.sources[sid].config.name if sid in engine.sources else sid,
        "rendered_at": _fmt_time(cur.get("rendered_at"), s.location.timezone),
        "device": dev,
        "device_seen": _fmt_time(dev.get("last_seen"), s.location.timezone),
        "next_run": scheduler.next_run.strftime("%H:%M:%S") if scheduler.next_run else "—",
        "pinned_name": (
            engine.sources[s.selection.pinned].config.name
            if s.selection.pinned in engine.sources
            else s.selection.pinned
        ),
        "enabled_count": len(engine.enabled_ids()),
        "events": engine.state.events(15),
        "tz": s.location.timezone,
        "fmt_time": _fmt_time,
        "flash": flash,
        "stamp": int(cur.get("rendered_at") or 0),
    }


def _status_partial(request: Request, flash: Any = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "_status.html", _dashboard_ctx(request, flash))
