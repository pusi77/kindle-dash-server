"""``json_api``: any JSON HTTP API, described in YAML.

Fields are JMESPath expressions evaluated on each item, or Jinja2 templates when they contain
``{{``. Templates see the item's keys directly, plus ``item``, ``fields`` (fields resolved so
far), ``today`` and ``now``. URL, params, headers and ``detail_url`` are templates too.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import jmespath
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..card import Card
from ..config import Duration
from ..net import FetchError
from .base import FilteredOut, Source, SourceConfig, SourceError

log = logging.getLogger(__name__)

_env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
CARD_FIELDS = ("image", "title", "body", "author", "credit", "url", "license")


def is_template(s: str) -> bool:
    return "{{" in s or "{%" in s


def render_template(template: str, context: dict[str, Any]) -> str:
    return _env.from_string(template).render(**context).strip()


def render_value(value: Any, context: dict[str, Any]) -> Any:
    """Render templates inside strings, dicts and lists."""
    if isinstance(value, str):
        return render_template(value, context) if is_template(value) else value
    if isinstance(value, dict):
        return {k: render_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, context) for v in value]
    return value


def flatten_params(params: dict[str, Any]) -> dict[str, str]:
    """Nested dicts/lists -> PHP/Rails style ``a[b][0]=c`` query parameters."""
    out: dict[str, str] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}[{k}]", v)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(f"{prefix}[{i}]", v)
        elif isinstance(value, bool):
            out[prefix] = "true" if value else "false"
        elif value is not None:
            out[prefix] = str(value)

    for k, v in params.items():
        walk(k, v)
    return out


class PagesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    param: str = "page"
    min: int = 1
    max: int = Field(1, ge=1)
    # JMESPath to the number of pages in the response; a random page past it is re-picked.
    total_path: str | None = None


class JsonApiConfig(SourceConfig):
    type: Literal["json_api"] = "json_api"
    url: str
    method: Literal["GET", "POST"] = "GET"
    params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    # JMESPath to the list of items. Unset: the response is the item (or the list).
    items: str | None = None
    pick: Literal["random", "daily", "first"] = "random"
    pages: PagesConfig | None = None
    # Optional second request per item (e.g. the Met: search returns IDs, details are separate).
    # The detail response becomes the item; the original is available as ``_item``.
    detail_url: str | None = None
    # image/title/body/author/credit/url/license/width/height/kicker, or anything (-> meta).
    # ``image`` can be a list: tried in order until one downloads.
    fields: dict[str, str | list[str]] = Field(default_factory=dict)
    # Constant values for fields the API doesn't provide (e.g. license: CC0).
    constants: dict[str, str] = Field(default_factory=dict)
    # Fields an item must have to be used. Default: ["image"] for the art layout.
    required: list[str] | None = None
    image_headers: dict[str, str] = Field(default_factory=dict)
    # How long API responses are cached on disk.
    response_ttl: Duration = 6 * 3600

    @field_validator("fields")
    @classmethod
    def _check_fields(cls, v: dict[str, str | list[str]]) -> dict[str, str | list[str]]:
        for name, expr in v.items():
            for e in [expr] if isinstance(expr, str) else expr:
                try:
                    if is_template(e):
                        _env.parse(e)
                    else:
                        jmespath.compile(e)
                except Exception as exc:
                    raise ValueError(f"field {name!r}: {exc}") from exc
        return v


class JsonApiSource(Source):
    type_name = "json_api"
    Config = JsonApiConfig
    config: JsonApiConfig

    def cache_key(self) -> str:
        return self.ctx.today.isoformat() if self.config.pick == "daily" else ""

    def _base_context(self) -> dict[str, Any]:
        return {
            "today": self.ctx.today,
            "now": self.ctx.now,
            "location": self.ctx.settings.location,
        }

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        c = self.config
        ctx = self._base_context()
        return self.ctx.http.get_json(
            render_value(url, ctx),
            params=flatten_params(render_value(params, ctx)) or None,
            headers=render_value(c.headers, ctx) or None,
            method=c.method,
            json_body=render_value(c.body, ctx),
            ttl=c.response_ttl,
        )

    def fetch_items(self, rng: Any) -> list[Any]:
        c = self.config
        params = dict(c.params)
        if c.pages:
            p = c.pages
            page = rng.randint(p.min, max(p.min, p.max))
            data = self._get(c.url, {**params, p.param: page})
            if p.total_path:
                total = jmespath.search(p.total_path, data)
                if isinstance(total, int | float) and p.min <= total < page:
                    page = rng.randint(p.min, int(total))
                    data = self._get(c.url, {**params, p.param: page})
        else:
            data = self._get(c.url, params)
        items = jmespath.search(c.items, data) if c.items else data
        if items is None:
            raise SourceError(f"items path {c.items!r} matched nothing")
        if not isinstance(items, list):
            items = [items]
        return items

    def fetch(self) -> Card:
        c = self.config
        rng = self.rng(c.pick)
        items = self.fetch_items(rng)
        if not items:
            raise SourceError("the API returned no items")
        order = list(items)
        if c.pick != "first":
            rng.shuffle(order)
        required = c.required
        if required is None:
            required = ["image"] if c.layout in ("art", "auto") and "image" in c.fields else []

        reasons: list[str] = []
        for item in order[: c.filters.max_attempts]:
            try:
                return self._card_from_item(item, required)
            except (SourceError, FetchError) as exc:
                reasons.append(str(exc))
                log.debug("%s: skipping item: %s", self.id, exc)
        summary = "; ".join(dict.fromkeys(reasons))[:500]
        raise SourceError(
            f"no usable item in {min(len(order), c.filters.max_attempts)} tried: {summary}"
        )

    def resolve_fields(self, item: Any) -> dict[str, Any]:
        """Evaluate the field mapping on one item (JMESPath first, then templates)."""
        c = self.config
        resolved: dict[str, Any] = {}
        templates: dict[str, str | list[str]] = {}
        for name, expr in c.fields.items():
            if isinstance(expr, list) or is_template(expr):
                templates[name] = expr
            else:
                resolved[name] = jmespath.search(expr, item)
        context = {**(item if isinstance(item, dict) else {}), "item": item, **self._base_context()}
        for name, expr in templates.items():
            context["fields"] = resolved
            exprs = [expr] if isinstance(expr, str) else expr
            values = [
                render_template(e, context) if is_template(e) else jmespath.search(e, item)
                for e in exprs
            ]
            values = [v for v in values if v not in (None, "")]
            resolved[name] = values if isinstance(expr, list) else (values[0] if values else None)
        for name, value in c.constants.items():
            if resolved.get(name) in (None, "", []):
                resolved[name] = value
        return resolved

    def _card_from_item(self, item: Any, required: list[str]) -> Card:
        c = self.config
        if c.detail_url:
            ctx = {**(item if isinstance(item, dict) else {}), "item": item, **self._base_context()}
            detail = self.ctx.http.get_json(
                render_template(c.detail_url, ctx), headers=c.headers or None, ttl=c.response_ttl
            )
            if isinstance(detail, dict):
                detail = {**detail, "_item": item}
            item = detail

        f = self.resolve_fields(item)
        missing = [r for r in required if f.get(r) in (None, "", [])]
        if missing:
            raise SourceError(f"item has no {', '.join(missing)}")
        reason = c.filters.check_size(_int(f.get("width")), _int(f.get("height")))
        if reason:
            raise FilteredOut(reason)

        image = None
        urls = f.get("image")
        if urls:
            urls = urls if isinstance(urls, list) else [urls]
            errors = []
            for u in urls:
                try:
                    image = self.load_image(
                        self.ctx.http.get_bytes(str(u), headers=c.image_headers or None)
                    )
                    self.check_image(image)
                    break
                except FilteredOut:
                    raise
                except (SourceError, FetchError) as exc:
                    errors.append(str(exc))
                    image = None
            if image is None:
                raise SourceError(f"image download failed: {'; '.join(errors)}")

        meta = {k: v for k, v in f.items() if k not in CARD_FIELDS and v is not None}
        return Card(
            title=_text(f.get("title")),
            body=_text(f.get("body")),
            author=_text(f.get("author")),
            credit=_text(f.get("credit")),
            image=image,
            url=_text(f.get("url")),
            license=_text(f.get("license")),
            source_name=c.name,
            meta=meta,
        )


def _text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v if x)
    s = str(v).strip()
    return s or None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
