"""Navigator agent: discovers the category tree and walks category listings to find product URLs.

Category tree  -> server-rendered HTML (subcategory links under the seed path).
Listing pages  -> rendered in Playwright; product references are read from the storefront's own
                  Algolia InstantSearch responses, paginating with the on-page "Next" control.
Fallback       -> product links in the rendered listing DOM.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from ..extractors.jsonld import parse_jsonld
from ..infra.browser import BrowserPool
from ..infra.logging import get_logger
from ..models import Category, ListingHit

log = get_logger("navigator")

_ANCHOR = re.compile(r"<a\b[^>]*?href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)


def static_listing_hits(html: str, category_url: str) -> list[ListingHit]:
    """Server-rendered JSON-LD product ItemList (first listing page only) - last-resort discovery."""
    hits = []
    for il in parse_jsonld(LexborHTMLParser(html)).get("ItemList", []):
        for el in il.get("itemListElement", []) or []:
            item = el.get("item") if isinstance(el, dict) else None
            url = (el.get("url") if isinstance(el, dict) else None) or (item or {}).get("url")
            if isinstance(item, dict) and item.get("@type") == "Product" and url:
                hits.append(ListingHit(url=canonical_url(url), name=item.get("name"), category_url=category_url,
                                       source="jsonld"))
    return hits


def canonical_url(url: str, base: str = "https://www.safcodental.com") -> str:
    s = urlsplit(urljoin(base, url.strip()))
    path = re.sub(r"/{2,}", "/", s.path).rstrip("/") or "/"
    return urlunsplit(("https", s.netloc.lower(), path, "", ""))


def category_id_for(url: str) -> str:
    return urlsplit(canonical_url(url)).path.strip("/")


@dataclass
class ListingTraversal:
    category_id: str
    hits: list[ListingHit] = field(default_factory=list)
    nb_hits: int | None = None
    nb_pages: int | None = None
    pages_seen: int = 0
    source: str = "algolia"
    stopped_reason: str = ""


class NavigatorAgent:
    def __init__(self, browser: BrowserPool | None, base_url: str):
        self.browser, self.base_url = browser, base_url

    # ---------------------------------------------------------------- categories
    def _is_child(self, parent_path: str, url: str) -> bool:
        path = urlsplit(url).path
        if not path.startswith(parent_path + "/"):
            return False
        rest = path[len(parent_path) + 1:]
        return bool(rest) and "/" not in rest

    def discover_subcategories(self, parent: Category, html: str, root_id: str) -> list[Category]:
        """Subcategories = pages exactly one path segment below the parent category.

        Primary source: the page's JSON-LD subcategory ItemList (CollectionPage entries).
        Fallback: anchor hrefs (regex scan, robust to the malformed rich-text markup on these pages).
        """
        parent_path = urlsplit(parent.canonical_url).path.rstrip("/")
        names: dict[str, Counter] = {}
        for il in parse_jsonld(LexborHTMLParser(html)).get("ItemList", []):
            for el in il.get("itemListElement", []) or []:
                item = el.get("item") if isinstance(el, dict) else None
                if isinstance(item, dict) and item.get("@type") == "CollectionPage" and item.get("url"):
                    url = canonical_url(item["url"], self.base_url)
                    if self._is_child(parent_path, url):
                        names.setdefault(url, Counter())[item.get("name") or ""] += 100
        if not names:
            for m in _ANCHOR.finditer(html):
                href = m.group(1)
                if href.startswith(("javascript:", "#", "mailto:")):
                    continue
                url = canonical_url(href, self.base_url)
                if not self._is_child(parent_path, url):
                    continue
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
                names.setdefault(url, Counter())
                if text and len(text) < 80:
                    names[url][text] += 1
        out = []
        for url, counter in sorted(names.items()):
            name = next((n for n, _ in counter.most_common() if n), None) or \
                url.rsplit("/", 1)[-1].replace("-", " ").title()
            out.append(Category(category_id=category_id_for(url), name=name, canonical_url=url,
                                parent_category_id=parent.category_id, root_category_id=root_id,
                                depth=parent.depth + 1))
        return out

    @staticmethod
    def category_name_from_html(html: str, fallback: str) -> str:
        tree = LexborHTMLParser(html)
        h1 = tree.css_first("main h1") or tree.css_first("h1")
        name = h1.text(strip=True) if h1 else ""
        return name or fallback

    # ------------------------------------------------------------------ listings
    async def traverse_listing(self, category: Category, max_products: int | None = None,
                               max_pages: int | None = None) -> ListingTraversal:
        assert self.browser is not None
        result = ListingTraversal(category_id=category.category_id)
        session = await self.browser.open_listing(category.canonical_url)
        seen: set[str] = set()
        try:
            res = await session.wait_for_result(0)
            if res is None:
                # Decision-gate fallback: no observable Algolia response -> rendered DOM links.
                links = await session.rendered_product_links()
                result.source, result.stopped_reason = "dom", "algolia_not_observed"
                for link in links:
                    url = canonical_url(link)
                    if url not in seen:
                        seen.add(url)
                        result.hits.append(ListingHit(url=url, category_url=category.canonical_url, source="dom"))
                if not result.hits:
                    result.source = "jsonld"
                    result.hits = static_listing_hits(await session.page.content(), category.canonical_url)
                log.warning("listing_dom_fallback", category=category.category_id, products=len(result.hits))
                return result
            result.nb_hits, result.nb_pages = res.get("nbHits"), res.get("nbPages")
            while True:
                result.pages_seen += 1
                new = 0
                for h in res.get("hits", []):
                    url = h.get("url") or h.get("family_url")
                    if not url:
                        continue
                    url = canonical_url(url)
                    if url in seen:
                        continue
                    seen.add(url)
                    new += 1
                    result.hits.append(ListingHit(
                        url=url, source_product_id=str(h.get("objectID") or h.get("family_id") or "") or None,
                        name=h.get("name"), type_id=h.get("type_id"), category_url=category.canonical_url,
                        source="algolia",
                        raw={k: h.get(k) for k in ("objectID", "type_id", "manufacturer_name", "stock_availability",
                                                    "categories", "sku")},
                    ))
                page_no = res.get("page", 0)
                log.info("listing_page", category=category.category_id, page=page_no, new_products=new,
                         total=len(result.hits), nb_hits=result.nb_hits)
                if max_products is not None and len(result.hits) >= max_products:
                    result.stopped_reason = "product_limit"
                    break
                if max_pages is not None and result.pages_seen >= max_pages:
                    result.stopped_reason = "page_limit"
                    break
                if result.nb_pages is not None and page_no + 1 >= result.nb_pages:
                    result.stopped_reason = "last_page"
                    break
                if new == 0 and result.pages_seen > 1:
                    result.stopped_reason = "no_new_products"
                    break
                if not await session.has_next():
                    result.stopped_reason = "no_next_control"
                    break
                before = len(session.results)
                await session.click_next()
                res = await session.wait_for_result(before)
                if res is None:
                    result.stopped_reason = "pagination_timeout"
                    log.warning("listing_pagination_timeout", category=category.category_id, page=page_no + 1)
                    break
        finally:
            await session.close()
        if max_products is not None:
            result.hits = result.hits[:max_products]
        return result
