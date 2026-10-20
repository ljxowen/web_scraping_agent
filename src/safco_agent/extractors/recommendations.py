"""Alternative products from the storefront's own Adobe Commerce Recommendations response.

Only units that express substitution/similarity are kept as alternatives. Generic merchandising
units (e.g. "Trending Products", "Recently viewed") say nothing about the product and are dropped.
"""
from __future__ import annotations

from ..models import AlternativeProduct

GENERIC_UNITS = ("trending", "recently viewed", "most viewed", "bestseller", "best seller", "popular",
                 "frequently bought", "bought together", "top selling")


def is_alternative_unit(unit_name: str) -> bool:
    n = (unit_name or "").lower()
    return bool(n) and not any(g in n for g in GENERIC_UNITS)


def _specific_categories(paths) -> set[str]:
    """Category paths that say what a product *is* (drop 'catalog' root and manufacturer listings)."""
    out = set()
    for c in paths or []:
        c = str(c).strip("/")
        if c.count("/") >= 1 and not c.startswith("shop-by-manufacturer"):
            out.add(c)
    return out


def alternatives_from_recs(payload: dict | None, own_sku: str | None = None,
                           own_categories: list[str] | None = None) -> list[AlternativeProduct]:
    """Keep recommended products that share a specific category with this product.

    The storefront's units are merchandising-driven (e.g. a house-brand unit shows Safco items for
    any page). Requiring a shared category turns them into genuine substitutes; with no category
    information for the product, nothing is filtered.
    """
    if not payload:
        return []
    mine = _specific_categories(own_categories)
    out: list[AlternativeProduct] = []
    seen: set[str] = set()
    for unit in payload.get("results", []) or []:
        name = unit.get("unitName") or ""
        if not is_alternative_unit(name):
            continue
        for p in unit.get("products", []) or []:
            url = p.get("url") or ""
            if url.startswith("//"):
                url = "https:" + url
            sku = p.get("sku")
            key = sku or url
            if not key or key in seen or (own_sku and sku == own_sku):
                continue
            if mine and not (mine & _specific_categories(p.get("categories"))):
                continue
            seen.add(key)
            out.append(AlternativeProduct(relation=name, name=p.get("name"), sku=sku, url=url or None))
    return out
