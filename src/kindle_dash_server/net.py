"""A polite HTTP client: descriptive User-Agent, timeouts, retries with backoff, disk cache,
and a minimum delay between requests to the same host."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import __version__

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


def user_agent(contact: str = "") -> str:
    extra = f"; {contact}" if contact else ""
    return (
        f"kindle-dash-server/{__version__} "
        f"(+https://github.com/pusi77/kindle-dash-server; e-ink dashboard{extra})"
    )


class HttpClient:
    def __init__(
        self,
        cache_dir: Path,
        contact: str = "",
        timeout: float = 20.0,
        retries: int = 3,
        min_host_interval: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.cache_dir = cache_dir
        self.retries = retries
        self.min_host_interval = min_host_interval
        self._host_last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(
            headers={"User-Agent": user_agent(contact), "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )

    def set_contact(self, contact: str) -> None:
        self._client.headers["User-Agent"] = user_agent(contact)

    def close(self) -> None:
        self._client.close()

    # -- cache -------------------------------------------------------------------------------

    def _key(self, method: str, url: str, params: Any, body: Any) -> Path:
        raw = json.dumps([method, url, params, body], sort_keys=True, default=str)
        h = hashlib.sha256(raw.encode()).hexdigest()
        return self.cache_dir / h[:2] / h

    def _cached(self, path: Path, ttl: float) -> bytes | None:
        try:
            if ttl > 0 and time.time() - path.stat().st_mtime < ttl:
                return path.read_bytes()
        except FileNotFoundError:
            pass
        return None

    def _store(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def prune(self, max_bytes: int = 500 * 1024 * 1024, max_age: float = 30 * 86400) -> int:
        """Drop entries older than max_age, then oldest first until under max_bytes."""
        files = [p for p in self.cache_dir.rglob("*") if p.is_file()]
        now = time.time()
        removed = 0
        entries = []
        for p in files:
            st = p.stat()
            if now - st.st_mtime > max_age:
                p.unlink(missing_ok=True)
                removed += 1
            else:
                entries.append((st.st_mtime, st.st_size, p))
        total = sum(s for _, s, _ in entries)
        for _, size, p in sorted(entries):
            if total <= max_bytes:
                break
            p.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed

    # -- requests ----------------------------------------------------------------------------

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        with self._lock:
            wait = self._host_last.get(host, 0) + self.min_host_interval - time.monotonic()
            self._host_last[host] = time.monotonic() + max(0.0, wait)
        if wait > 0:
            time.sleep(wait)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: dict | None = None,
        json_body: Any = None,
        ttl: float = 0,
    ) -> bytes:
        key = self._key(method, url, params, json_body)
        hit = self._cached(key, ttl)
        if hit is not None:
            return hit
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle(url)
            try:
                resp = self._client.request(
                    method, url, params=params, headers=headers, json=json_body
                )
            except httpx.TransportError as exc:
                last_exc = FetchError(f"{urlsplit(url).netloc}: {type(exc).__name__}: {exc}")
                delay = 2**attempt + random.random()
            else:
                if resp.status_code < 400:
                    data = resp.content
                    if ttl > 0:
                        self._store(key, data)
                    return data
                last_exc = FetchError(f"HTTP {resp.status_code} for {resp.request.url}")
                if resp.status_code in (403, 503) and (
                    "cf-mitigated" in resp.headers or b"challenges.cloudflare.com" in resp.content
                ):
                    last_exc = FetchError(
                        f"{urlsplit(url).netloc}: blocked by Cloudflare bot protection "
                        f"(HTTP {resp.status_code}); this usually depends on the server's IP"
                    )
                    break
                if resp.status_code not in RETRY_STATUS:
                    break
                delay = _retry_after(resp) or 2**attempt + random.random()
            if attempt < self.retries:
                log.info("retrying %s in %.1fs (%s)", url, delay, last_exc)
                time.sleep(min(delay, 60))
        # Stale cache beats nothing.
        if key.exists():
            log.warning("using stale cache for %s after: %s", url, last_exc)
            return key.read_bytes()
        raise FetchError(str(last_exc)) from last_exc

    def get_json(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict | None = None,
        ttl: float = 3600,
        method: str = "GET",
        json_body: Any = None,
    ) -> Any:
        data = self.request(
            method, url, params=params, headers=headers, json_body=json_body, ttl=ttl
        )
        try:
            return json.loads(data)
        except ValueError as exc:
            raise FetchError(f"invalid JSON from {url}: {exc}") from exc

    def get_bytes(self, url: str, *, headers: dict | None = None, ttl: float = 7 * 86400) -> bytes:
        return self.request("GET", url, headers=headers, ttl=ttl)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value and value.isdigit():
        return float(value)
    return None
