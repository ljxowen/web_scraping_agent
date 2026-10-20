"""Level-1 extraction: schema.org JSON-LD blocks embedded in the page."""
from __future__ import annotations

import json
from typing import Any

from selectolax.lexbor import LexborHTMLParser


def _flatten(node: Any) -> list[dict]:
    if isinstance(node, list):
        return [x for n in node for x in _flatten(n)]
    if isinstance(node, dict):
        if "@graph" in node:
            return _flatten(node["@graph"])
        return [node]
    return []


def parse_jsonld(tree: LexborHTMLParser) -> dict[str, list[dict]]:
    """Return JSON-LD objects grouped by @type. Malformed blocks are skipped, not fatal."""
    out: dict[str, list[dict]] = {}
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text(deep=True, strip=False) or "")
        except json.JSONDecodeError:
            continue
        for obj in _flatten(data):
            types = obj.get("@type")
            for t in types if isinstance(types, list) else [types]:
                if t:
                    out.setdefault(str(t), []).append(obj)
    return out


def product_node(ld: dict[str, list[dict]]) -> dict | None:
    nodes = ld.get("Product") or ld.get("ProductGroup") or []
    return nodes[0] if nodes else None


def breadcrumb_names(ld: dict[str, list[dict]]) -> list[tuple[str, str | None]]:
    """[(name, url), ...] ordered by position, excluding 'Home'."""
    lists = ld.get("BreadcrumbList") or []
    if not lists:
        return []
    items = sorted(lists[0].get("itemListElement", []), key=lambda i: i.get("position", 0))
    out = []
    for it in items:
        item = it.get("item")
        name = it.get("name") or (item.get("name") if isinstance(item, dict) else None)
        url = item if isinstance(item, str) else (item.get("@id") if isinstance(item, dict) else None)
        if name and name.strip().lower() != "home":
            out.append((name.strip(), url))
    return out


def offers_list(product: dict) -> list[dict]:
    offers = product.get("offers")
    if isinstance(offers, dict) and offers.get("@type") == "AggregateOffer" and offers.get("offers"):
        return list(offers["offers"]) + [offers]
    if isinstance(offers, dict):
        return [offers]
    if isinstance(offers, list):
        return offers
    return []
