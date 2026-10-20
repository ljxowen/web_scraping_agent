"""Playwright wrapper: page rendering with resource blocking and capture of the site's own XHR JSON.

The browser only *observes* the responses the storefront requests for itself (Algolia InstantSearch
for listings, Adobe Commerce Recommendations for alternatives). It never calls those APIs directly.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import parse_qsl

from playwright.async_api import Browser, Page, Response, async_playwright

from ..config import AppConfig
from ..models import FetchedPage
from .http import FetchError, RobotsPolicy
from .logging import get_logger
from .rate_limit import TokenBucket

log = get_logger("browser")

ALGOLIA_MULTI_QUERY = "algolia.net/1/indexes/*/queries"
ADOBE_RECS = "commerce.adobe.io/recs"


def _safe_post_data(resp: Response) -> str | None:
    try:
        return resp.request.post_data
    except Exception:  # binary / gzip bodies raise on utf-8 decode
        return None


def parse_algolia_listing(post_data: str | None, body: str) -> dict | None:
    """Return the primary (hits-bearing) result of an InstantSearch multi-query, or None."""
    try:
        req = json.loads(post_data or "{}")
        res = json.loads(body)
    except json.JSONDecodeError:
        return None
    requests_ = req.get("requests") or []
    for i, r in enumerate(requests_):
        params = dict(parse_qsl(r.get("params", "")))
        if int(params.get("hitsPerPage", "0") or 0) > 0 and i < len(res.get("results", [])):
            result = res["results"][i]
            result["_facetFilters"] = params.get("facetFilters")
            return result
    return None


class ListingSession:
    """A live category page; exposes primitives the Navigator agent uses to paginate."""

    def __init__(self, page: Page, limiter: TokenBucket, settle_ms: int):
        self.page, self.limiter, self.settle_ms = page, limiter, settle_ms
        self.results: list[dict] = []
        self._event = asyncio.Event()
        page.on("response", self._on_response)

    async def _on_response(self, resp: Response) -> None:
        if ALGOLIA_MULTI_QUERY not in resp.url or resp.request.method != "POST":
            return
        try:
            body = await resp.text()
        except Exception:
            return
        parsed = parse_algolia_listing(_safe_post_data(resp), body)
        if parsed is not None:
            self.results.append(parsed)
            self._event.set()

    async def wait_for_result(self, previous_count: int, timeout_s: float = 20) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while len(self.results) <= previous_count and time.monotonic() < deadline:
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
        return self.results[-1] if len(self.results) > previous_count else None

    async def has_next(self) -> bool:
        nxt = self.page.locator(".ais-Pagination-item--nextPage:not(.ais-Pagination-item--disabled) a")
        return await nxt.count() > 0

    async def click_next(self) -> None:
        # Client-side pagination: the click triggers an Algolia query; no ?page= URL is fetched.
        await self.limiter.acquire()
        await self.page.locator(".ais-Pagination-item--nextPage a").first.click()
        await self.page.wait_for_timeout(self.settle_ms)

    async def rendered_product_links(self) -> list[str]:
        return await self.page.eval_on_selector_all(
            ".ais-Hits a[href*='/product/'], .ais-InfiniteHits a[href*='/product/']",
            "els => [...new Set(els.map(e => e.href))]",
        )

    async def close(self) -> None:
        await self.page.close()


class BrowserPool:
    def __init__(self, cfg: AppConfig, limiter: TokenBucket, robots: RobotsPolicy):
        self.cfg, self.limiter, self.robots = cfg, limiter, robots
        self._pw = None
        self.browser: Browser | None = None
        self._context = None
        self.stats = {"navigations": 0, "errors": 0}

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=self.cfg.browser.headless)
        self._context = await self.browser.new_context(user_agent=None, locale="en-US",
                                                       viewport={"width": 1366, "height": 900})
        await self._context.route("**/*", self._route)

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def _route(self, route) -> None:
        req = route.request
        if req.resource_type in self.cfg.browser.block_resource_types or any(
            h in req.url for h in self.cfg.browser.block_hosts
        ):
            await route.abort()
        else:
            await route.continue_()

    async def _goto(self, page: Page, url: str) -> int | None:
        if not self.robots.allowed(url):
            from .http import RobotsDisallowed
            raise RobotsDisallowed(url)
        await self.limiter.acquire()
        self.stats["navigations"] += 1
        try:
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=self.cfg.browser.navigation_timeout_ms)
        except Exception as e:  # playwright TimeoutError / net errors -> transient
            self.stats["errors"] += 1
            raise FetchError(url, f"browser navigation failed: {type(e).__name__}: {e}") from e
        status = resp.status if resp else None
        if status and status >= 400:
            self.stats["errors"] += 1
            raise FetchError(url, f"HTTP {status}", status, transient=status == 429 or status >= 500)
        return status

    async def open_listing(self, url: str) -> ListingSession:
        page = await self._context.new_page()
        session = ListingSession(page, self.limiter, self.cfg.browser.listing_settle_ms)
        try:
            await self._goto(page, url)
        except Exception:
            await page.close()
            raise
        return session

    async def render_product(self, url: str) -> FetchedPage:
        page = await self._context.new_page()
        captured: dict[str, Any] = {}
        recs_seen = asyncio.Event()

        async def on_response(resp: Response) -> None:
            if ADOBE_RECS in resp.url:
                try:
                    captured["recommendations"] = json.loads(await resp.text())
                    recs_seen.set()
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            status = await self._goto(page, url)
            # The grouped "Products" table renders lazily when scrolled into view. Rendering it gives the
            # LLM fallback visible evidence to work from if the embedded variant data ever disappears.
            try:
                await page.locator("section#product-grouped-info").first.scroll_into_view_if_needed(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            # Recommendations load lazily below the fold.
            for _ in range(4):
                await page.mouse.wheel(0, 2500)
                await page.wait_for_timeout(400)
            try:
                await asyncio.wait_for(recs_seen.wait(), self.cfg.browser.recommendations_timeout_ms / 1000)
            except asyncio.TimeoutError:
                log.info("recommendations_not_observed", url=url)
            html = await page.content()
            return FetchedPage(url=url, final_url=page.url, status=status, html=html,
                               via="browser", captured=captured)
        finally:
            await page.close()
