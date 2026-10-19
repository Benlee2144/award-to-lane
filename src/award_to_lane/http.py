"""A polite HTTP session: identifiable User-Agent, spacing between requests, retries, cached downloads."""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("award_to_lane.http")


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class PoliteSession:
    def __init__(self, user_agent: str, min_interval: float = 3.0, timeout: float = 60,
                 max_retries: int = 3, referer: str | None = None):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        if referer:
            self.session.headers["Referer"] = referer
        self.min_interval = float(min_interval)
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self._last_request = 0.0
        self.requests_made = 0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
            stream: bool = False) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._wait()
            self._last_request = time.monotonic()
            self.requests_made += 1
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout, stream=stream)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                log.warning("request failed (%s), attempt %d/%d: %s", type(exc).__name__, attempt, self.max_retries, url)
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code in (403, 404, 410):
                    raise HttpError(resp.status_code, url)
                last_exc = HttpError(resp.status_code, url)
                log.warning("HTTP %s, attempt %d/%d: %s", resp.status_code, attempt, self.max_retries, url)
            time.sleep(min(30, 2 ** attempt))
        assert last_exc is not None
        raise last_exc

    def get_json(self, url: str, *, params: dict | None = None):
        resp = self.get(url, params=params, headers={"Accept": "application/json"})
        return resp.json()

    def download(self, url: str, dest: Path, *, expect_pdf: bool = False) -> str:
        """Download to dest (skipped if a non-empty file already exists). Returns the sha256 hex digest."""
        if dest.exists() and dest.stat().st_size > 0:
            log.debug("cached: %s", dest.name)
            return sha256_file(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = self.get(url, headers={"Accept": "application/pdf,*/*"} if expect_pdf else None)
        content = resp.content
        if expect_pdf and not content.startswith(b"%PDF-"):
            raise HttpError(resp.status_code, f"{url} (not a PDF: {content[:40]!r})")
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(content)
        tmp.replace(dest)
        return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
