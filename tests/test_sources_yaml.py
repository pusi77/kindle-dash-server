"""Declarative sources: YAML validation and the json_api / text_file / local_folder types."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import add_source, jpeg
from PIL import Image

from kindle_dash_server.config import DEFAULTS_DIR, Paths, Settings
from kindle_dash_server.sources import SOURCE_TYPES, SourceContext, SourceError
from kindle_dash_server.sources.json_api import flatten_params
from kindle_dash_server.sources.loader import (
    SourceDefinitionError,
    load_sources,
    parse_source,
    seed_defaults,
)
from kindle_dash_server.sources.text_file import parse_fortune

TZ = ZoneInfo("Europe/Rome")


def source(text: str, http, data_dir: Path, sid: str = "test", now: datetime | None = None):
    cfg = parse_source(text)
    ctx = SourceContext(
        http=http,
        paths=Paths(data_dir),
        settings=Settings(),
        now=now or datetime(2026, 11, 2, 7, 0, tzinfo=TZ),
    )
    return SOURCE_TYPES[cfg.type](sid, cfg, ctx)


# -- validation ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,message",
    [
        ("just a string", "mapping"),
        ("name: x", "missing 'type'"),
        ("type: nope\nname: x", "unknown type"),
        ("type: text_file\nname: x", "path: Field required"),
        ("type: text_file\nname: x\npath: a\ncolour: red", "colour: Extra inputs"),
        ("type: text_file\nname: x\npath: a\nrefresh: soon", "refresh"),
        ("type: json_api\nname: x\nurl: u\nfields: {title: 'a[['}", "fields"),
        ("type: json_api\nname: x\nurl: u\nwhere: '[[['", "where"),
        ("type: text_file\nname: '  '\npath: a", "name"),
        ("type: text_file\nname: x\npath: a\n  bad: indent", "invalid YAML"),
    ],
)
def test_invalid_definitions_are_explained(text, message):
    with pytest.raises(SourceDefinitionError, match=message):
        parse_source(text)


def test_durations():
    for value, seconds in [("90", 90), ("15m", 900), ("6h", 21600), ("1d", 86400), (30, 30)]:
        cfg = parse_source(f"type: text_file\nname: x\npath: a\nrefresh: {value}")
        assert cfg.refresh == seconds


@pytest.mark.parametrize(
    "path", sorted((DEFAULTS_DIR / "sources").glob("*.yaml")), ids=lambda p: p.stem
)
def test_bundled_defaults_are_valid(path):
    cfg = parse_source(path.read_text())
    assert cfg.name


def test_load_sources_reports_bad_files_and_ids(data_dir):
    add_source(data_dir, "good", "type: text_file\nname: Good\npath: x.txt")
    add_source(data_dir, "broken", "type: text_file\nname: Broken")
    add_source(data_dir, "Bad Name", "type: text_file\nname: x\npath: x.txt")
    res = load_sources(data_dir / "sources")
    assert set(res.configs) == {"good"}
    assert "path" in res.errors["broken"]
    assert "file name" in res.errors["Bad Name"]


def test_seed_defaults_once(data_dir):
    folder = data_dir / "sources"
    first = seed_defaults(folder)
    assert "aphorisms" in first
    (folder / "aphorisms.yaml").unlink()
    assert seed_defaults(folder) == []  # a deleted default doesn't come back
    assert not (folder / "aphorisms.yaml").exists()


# -- json_api --------------------------------------------------------------------------------


def test_flatten_params():
    assert flatten_params(
        {"q": {"bool": {"must": [{"term": {"pd": True}}]}}, "n": 1, "skip": None}
    ) == {"q[bool][must][0][term][pd]": "true", "n": "1"}


AIC_LIKE = """
type: json_api
name: Museum
url: https://api.example/search
headers: {X-Who: kds}
params:
  limit: 2
  query: {term: {pd: true}}
items: "data[?image_id]"
pick: first
fields:
  image:
    - "https://img.example/{{ image_id }}/big.jpg"
    - "https://img.example/{{ image_id }}/small.jpg"
  title: title
  author: artist.name
  credit: "{{ date }} · {{ fields.author }}"
  url: "'https://example/x'"
  width: dims.w
  height: dims.h
  extra_info: kind
constants:
  license: CC0
"""


def test_json_api_fields_templates_and_image_fallback(web, http, data_dir):
    web.route(
        "https://api.example/search",
        {
            "data": [
                {"image_id": None, "title": "skipped"},
                {
                    "image_id": "a1",
                    "title": "Etching",
                    "artist": {"name": "Rembrandt"},
                    "date": "1654",
                    "dims": {"w": 3000, "h": 4000},
                    "kind": "print",
                },
            ]
        },
    )
    web.route("https://img.example/a1/big.jpg", 404)
    web.route("https://img.example/a1/small.jpg", jpeg((300, 400)))
    card = source(AIC_LIKE, http, data_dir).fetch()
    assert card.title == "Etching"
    assert card.author == "Rembrandt"
    assert card.credit == "1654 · Rembrandt"  # templates see fields resolved before them
    assert card.url == "https://example/x"
    assert card.license == "CC0"
    assert card.meta["extra_info"] == "print"
    assert card.image.size == (300, 400)
    search = web.requests[0]
    assert search.headers["X-Who"] == "kds"
    assert search.url.params["query[term][pd]"] == "true"
    assert search.headers["User-Agent"].startswith("kindle-dash-server/")


def test_json_api_skips_items_rejected_by_filters(web, http, data_dir):
    web.route(
        "https://api.example/search",
        {
            "data": [
                {"image_id": "wide", "title": "Wide", "dims": {"w": 4000, "h": 1000}},
                {"image_id": "ok", "title": "Tall", "dims": {"w": 1000, "h": 1400}},
            ]
        },
    )
    web.route("https://img.example/ok/", jpeg((500, 700)))
    text = AIC_LIKE + "filters: {max_aspect: 1.5}\n"
    card = source(text, http, data_dir).fetch()
    assert card.title == "Tall"
    assert not any("wide" in u for u in web.urls())  # rejected from metadata, never downloaded


def test_json_api_filters_on_downloaded_size_too(web, http, data_dir):
    web.route("https://api.example/search", {"data": [{"image_id": "x", "title": "Small"}]})
    web.route("https://img.example/x/", jpeg((100, 100)))
    with pytest.raises(SourceError, match="width 100 < 600"):
        source(AIC_LIKE + "filters: {min_width: 600}\n", http, data_dir).fetch()


def test_json_api_required_and_where(web, http, data_dir):
    web.route("https://api.example/list", [1, 2, 3])
    web.route("https://api.example/obj/1", {"ok": False, "title": "no"})
    web.route("https://api.example/obj/2", {"ok": True, "title": ""})
    web.route("https://api.example/obj/3", {"ok": True, "title": "yes"})
    text = """
type: json_api
name: Detail
url: https://api.example/list
pick: first
detail_url: "https://api.example/obj/{{ item }}"
where: ok
required: [title]
fields: {title: title, body: "_item"}
layout: quote
"""
    card = source(text, http, data_dir).fetch()
    assert card.title == "yes"
    assert card.body == "3"  # the original list item is available as _item


def test_json_api_pages_repick_within_total(web, http, data_dir):
    def handler(req: httpx.Request) -> httpx.Response:
        skip = int(req.url.params["skip"])
        return httpx.Response(200, json={"total": 250, "data": [{"t": str(skip)}]})

    web.route("https://api.example/p", handler)
    text = """
type: json_api
name: Paged
url: https://api.example/p
pages: {param: skip, min: 0, max: 1000, step: 100, total_items_path: total, per_page: 100}
items: data
fields: {title: t}
layout: quote
response_ttl: 0
"""
    shown = {int(source(text, http, data_dir).fetch().title) for _ in range(30)}
    # 250 items = 3 pages of 100: whatever page is picked first, the item comes from 0..200.
    assert shown <= {0, 100, 200}
    assert len(shown) > 1


def test_json_api_daily_pick_is_stable_within_a_day(web, http, data_dir):
    web.route("https://api.example/d", {"data": [{"t": str(i)} for i in range(50)]})
    text = (
        "type: json_api\nname: D\nurl: https://api.example/d\nitems: data\npick: daily\n"
        "fields: {title: t}\nlayout: quote\n"
    )
    day1 = datetime(2026, 11, 2, 7, tzinfo=TZ)
    a = source(text, http, data_dir, now=day1).fetch().title
    b = source(text, http, data_dir, now=day1.replace(hour=22)).fetch().title
    picks = {
        source(text, http, data_dir, now=day1.replace(day=d)).fetch().title for d in range(1, 20)
    }
    assert a == b
    assert len(picks) > 5
    assert source(text, http, data_dir).cache_key() == "2026-11-02"


def test_json_api_templated_url_and_regex_filter(web, http, data_dir):
    web.route(
        "https://api.example/feed/2026/11/02",
        {
            "image": {
                "thumbnail": {"source": "https://up.example/thumb/a.jpg/960px-a.jpg", "width": 640}
            }
        },
    )
    web.route("https://up.example/thumb/a.jpg/1280px-a.jpg", jpeg())
    text = """
type: json_api
name: Feed
url: "https://api.example/feed/{{ today.strftime('%Y/%m/%d') }}"
items: image
fields:
  image: "{{ thumbnail.source | regex_replace('/[0-9]+px-', '/1280px-', 1) }}"
"""
    card = source(text, http, data_dir).fetch()
    assert card.image is not None


def test_json_api_errors_are_readable(web, http, data_dir):
    web.route("https://api.example/x", {"data": []})
    text = "type: json_api\nname: X\nurl: https://api.example/x\nitems: data\nfields: {}\n"
    with pytest.raises(SourceError, match="no items"):
        source(text, http, data_dir).fetch()
    with pytest.raises(Exception, match="HTTP 404"):
        source(text.replace("/x", "/missing"), http, data_dir).fetch()


def test_http_cache_and_stale_fallback(web, http, data_dir):
    calls = []

    def handler(req):
        calls.append(1)
        return (
            httpx.Response(200, json={"n": len(calls)}) if len(calls) == 1 else httpx.Response(500)
        )

    web.route("https://api.example/c", handler)
    assert http.get_json("https://api.example/c", ttl=3600) == {"n": 1}
    assert http.get_json("https://api.example/c", ttl=3600) == {"n": 1}  # cached
    assert len(calls) == 1
    # Expired: the server now fails, the stale copy is used.
    assert http.get_json("https://api.example/c", ttl=0.000001) == {"n": 1}


def test_cloudflare_challenge_is_explained(web, http):
    web.route(
        "https://cf.example/",
        lambda r: httpx.Response(
            403, headers={"cf-mitigated": "challenge"}, text="Just a moment..."
        ),
    )
    with pytest.raises(Exception, match="Cloudflare"):
        http.get_json("https://cf.example/x")


# -- text_file -------------------------------------------------------------------------------


def test_parse_fortune():
    entries = parse_fortune("""# a comment entry
%
First line
continues here.

Second paragraph.
— Seneca, Moral Letters 1
%
No author.
%

%
-- Oscar Wilde
""")
    assert len(entries) == 3
    assert entries[0].text == "First line continues here.\nSecond paragraph."
    assert (entries[0].author, entries[0].work) == ("Seneca", "Moral Letters 1")
    assert entries[1].author is None
    assert entries[2].text == "-- Oscar Wilde"  # a lone author line is just text


def test_text_file_source(http, data_dir):
    (data_dir / "q.txt").write_text("One\n— A\n%\nTwo\n— B\n%\n" + "x" * 900 + "\n")
    src = source("type: text_file\nname: Q\npath: q.txt\npick: daily", http, data_dir)
    card = src.fetch()
    assert card.body in ("One", "Two")  # the 900-char entry is over max_length
    assert card.layout == "quote"


def test_builtin_aphorisms_are_attributed(http, data_dir):
    src = source("type: text_file\nname: A\npath: builtin:texts/aphorisms.txt", http, data_dir)
    entries = src.entries()
    assert len(entries) >= 25
    assert all(e.author for e in entries)


def test_text_file_missing(http, data_dir):
    with pytest.raises(SourceError, match="can't read"):
        source("type: text_file\nname: Q\npath: nope.txt", http, data_dir).fetch()


# -- local_folder ----------------------------------------------------------------------------


def test_local_folder(http, data_dir):
    folder = data_dir / "pics"
    (folder / "sub").mkdir(parents=True)
    Image.new("L", (100, 100)).save(folder / "tiny_one.png")
    Image.new("L", (800, 1000)).save(folder / "sub" / "big_one.png")
    (folder / ".hidden.png").write_bytes(b"junk")
    text = "type: local_folder\nname: P\npath: pics\nkinds: [image]\nfilters: {min_width: 500}"
    card = source(text, http, data_dir).fetch()
    assert card.title == "big one"
    assert card.image.size == (800, 1000)


def test_local_folder_text_and_missing(http, data_dir):
    (data_dir / "notes").mkdir()
    (data_dir / "notes" / "n.md").write_text("# Title\n\nSome text.")
    card = source("type: local_folder\nname: N\npath: notes", http, data_dir).fetch()
    assert (card.title, card.body) == ("Title", "Some text.")
    with pytest.raises(SourceError, match="doesn't exist"):
        source("type: local_folder\nname: N\npath: nowhere", http, data_dir).fetch()
