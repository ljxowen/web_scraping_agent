"""HTTP fetcher: robots.txt aware, rate limited, bounded retries with backoff + jitter."""
from __future__ import annotations

import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import (AsyncRetrying, retry_if_exception, stop_after_attempt,
                      wait_exponential_jitter)

from ..config import AppConfig
from ..models import FetchedPage
from .logging import get_logger
from .rate_limit import TokenBucket

log = get_logger("http")


class FetchError(Exception):
    def __init__(self, url: str, message: str, status: int | None = None, transient: bool = True):
        super().__init__(message)
        self.url, self.status, self.transient = url, status, transient


class RobotsDisallowed(FetchError):
    def __init__(self, url: str):
        super().__init__(url, "disallowed by robots.txt", transient=False)


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, FetchError) and exc.transient


class RobotsPolicy:
    def __init__(self, base_url: str, user_agent: str, enabled: bool = True):
        self.base_url, self.user_agent, self.enabled = base_url, user_agent, enabled
        self._parser: RobotFileParser | None = None

    async def load(self, client: httpx.AsyncClient) -> None:
        if not self.enabled:
            return
        rp = RobotFileParser()
        try:
            r = await client.get(self.base_url.rstrip("/") + "/robots.txt")
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        self._parser = rp

    def allowed(self, url: str) -> bool:
        if not self.enabled or self._parser is None:
            return True
        # urllib's parser ignores wildcards; check the site's query-pattern rules explicitly too.
        q = urlparse(url).query
        if any(q.startswith(p) or f"&{p}" in q for p in ("p=", "page=", "price", "sortBy", "pageType=")):
            return False
        return self._parser.can_fetch(self.user_agent, url) and self._parser.can_fetch("*", url)


class HttpFetcher:
    def __init__(self, cfg: AppConfig, limiter: TokenBucket):
        self.cfg = cfg
        self.limiter = limiter
        self.client = httpx.AsyncClient(
            headers={"User-Agent": cfg.site.user_agent, "Accept-Language": "en-US,en;q=0.9"},
            timeout=cfg.http.timeout_seconds,
            follow_redirects=True,
        )
        self.robots = RobotsPolicy(cfg.site.base_url, cfg.site.user_agent, cfg.site.respect_robots)
        self.stats = {"requests": 0, "retries": 0, "errors": 0}

    async def start(self) -> None:
        await self.robots.load(self.client)

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch(self, url: str) -> FetchedPage:
        if not self.robots.allowed(url):
            raise RobotsDisallowed(url)
        attempt_no = 0
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.cfg.http.max_retries),
            wait=wait_exponential_jitter(initial=self.cfg.http.backoff_base_seconds,
                                         max=self.cfg.http.backoff_max_seconds),
            retry=retry_if_exception(_is_transient),
            reraise=True,
        ):
            with attempt:
                attempt_no += 1
                if attempt_no > 1:
                    self.stats["retries"] += 1
                return await self._fetch_once(url, attempt_no)
        raise AssertionError("unreachable")

    async def _fetch_once(self, url: str, attempt_no: int) -> FetchedPage:
        await self.limiter.acquire()
        self.stats["requests"] += 1
        t0 = time.monotonic()
        try:
            r = await self.client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            self.stats["errors"] += 1
            log.warning("fetch_transport_error", url=url, attempt=attempt_no, error=str(e))
            raise FetchError(url, f"{type(e).__name__}: {e}") from e
        dur = round(time.monotonic() - t0, 3)
        if r.status_code == 429 or r.status_code >= 500:
            self.stats["errors"] += 1
            retry_after = r.headers.get("Retry-After")
            if r.status_code == 429:
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 30.0
                self.limiter.back_off(wait)
            log.warning("fetch_retryable_status", url=url, status=r.status_code, attempt=attempt_no)
            raise FetchError(url, f"HTTP {r.status_code}", r.status_code, transient=True)
        if r.status_code >= 400:
            # 403/404 etc. are permanent: never retried, never bypassed.
            self.stats["errors"] += 1
            log.warning("fetch_permanent_status", url=url, status=r.status_code)
            raise FetchError(url, f"HTTP {r.status_code}", r.status_code, transient=False)
        log.debug("fetched", url=url, status=r.status_code, duration_s=dur, attempt=attempt_no)
        return FetchedPage(url=url, final_url=str(r.url), status=r.status_code, html=r.text, via="http")
