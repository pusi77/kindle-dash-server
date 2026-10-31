# kindle-dash-server

Self-hosted image server for an e-ink Kindle dashboard. It renders a PNG from interchangeable
**sources** (public-domain art, quotes, generative art…) and serves it to
[kindle-dash-pw3](https://github.com/pusi77/kindle-dash-pw3), with a small web UI to manage
sources and choose what is shown.

> Status: phase 1 (skeleton, pipeline, `text_file` / `json_api` / `generative` sources,
> device endpoint, minimal UI, Docker). Full docs come in phase 3.

![samples](docs/samples/contact-sheet.png)

## Client contract (kindle-dash-pw3)

| | |
|---|---|
| Request | `GET <DASH_URL>` (default path here: `/dash.png`, configurable), `curl -f --max-time 30` |
| Headers | `X-Kindle-Battery: 0-100`; `Authorization: Bearer <DASH_TOKEN>` if a token is set |
| Image | PNG, **1072×1448 portrait**, **8-bit greyscale (colour type 0), no alpha** |
| Levels | 16 greys: 0x00, 0x11 … 0xFF (dithered here, Floyd–Steinberg by default) |
| Errors | anything but 200 → the Kindle keeps the previous image |

Images are rendered ahead of time on clock-aligned slots (default every 15 min, 60 s early),
so the device endpoint just returns a cached file.

## Quick start

```sh
docker compose up -d --build          # UI on http://<host>:8080, data in ./data
```

or locally with uv:

```sh
uv sync
uv run kindle-dash-server --data-dir ./data --port 8080
```

Then on the Kindle, in `/mnt/us/dashboard/local/env.sh`:

```sh
DASH_URL="http://<server-ip>:8080/dash.png"
INTERVAL_MINUTES=15
```

## Layout of `/data`

```
settings.yaml     global settings (output, device path/token, location, schedule)
sources/*.yaml    one file per source (defaults are copied here on first start)
plugins/*.py      Python sources (phase 2)
fonts/<family>/   extra fonts: regular.ttf, italic.ttf, bold.ttf
cache/            HTTP cache, cards, previews, current.png
state.sqlite3     runtime state
```

## Endpoints

- `GET /dash.png` — the device image (path: `device.path` in settings.yaml)
- `GET /preview/{source_id}.png` — render a source on demand (doesn't change the device image)
- `POST /render-now`, `GET /healthz`
- `/` dashboard, `/sources` source list

Optional basic auth for the UI: `KDS_BASIC_AUTH=user:password`.

## Fonts and licences

Bundled fonts: Literata and EB Garamond, SIL Open Font License 1.1 (see `fonts/*/OFL.txt`).
htmx is vendored under its Zero-Clause BSD licence.
