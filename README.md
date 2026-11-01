# kindle-dash-server

A small self-hosted server that makes the image for an e-ink Kindle dashboard. It turns
interchangeable **sources** (public-domain prints, aphorisms, "on this day", a chess puzzle,
the moon phase, generative art…) into a PNG made for the Kindle Paperwhite 3's panel, and serves
it to [kindle-dash-pw3](https://github.com/pusi77/kindle-dash-pw3). A web UI manages the sources
and what is shown.

![The bundled sources, rendered live](docs/samples/live-sources.png)

- **Made for the panel**: 1072×1448, 8-bit greyscale PNG, dithered to the 16 grey levels the
  screen can show. Prints are toned so their yellowed paper becomes white.
- **In sync with the Kindle**: the Kindle reports its refresh schedule, and the server swaps the
  image right before each fetch, never in between. The endpoint just returns a file, instantly.
- **Sources are easy to add**: a YAML file for any JSON API, a text file or a folder; a Python
  file for anything else.
- **Fails gracefully**: if a source breaks, the next one is tried; if all fail, the Kindle keeps
  the last good image and the UI shows the error.
- **Self-contained**: no cloud, no headless browser, everything in one `/data` volume.

## Contents

- [Quick start](#quick-start)
- [Connect the Kindle](#connect-the-kindle)
- [How it works](#how-it-works)
- [Bundled sources](#bundled-sources)
- [Choosing what is shown](#choosing-what-is-shown)
- [Adding a source with YAML](#adding-a-source-with-yaml)
- [Adding a source in Python](#adding-a-source-in-python)
- [Layouts](#layouts)
- [Settings](#settings)
- [Endpoints](#endpoints)
- [Development](#development)
- [Licences](#licences)

## Quick start

With Docker (recommended), using the published image
(`ghcr.io/pusi77/kindle-dash-server`, amd64 and arm64):

```sh
mkdir kindle-dash-server && cd kindle-dash-server
curl -O https://raw.githubusercontent.com/pusi77/kindle-dash-server/main/docker-compose.yml
docker compose up -d
```

The web UI is at `http://<server>:8080`, the Kindle's image at `http://<server>:8080/dash.png`.
Everything the server keeps (settings, sources, plugins, cache) lives in `./data`, mounted as
`/data`. The container starts as root only to give `./data` to the user it runs as (uid/gid 1000,
or `PUID`/`PGID`), then drops to that user.

Update with `docker compose pull && docker compose up -d`. Image tags:

| Tag | |
|---|---|
| `latest` | the `main` branch |
| `X.Y.Z`, `X.Y` | releases (git tags `vX.Y.Z`) |
| `sha-<commit>` | a specific commit on `main` |

To build from a checkout instead, swap `image:` for `build: .` in `docker-compose.yml`.

Without Docker, with [uv](https://docs.astral.sh/uv/) and Python 3.12+:

```sh
uv sync
uv run kindle-dash-server --data-dir ./data --port 8080
```

Environment variables:

| Variable | Default | |
|---|---|---|
| `KDS_DATA_DIR` | `/data` | data volume (or `--data-dir`) |
| `KDS_BASIC_AUTH` | — | `user:password` enables HTTP basic auth on the web UI (the device endpoint and `/healthz` stay open) |
| `KDS_LOG_LEVEL` | `INFO` | |
| `KDS_HOST` / `KDS_PORT` | `0.0.0.0` / `8080` | or `--host` / `--port` |
| `TZ` | — | the container's clock; the server itself uses the timezone in Settings |
| `PUID` / `PGID` | `1000` | Docker only: the user and group the server runs as, and that `./data` is given to |

On first start the bundled sources are copied into `/data/sources/`. Each one appears once:
delete a default you don't want and it stays deleted. In Settings, set your **location** (for
sunrise and sunset) and a **contact** address, which is added to the User-Agent sent to the
public APIs.

## Connect the Kindle

On the Kindle, edit `/mnt/us/dashboard/local/env.sh`
([kindle-dash-pw3](https://github.com/pusi77/kindle-dash-pw3)):

```sh
DASH_URL="http://192.168.1.10:8080/dash.png"   # this server
INTERVAL_MINUTES=15
ACTIVE_HOURS="7-22"
TIMEZONE="Europe/Rome"
#DASH_TOKEN="..."   # only if you set a token in Settings → Device
```

Use **kindle-dash-pw3 pw3-v0.3.0 or later**. From that version the Kindle sends its schedule
with each request (see below), and the server follows it automatically. With an older version,
copy the same values into Settings → Refresh schedule.

Plain HTTP on the LAN is fine. If the server is reachable from the internet, put it behind HTTPS
(the Kindle supports TLS 1.2 and public CAs such as Let's Encrypt, not self-signed certificates)
and set a token.

### What the Kindle expects

| | |
|---|---|
| Request | `GET <DASH_URL>`, 30 s timeout, retried 3 times |
| Headers sent | `X-Kindle-Battery: 0-100`, `X-Kindle-Schedule`, `X-Kindle-Timezone`, `X-Kindle-Sleep-After` (pw3-v0.3.0+), `Authorization: Bearer <DASH_TOKEN>` if set |
| Headers received | `X-Dash-Schedule`, `X-Dash-Sleep-After`, only in "set from here" mode, applied only with `ALLOW_SERVER_SCHEDULE=true` |
| Image | PNG, **1072×1448 portrait**, **8-bit greyscale (colour type 0), no alpha**, drawn 1:1 from the top-left corner with no scaling or rotation |
| Grey levels | 16: 0x00, 0x11 … 0xFF |
| Errors | anything but 200 → the previous image stays on screen |

The server produces exactly this. For a Kindle mounted sideways, set Rotation in Settings: the
layouts then draw a landscape canvas, and the result is rotated back to 1072×1448.

## How it works

```
Source ──fetch()──▶ Card ──Layout──▶ canvas ──pipeline──▶ PNG ──▶ /dash.png
(YAML or Python)   image, title,     art, quote,    fit, tone, grey,
                   body, author,     article, info  16 levels + dither
                   credit, licence
```

- A **source** produces a **card**: an optional image, title, body, author, a credit line, the
  licence and some metadata. Sources never draw text themselves.
- A **layout** draws the card onto a canvas, with the bundled serif fonts.
- The **pipeline** fits the canvas to the exact size, rotates it if needed, converts it to
  greyscale and reduces it to 16 greys with dithering (Floyd–Steinberg, Atkinson, ordered or
  none). Photographic images are toned first: auto-contrast, gamma, and *paper white*. When most
  of an image is one bright tone, as in a scanned print, that tone becomes pure white instead of a
  speckled grey.

### In sync with the Kindle's refresh schedule

kindle-dash-pw3 wakes on a cron schedule. When the next slot is further away than
`SLEEP_SCREEN_INTERVAL`, it shows a sleep screen instead of fetching; that's how `ACTIVE_HOURS`
works. Since pw3-v0.3.0 it sends that schedule with each request:

```
X-Kindle-Schedule: */15 7-22 * * *   # REFRESH_SCHEDULE
X-Kindle-Timezone: Europe/Rome       # TIMEZONE
X-Kindle-Sleep-After: 960            # SLEEP_SCREEN_INTERVAL
```

The server replays the same loop. With the values above it renders at 06:59, 07:14 … 22:29,
then nothing until 06:59 the next day: at 22:45 the Kindle shows its sleep screen instead of
fetching. Each image is made **for** the moment it will be fetched, so daily picks, dates, the
moon phase and schedule slots are evaluated at 07:00, not at 06:59. The time between render and
fetch is configurable (default 60 s).

#### Setting the Kindle's schedule from the browser

Settings → Refresh schedule has three modes:

- **The Kindle's own schedule** (default): follow what the Kindle reports, as above.
- **Set the Kindle's schedule from here**: the schedule you enter is sent with the next image as
  `X-Dash-Schedule` / `X-Dash-Sleep-After`. The Kindle uses it from its next wake-up and confirms
  it on the following request. The page shows *will be sent*, *sent*, *applied*, or *not
  applied* (with the reason). This needs kindle-dash-pw3 with server schedules and
  `ALLOW_SERVER_SCHEDULE=true` in its `local/env.sh`: the Kindle only accepts what it's allowed
  to and validates everything. It refuses anything more frequent than every
  `SERVER_SCHEDULE_MIN_MINUTES` (default 5), and the server refuses the same schedules before
  sending them. Switching to another mode sends `default`, and the Kindle goes back to its
  `env.sh`.
- **Manual**: the values typed here, for a kindle-dash-pw3 too old to report its schedule.

The dashboard shows the schedule in use and whether it came from the Kindle, as well as the
battery level the Kindle reported. A schedule that would never fetch (an interval longer than
`SLEEP_SCREEN_INTERVAL`) is rejected with an explanation.

### Low battery

When the battery level the Kindle reports is at or below a threshold (default 20 %), a small
battery badge is drawn in a corner of the image; layouts keep their text clear of it. The
threshold and corner are in Settings (0 turns it off).

## Bundled sources

| Source | Type | Default | Notes |
|---|---|---|---|
| Art Institute of Chicago — prints | `json_api` | on | public-domain etchings, woodcuts, engravings, lithographs; IIIF images, CC0 |
| Cleveland Museum of Art — prints & drawings | `json_api` | on | CC0 |
| The Met — prints & drawings | `json_api` | off | public domain, 1450–1850; one extra request per candidate and full-size images, so off by default |
| Wikimedia — picture of the day | `json_api` | on | with its author and licence (often CC BY-SA) |
| Wikipedia — on this day | `json_api` | on | a selected anniversary with the article's summary |
| Aphorisms | `text_file` | on | a curated file of public-domain authors in public-domain translations |
| Lichess daily puzzle | `lichess_puzzle` | on | drawn as a board from the side to move (no solution) |
| Almanac | `almanac` | on | moon phase drawn to scale, sunrise/sunset, moonrise/moonset, next full and new moon; no network |
| Generative art | `generative` | on | flow fields, Truchet tiles, ridgelines; seeded by the date; no network |

All network sources use a descriptive User-Agent, timeouts, retries with backoff (respecting
`Retry-After`), at most one request per second per host, and a disk cache. If an API is down, a
stale cached response is used. Nothing copyrighted is bundled: images are fetched from the
museums with their licence and shown with a credit line.

## Choosing what is shown

The **What's shown** page:

- **Pinned**: always the same source. If it fails, the last image stays.
- **Rotation**: a different source at each fetch, by weight (each source has a weight, editable
  on the Sources page) or round robin. Optionally limited to a subset of sources.
- **Schedule**: time slots (`MON-FRI 07:00–09:00 → Almanac`), the rotation outside them. Days
  use cron syntax; an end time before the start crosses midnight; the first matching slot wins.

Whatever the mode, rendering always happens right before the Kindle's own fetches: these modes
only choose *what* each fetch shows. When the chosen source fails, the others are tried, healthy
ones first.

## Adding a source with YAML

A source is a YAML file in `/data/sources/`; its file name is its ID (`my-source.yaml` →
`my-source`). Create and edit sources in the web UI (**Sources → New source**, or **Duplicate**
an existing one). The editor validates the YAML and can preview it live without saving, and lists
every option of every type. Files edited by hand are picked up with **Reload files & plugins**.

Options every source has:

| Option | Default | |
|---|---|---|
| `type` | required | `json_api`, `text_file`, `local_folder`, `generative`, `almanac`, `lichess_puzzle`, or a plugin's type |
| `name` | required | shown in the UI |
| `description` | — | |
| `enabled` | `true` | |
| `layout` | `auto` | `art`, `quote`, `article`, `info`; `auto` lets the source decide |
| `layout_options` | `{}` | see [Layouts](#layouts) |
| `refresh` | `1h` | how long a fetched item is reused: `90`, `15m`, `6h`, `1d` |
| `weight` | `1` | for the weighted rotation |
| `filters` | — | `min_width`, `min_height`, `min_aspect`, `max_aspect` (width/height), `max_attempts` (default 8) |

### `text_file`: quotes, fortunes

```yaml
type: text_file
name: My quotes
path: texts/quotes.txt        # relative to /data; builtin:texts/aphorisms.txt is the bundled one
pick: random                  # or daily: the same entry all day
layout: quote
refresh: 15m
```

The file uses the `fortune` format: entries separated by a line containing only `%`. An optional
last line `— Author, Work` (also `--` or `~`) gives the author; text after the first comma is
shown smaller underneath.

```
Nothing, Lucilius, is ours, except time.
— Seneca, Moral Letters to Lucilius 1
%
Experience is the name every one gives to their mistakes.
— Oscar Wilde, Lady Windermere's Fan
```

### `local_folder`: your own pictures and notes

```yaml
type: local_folder
name: Family photos
path: photos                  # /data/photos, searched recursively
kinds: [image]                # image and/or text (.txt, .md)
pick: random
credit: "Our holidays"
filters: {min_width: 800}
```

The file name becomes the title (`summer_2019.jpg` → "summer 2019"). A text file whose first
line is `# Title` is shown as an article.

### `json_api`: any JSON API

This is the main way to add a source. The example is a simplified version of the bundled
Cleveland one:

```yaml
type: json_api
name: Cleveland prints
url: https://openaccess-api.clevelandart.org/api/artworks/
params:                       # query string; nested values become a[b][0]=c
  cc0: 1
  has_image: 1
  type: Print
  limit: 100
pages:                        # pick a random page of results
  param: skip
  min: 0
  max: 40
  step: 100                   # sends skip=0, 100, 200…
  total_items_path: info.total
  per_page: 100
items: "data[?images.web.url]"   # JMESPath to the list of items
pick: random                  # random | daily | first
fields:                       # JMESPath, or a Jinja template when it contains {{ }}
  image:                      # a list: tried in order until one downloads
    - images.print.url
    - images.web.url
  title: title
  author: "{{ creators[0].description if creators else '' }}"
  credit: "{{ creation_date }} · Cleveland Museum of Art · CC0"
  url: url
  width: images.print.width   # lets filters skip items before downloading
  height: images.print.height
constants:
  license: CC0
layout: art
refresh: 1h
response_ttl: 1d              # how long API responses are cached
filters: {min_aspect: 0.45, max_aspect: 1.6}
```

**Fields.** The card's fields are `image`, `title`, `body`, `author`, `credit`, `url`,
`license`; also `width`/`height` for the filters, and `kicker` (the small header of the
`article` and `info` layouts). Any other name is kept as metadata. A field value is:

- a **JMESPath** expression evaluated on the item (`title`, `artist.name`, `data[0].id`). Use a
  raw-string literal for a constant: `"'On this day'"`, or put it in `constants`;
- a **Jinja template** when it contains `{{` or `{%`. Templates see the item's keys directly,
  plus `item`, `fields` (fields resolved so far), `today`, `now` and `location`. All Jinja
  filters work (e.g. `random`, `truncate`, `striptags`), plus
  `regex_replace(pattern, replacement, count)`.

`url`, `params`, `headers`, `body` and `detail_url` can be templates too, e.g. a URL containing
`{{ today.strftime('%Y/%m/%d') }}`, or a parameter `"{{ ['Print', 'Drawing'] | random }}"` that
changes on each request.

**Other options.**

| Option | |
|---|---|
| `method`, `body` | `POST` with a JSON body |
| `headers`, `image_headers` | extra headers for the API and for image downloads |
| `items` | JMESPath to the list; unset means the response is the item (or the list) |
| `pick` | `random`, `daily` (the same item all day, a different one tomorrow), `first` |
| `pages` | `param`, `min`, `max`, `step` (sent value = index × step), and `total_path` (number of pages) or `total_items_path` + `per_page`: a page past the end is re-picked |
| `detail_url` | a template for a second request per item, e.g. the Met's search returns IDs only. The response becomes the item; the original is in `_item` |
| `where` | JMESPath condition the (detail) item must satisfy, e.g. `"isPublicDomain && primaryImage != ''"` |
| `required` | fields an item must have; default `[image]` for the art layout |
| `response_ttl` | cache time for API responses (default 6 h) |

Items that are missing a required field, don't match `where`, fail the filters or whose image
won't download are skipped, up to `filters.max_attempts`.

## Adding a source in Python

For anything YAML can't express, drop a `.py` file into `/data/plugins/`. Every `Source`
subclass with a `type_name` becomes a source type. The first time a plugin type is seen, a
**disabled** YAML file is created for it in `/data/sources/`, so it shows up in the UI; deleting
that file later doesn't bring it back. Load errors are shown on the Sources page.

A complete plugin
([`examples/plugins/countdown.py`](examples/plugins/countdown.py)):

```python
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
    defaults: ClassVar[dict] = {"name": "Countdown", "target": "2027-01-01",
                                "event": "New Year's Day"}

    def cache_key(self) -> str:
        # Cached cards are dropped when this changes: recompute every day.
        return self.ctx.today.isoformat()

    def fetch(self) -> Card:
        days = (self.config.target - self.ctx.today).days
        if days < 0:
            raise SourceError(f"{self.config.event} is in the past")
        text = "Today!" if days == 0 else f"{days} day{'s' if days != 1 else ''}"
        return Card(body=text, author=f"until {self.config.event}",
                    title=self.config.target.strftime("%-d %B %Y"))
```

Inside a source:

- `self.config`: your validated config.
- `self.ctx.http`: the shared polite HTTP client. `get_json(url, params=..., headers=...,
  ttl=...)` and `get_bytes(url)` give you caching, retries and the User-Agent for free.
- `self.ctx.now` / `self.ctx.today`: the moment the image is being made for (the next fetch),
  in your timezone.
- `self.ctx.settings`: e.g. `location`. `self.ctx.canvas_size` is the canvas size.
- Helpers: `self.rng("daily")` (a random generator that's stable all day), `self.load_image(bytes)`
  and `self.check_image(img)` (applies the filters).

Return a `Card`. Raise `SourceError` with a readable message on failure: it's shown in the UI
and the next source is tried. A card with an `image` can be drawn by any layout; set
`meta={"rows": [("Label", "value"), ...]}` for the `info` layout. Plugins run with the server's
permissions, so only install code you trust.

## Layouts

| Layout | For | `layout_options` |
|---|---|---|
| `art` | full-bleed image, caption at the bottom | `fit` (`auto`, `cover`, `contain`), `cover_tolerance` (0.15), `margin`, `caption` (`false` hides it), `title_size`, `credit_size`, `caption_padding` |
| `quote` | large centred serif text, author underneath | `max_size`, `line_spacing`, `author_size`, `margin` |
| `article` | header, optional picture, title, text | `kicker`, `date`, `date_format`, `image_height` (share of the page), `image_fit`, `title_height`, `body_max_size`, `margin` |
| `info` | centred picture, title, a table of rows | `image_height`, `row_size`, `raw_image`, `kicker`, `date`, `margin` |

The image toning can also be changed per source in `layout_options`: `autocontrast`,
`black_cutoff`, `white_cutoff`, `paper_white`, `gamma`. Sizes are given for the 1072 px-wide
canvas and scale with it.

## Settings

Settings are stored in `/data/settings.yaml` and edited on the Settings page:

- **Refresh schedule**: follow the schedule the Kindle reports, set it from here, or enter
  it manually; how long before each fetch to render.
- **Device**: image path (default `/dash.png`), optional token (`DASH_TOKEN`), low-battery
  badge threshold and corner.
- **Output**: size, rotation, grey levels, dithering, auto-contrast, paper white, gamma. The
  format is always PNG, 8-bit greyscale, which is what the Kindle draws.
- **Look & location**: font (Literata or EB Garamond; add more as
  `/data/fonts/<name>/regular.ttf` plus optional `italic.ttf` and `bold.ttf`), place, latitude,
  longitude, timezone, and a contact address for the User-Agent.

### The data volume

```
/data
├── settings.yaml
├── sources/*.yaml      one file per source
├── plugins/*.py        Python sources
├── fonts/<family>/     extra fonts
├── cache/              API responses, images, cards, previews, current.png
└── state.sqlite3       what's on screen, source status, recent events
```

## Endpoints

| | |
|---|---|
| `GET /dash.png` | the image for the Kindle (path configurable); records the battery level and schedule |
| `GET /preview/{source}.png` | renders a source on demand without changing what the Kindle shows; `?refresh=true` fetches a new item |
| `POST /render-now` | renders the image for the next fetch now (JSON, or HTML for the UI) |
| `GET /healthz` | `{"status": "ok", "has_image": …, "rendered_at": …, "rendered_for": …}` |
| `GET /current.png` | what the Kindle is shown, for the UI |
| `/`, `/sources`, `/display`, `/settings` | web UI |

## Development

```sh
uv sync
uv run pytest           # no test touches the network
uv run ruff check src tests examples && uv run ruff format --check src tests examples
uv run kindle-dash-server --data-dir ./data --port 8080
```

Stack: Python 3.12, FastAPI, Jinja2 and [htmx](https://htmx.org) (vendored, no JavaScript build
step), Pillow and NumPy for all rendering, httpx, pydantic, SQLite.

## Licences

- Code: MIT.
- Fonts: [Literata](https://github.com/googlefonts/literata),
  [EB Garamond](https://github.com/octaviopardo/EBGaramond12) and Noto Sans Symbols 2 (chess
  pieces), all under the SIL Open Font License 1.1 (`fonts/*/OFL.txt`).
- htmx: Zero-Clause BSD.
- Bundled aphorisms: public-domain authors in public-domain translations (George Long, Richard
  M. Gummere, Willis Bund & Hain Friswell). See the file header.
- Fetched content keeps its own licence, shown in each image's credit line: CC0 for the AIC,
  Cleveland and Met images; usually CC BY-SA for Wikimedia and Wikipedia.
