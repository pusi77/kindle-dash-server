"""FastAPI app: device endpoint, previews, health, and the HTMX web UI."""

from __future__ import annotations

import base64
import binascii
import hmac
import io
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..config import Env, Paths, Settings, SettingsStore
from ..engine import Engine
from ..render.fonts import available_families
from ..scheduler import Scheduler
from ..sources.base import SOURCE_TYPES, SourceConfig
from ..sources.loader import ID_RE, SourceDefinitionError, format_validation_error, parse_source
from ..state import State
from .yamledit import set_top_level_key

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=HERE / "templates")
NO_STORE = {"Cache-Control": "no-store"}

NEW_SOURCE_TEMPLATE = """\
# A new source. Pick a type and fill in its options; "Validate" and "Preview" don't save.
type: text_file
name: My quotes
enabled: true
path: texts/my-quotes.txt     # relative to the data volume
pick: random
layout: quote
refresh: 1h
weight: 1
"""


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
    templates.env.globals["version"] = __version__
    templates.env.filters["ago"] = _ago

    def eng(request: Request) -> Engine:
        return request.app.state.engine

    def sched(request: Request) -> Scheduler:
        return request.app.state.scheduler

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
        engine = eng(request)
        cur = engine.state.get("current") or {}
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "has_image": engine.current_png is not None,
                "rendered_at": cur.get("rendered_at"),
                "rendered_for": cur.get("target"),
                "source_id": cur.get("source_id"),
            },
            headers=NO_STORE,
        )

    @app.post("/render-now")
    async def render_now(request: Request):
        result = await sched(request).render_now("manual")
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
        engine = eng(request)
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
        png = eng(request).current_png
        if png is None:
            raise HTTPException(503, "nothing rendered yet")
        return Response(png, media_type="image/png", headers=NO_STORE)

    @app.get("/thumb/{name}.png")
    async def thumb(request: Request, name: str) -> Response:
        engine = eng(request)
        if name != "_current" and name not in engine.sources:
            raise HTTPException(404)
        path = (
            engine.paths.current_png
            if name == "_current"
            else engine.paths.previews / f"{name}.png"
        )
        if not path.exists():
            if name == "_current":
                raise HTTPException(404)
            try:
                await run_in_threadpool(engine.render_source, name)
            except Exception:
                return Response(status_code=204)
        return Response(_thumbnail(path), media_type="image/png", headers=NO_STORE)

    # -- dashboard ---------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", _dashboard_ctx(request))

    @app.get("/status", response_class=HTMLResponse)
    async def status(request: Request):
        return _status_partial(request)

    # -- sources -----------------------------------------------------------------------------

    @app.get("/sources", response_class=HTMLResponse)
    async def sources_page(request: Request):
        engine = eng(request)
        return templates.TemplateResponse(
            request,
            "sources.html",
            {
                "rows": _source_rows(engine),
                "load_errors": engine.load_errors,
                "plugin_errors": engine.plugin_errors,
                "plugin_files": engine.plugin_files,
                "settings": engine.settings,
            },
        )

    @app.post("/sources/reload")
    async def reload_sources(request: Request):
        await run_in_threadpool(eng(request).reload_sources)
        return _redirect_or_htmx(request, "/sources")

    @app.post("/sources/{sid}/toggle", response_class=HTMLResponse)
    async def toggle(request: Request, sid: str):
        engine = eng(request)
        src = _require(engine, sid)
        _edit_source_file(engine, sid, "enabled", "false" if src.config.enabled else "true")
        return _row_partial(request, sid)

    @app.post("/sources/{sid}/weight", response_class=HTMLResponse)
    async def weight(request: Request, sid: str, weight: float = Form(...)):
        engine = eng(request)
        _require(engine, sid)
        if weight < 0 or weight > 1000:
            raise HTTPException(422, "weight must be between 0 and 1000")
        _edit_source_file(engine, sid, "weight", f"{weight:g}")
        return _row_partial(request, sid)

    @app.post("/sources/{sid}/pin")
    async def pin(request: Request, sid: str):
        engine = eng(request)
        _require(engine, sid)
        sel = engine.settings.selection.model_copy(update={"mode": "pinned", "pinned": sid})
        engine.save_settings(engine.settings.model_copy(update={"selection": sel}))
        await sched(request).render_now("pinned")
        return _redirect_or_htmx(request, "/")

    @app.get("/sources/new", response_class=HTMLResponse)
    async def new_source(request: Request, src: str | None = None):
        engine = eng(request)
        text, new_id = NEW_SOURCE_TEMPLATE, ""
        if src:
            if src not in engine.source_files:
                raise HTTPException(404, f"unknown source {src!r}")
            text = engine.source_files[src].read_text(encoding="utf-8")
            new_id = _free_id(engine, f"{src}-copy")
        return _editor(request, sid=new_id, original=None, text=text)

    @app.get("/sources/{sid}/edit", response_class=HTMLResponse)
    async def edit_source(request: Request, sid: str):
        engine = eng(request)
        if sid not in engine.source_files:
            raise HTTPException(404, f"unknown source {sid!r}")
        return _editor(
            request,
            sid=sid,
            original=sid,
            text=engine.source_files[sid].read_text(encoding="utf-8"),
        )

    @app.post("/sources/validate", response_class=HTMLResponse)
    async def validate_source(request: Request, yaml_text: str = Form("")):
        try:
            cfg = parse_source(yaml_text)
        except SourceDefinitionError as exc:
            return _msg(request, False, str(exc))
        return _msg(request, True, f"Valid {cfg.type} source “{cfg.name}”.")

    @app.post("/sources/preview", response_class=HTMLResponse)
    async def preview_source(
        request: Request, yaml_text: str = Form(""), source_id: str = Form("")
    ):
        engine = eng(request)
        try:
            cfg = parse_source(yaml_text)
        except SourceDefinitionError as exc:
            return _msg(request, False, str(exc))
        try:
            png = await run_in_threadpool(engine.render_config, source_id or "preview", cfg)
        except Exception as exc:
            return _msg(request, False, f"Preview failed: {type(exc).__name__}: {exc}")
        b64 = base64.b64encode(png).decode()
        return HTMLResponse(
            f'<img class="live-preview" alt="preview" src="data:image/png;base64,{b64}">'
        )

    @app.post("/sources/save", response_class=HTMLResponse)
    async def save_source(
        request: Request,
        yaml_text: str = Form(""),
        source_id: str = Form(""),
        original_id: str = Form(""),
    ):
        engine = eng(request)
        sid = source_id.strip().lower()
        original = original_id or None
        error = None
        if not ID_RE.match(sid):
            error = (
                "ID: lowercase letters, digits, '-' and '_' (max 64), starting with a "
                "letter or digit"
            )
        elif sid != original and (engine.paths.sources / f"{sid}.yaml").exists():
            error = f"a source with ID {sid!r} already exists"
        else:
            try:
                parse_source(yaml_text)
            except SourceDefinitionError as exc:
                error = str(exc)
        if error:
            return _editor(
                request, sid=sid, original=original, text=yaml_text, error=error, status_code=422
            )
        path = engine.paths.sources / f"{sid}.yaml"
        path.write_text(
            yaml_text if yaml_text.endswith("\n") else yaml_text + "\n", encoding="utf-8"
        )
        if original and original != sid and original in engine.source_files:
            engine.source_files[original].unlink(missing_ok=True)
            engine.state.forget_source(original)
        await run_in_threadpool(engine.reload_sources)
        engine.state.event("info", "source saved", sid)
        return RedirectResponse(f"/sources#src-{sid}", status_code=303)

    @app.post("/sources/{sid}/delete")
    async def delete_source(request: Request, sid: str):
        engine = eng(request)
        if sid not in engine.source_files:
            raise HTTPException(404)
        engine.source_files[sid].unlink(missing_ok=True)
        engine.state.forget_source(sid)
        for p in (engine.paths.previews / f"{sid}.png", engine.paths.cards / f"{sid}.json"):
            p.unlink(missing_ok=True)
        await run_in_threadpool(engine.reload_sources)
        engine.state.event("info", "source deleted", sid)
        return _redirect_or_htmx(request, "/sources")

    # -- what's shown: pinned / rotation / schedule ----------------------------------------------

    @app.get("/display", response_class=HTMLResponse)
    async def display_page(request: Request):
        return _display(request)

    @app.get("/display/slot-row", response_class=HTMLResponse)
    async def slot_row(request: Request):
        return templates.TemplateResponse(
            request, "_slot_row.html", {"slot": None, "sources": _source_choices(eng(request))}
        )

    @app.post("/display", response_class=HTMLResponse)
    async def display_save(request: Request):
        engine = eng(request)
        form = await request.form()
        slots = []
        for days, start, end, source in zip(
            form.getlist("slot_days"),
            form.getlist("slot_start"),
            form.getlist("slot_end"),
            form.getlist("slot_source"),
            strict=False,
        ):
            if any(str(v).strip() for v in (start, end, source)):
                slots.append(
                    {
                        "days": days,
                        "start": start or "00:00",
                        "end": end or "24:00",
                        "source": source or None,
                    }
                )
        sel = {
            "mode": form.get("mode", "rotation"),
            "pinned": form.get("pinned") or None,
            "strategy": form.get("strategy", "weighted_random"),
            "rotation_sources": form.getlist("rotation_sources"),
            "slots": slots,
        }
        try:
            new = Settings.model_validate({**engine.settings.model_dump(), "selection": sel})
        except ValidationError as exc:
            return _display(request, error=format_validation_error(exc), draft=sel)
        engine.save_settings(new)
        sched(request).reschedule()
        if form.get("render") == "1":
            await sched(request).render_now("settings")
        return _display(request, saved=True)

    @app.post("/selection/rotation")
    async def rotation(request: Request):
        engine = eng(request)
        sel = engine.settings.selection.model_copy(update={"mode": "rotation"})
        engine.save_settings(engine.settings.model_copy(update={"selection": sel}))
        sched(request).reschedule()
        return _redirect_or_htmx(request, "/")

    # -- global settings ---------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return _settings(request)

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request):
        engine = eng(request)
        form = await request.form()
        data = engine.settings.model_dump()
        for key in form:
            if "." not in key:
                continue
            section, field = key.split(".", 1)
            if not isinstance(data.get(section), dict):
                continue
            value = form.getlist(key)[-1]  # checkbox: hidden "false" first, then "true"
            data[section][field] = value if str(value).strip() != "" else None
        for key in ("font", "contact"):
            if key in form:
                data[key] = form[key]
        try:
            new = Settings.model_validate(data)
        except ValidationError as exc:
            return _settings(request, error=format_validation_error(exc), status_code=422)
        old = engine.settings
        engine.save_settings(new)
        sched(request).reschedule()
        if new.output != old.output or new.font != old.font or new.battery != old.battery:
            await sched(request).render_now("settings")
        return _settings(request, saved=True)

    # -- device endpoint (last: catch-all, path configurable) ----------------------------------

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def device(request: Request, path: str) -> Response:
        engine = eng(request)
        dev = engine.settings.device
        if "/" + path != dev.path:
            raise HTTPException(404)
        if dev.token:
            auth = request.headers.get("authorization", "")
            supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(supplied.encode(), dev.token.encode()):
                engine.state.event(
                    "warning",
                    "device request with a bad token from "
                    f"{request.client.host if request.client else '?'}",
                )
                return Response("unauthorized", status_code=401)
        png = engine.current_png
        if png is None:
            return Response("nothing rendered yet", status_code=503)
        if request.method == "GET" and engine.record_device_request(
            dict(request.headers), request.client.host if request.client else None
        ):
            sched(request).reschedule()
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


def _free_id(engine: Engine, base: str) -> str:
    base = base[:60]
    sid, n = base, 2
    while (engine.paths.sources / f"{sid}.yaml").exists():
        sid, n = f"{base}-{n}", n + 1
    return sid


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


def _msg(request: Request, ok: bool, text: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "_msg.html", {"ok": ok, "text": text})


def _type_reference() -> list[dict[str, Any]]:
    """Each source type's own options (beyond the common ones), for the editor."""
    import sys

    common = set(SourceConfig.model_fields) - {"layout"}
    out = []
    for name, cls in sorted(SOURCE_TYPES.items()):
        fields = []
        for fname, f in cls.Config.model_fields.items():
            if fname in common or fname == "type":
                continue
            if f.is_required():
                default: Any = "required"
            else:
                default = f.default_factory() if f.default_factory else f.default
            ann = getattr(f.annotation, "__name__", None) or str(f.annotation)
            fields.append({"name": fname, "type": ann.replace("typing.", ""), "default": default})
        module = sys.modules.get(cls.__module__)
        doc = (cls.__doc__ or (module.__doc__ if module else "") or "").strip()
        out.append(
            {
                "name": name,
                "doc": doc.split("\n\n")[0],
                "fields": fields,
                "plugin": cls.__module__.startswith("kds_plugins."),
            }
        )
    return out


def _editor(
    request: Request,
    sid: str,
    original: str | None,
    text: str,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    engine: Engine = request.app.state.engine
    return templates.TemplateResponse(
        request,
        "editor.html",
        {
            "sid": sid,
            "original": original,
            "text": text,
            "error": error,
            "types": _type_reference(),
            "settings": engine.settings,
            "common": [
                (
                    n,
                    "required"
                    if f.is_required()
                    else f.default_factory()
                    if f.default_factory
                    else f.default,
                )
                for n, f in SourceConfig.model_fields.items()
                if n != "type"
            ],
            "load_error": engine.load_errors.get(original) if original else None,
        },
        status_code=status_code,
    )


def _source_choices(engine: Engine) -> list[tuple[str, str]]:
    return sorted(
        ((sid, s.config.name) for sid, s in engine.sources.items()), key=lambda x: x[1].lower()
    )


def _display(
    request: Request, error: str | None = None, saved: bool = False, draft: dict | None = None
) -> HTMLResponse:
    engine: Engine = request.app.state.engine
    sel = engine.settings.selection.model_dump()
    if draft:
        sel.update(draft)
    return templates.TemplateResponse(
        request,
        "display.html",
        {
            "settings": engine.settings,
            "sel": sel,
            "sources": _source_choices(engine),
            "enabled": set(engine.enabled_ids()),
            "error": error,
            "saved": saved,
            "active_slot": engine.active_slot(engine.next_fetch()),
        },
        status_code=422 if error else 200,
    )


def _settings(
    request: Request, error: str | None = None, saved: bool = False, status_code: int = 200
) -> HTMLResponse:
    engine: Engine = request.app.state.engine
    schedule, origin = engine.device_schedule()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": engine.settings,
            "s": engine.settings,
            "error": error,
            "saved": saved,
            "fonts": available_families(),
            "schedule": schedule,
            "origin": origin,
            "reported": engine.state.get("device_schedule"),
        },
        status_code=status_code,
    )


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
    schedule, origin = engine.device_schedule()
    target = cur.get("target")
    battery = dev.get("battery")
    next_fetch = scheduler.next_fetch or engine.next_fetch()
    return {
        "settings": s,
        "current": cur,
        "current_name": engine.sources[sid].config.name if sid in engine.sources else sid,
        "rendered_at": _fmt_time(cur.get("rendered_at"), s.location.timezone),
        "rendered_for": datetime.fromisoformat(target).strftime("%a %H:%M") if target else None,
        "device": dev,
        "device_seen": _fmt_time(dev.get("last_seen"), s.location.timezone),
        "battery_low": battery is not None
        and bool(s.battery.show_below)
        and battery <= s.battery.show_below,
        "schedule": schedule,
        "origin": origin,
        "next_fetch": next_fetch,
        "next_run": next_fetch - timedelta(seconds=s.scheduler.lead_seconds),
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
