"""Level-2 extraction of purchasable SKUs (variants) and value normalization helpers.

Safco (Magento/Hyvä) embeds the grouped-product child table as `window.masterData = "<json string>"`.
Each entry is one purchasable SKU with its own price, stock status and manufacturer part number.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from decimal import Decimal, InvalidOperation

from ..models import Availability, Variant

SIZE_WORDS = ["xx-small", "x-small", "extra small", "small", "medium", "large", "x-large", "extra large",
              "xx-large", "xxx-large"]
_SIZE_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(SIZE_WORDS, key=len, reverse=True)) + r")\b", re.I)
_UNITS = ("box|bx|pk|pack|package|case|cs|bag|bottle|btl|ea|each|roll|tube|kit|jar|dozen|dz|tray|pouch|vial|"
          "syringe|carton|ctn|sleeve|cartridge|refill|pad|sheet|unit|pc|pcs|set|cup|can|packet|gallon|bucket")
_PACK_SLASH = re.compile(rf"(\d[\d,]*)\s*/\s*({_UNITS})\b", re.I)
_PACK_OF = re.compile(rf"\b({_UNITS})\s+of\s+(\d[\d,]*)\b", re.I)
_PACK_PER = re.compile(rf"(\d[\d,]*)\s+(?:[a-z-]+\s+){{0,2}}per\s+({_UNITS})\b", re.I)
_PACK_DASH = re.compile(r"\b(\d[\d,]*)[- ]?(pack|pk|count|ct)\b", re.I)
_CONTAINER = re.compile(rf"\b({_UNITS})\b\s*$", re.I)
_VOLUME = re.compile(r"(?<![\w.])(\d*\.?\d+)\s?(cc|ml|fl\.? ?oz|oz|gm|kg|g|l|lb)\b", re.I)
_DIMENSIONS = re.compile(r"\b\d+(?:\.\d+)?\s?(?:mm|cm)?\s?x\s?\d+(?:\.\d+)?(?:\s?x\s?\d+(?:\.\d+)?)?\s?(?:mm|cm|in)\b",
                         re.I)
_UNIT_CANON = {"bx": "box", "pk": "pack", "package": "pack", "cs": "case", "btl": "bottle", "ea": "each",
               "dz": "dozen", "ctn": "carton", "pcs": "pc"}


def strip_html(value) -> str:
    """Option labels sometimes carry Page Builder HTML; keep text only."""
    if not value or not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def parse_price(value) -> Decimal | None:
    if value is None or value == "" or value is False:
        return None
    s = str(value).replace("$", "").replace(",", "").strip()
    try:
        d = Decimal(s)
    except InvalidOperation:
        m = re.search(r"\d+(?:\.\d+)?", s)
        if not m:
            return None
        d = Decimal(m.group(0))
    if d <= 0:
        return None  # Magento renders 0 for "call for price" / hidden prices; that's unknown, not free
    return d.quantize(Decimal("0.01"))


def normalize_availability(value: str | None) -> Availability:
    if not value:
        return Availability.UNKNOWN
    v = value.lower().replace("https://schema.org/", "").replace("http://schema.org/", "")
    v = v.replace("_", "").replace(" ", "").replace("-", "")
    if v in ("instock", "limitedavailability", "onlineonly"):
        return Availability.IN_STOCK
    if v in ("outofstock", "soldout"):
        return Availability.OUT_OF_STOCK
    if v in ("backorder", "preorder", "presale", "backordered"):
        return Availability.BACKORDER
    if v in ("specialorder",):
        return Availability.SPECIAL_ORDER
    if v in ("directfrommanufacturer", "dropship", "shipsfrommanufacturer"):
        return Availability.DROP_SHIP
    if v in ("nonpurchasable", "notpurchasable", "notavailable"):
        return Availability.NOT_PURCHASABLE
    if v in ("discontinued",):
        return Availability.DISCONTINUED
    return Availability.UNKNOWN


def parse_pack(text: str | None) -> tuple[int | None, str | None]:
    """'X-small, 250/box' -> (250, 'box'); 'Box of 12' -> (12, 'box'); unknown -> (None, None)."""
    if not text:
        return None, None
    for rx, num_first in ((_PACK_SLASH, True), (_PACK_OF, False), (_PACK_PER, True), (_PACK_DASH, True)):
        m = rx.search(text)
        if m:
            num, unit = (m.group(1), m.group(2)) if num_first else (m.group(2), m.group(1))
            try:
                n = int(num.replace(",", ""))
            except ValueError:
                continue
            unit = unit.lower()
            if unit in ("count", "ct", "pk"):
                unit = "pack"
            return n, _UNIT_CANON.get(unit, unit)
    return None, None


def parse_container(text: str | None) -> str | None:
    """Sellable unit when only the container is named ('0.5cc jar' -> 'jar'); count is not inferred."""
    m = _CONTAINER.search(text or "")
    if not m:
        return None
    unit = m.group(1).lower()
    return _UNIT_CANON.get(unit, unit)


def parse_option_attributes(option_text: str | None) -> dict[str, str]:
    """Best-effort attribute parsing from an option label. Only emits what is literally present."""
    attrs: dict[str, str] = {}
    if not option_text:
        return attrs
    m = _SIZE_RE.search(option_text)
    if m:
        attrs["size"] = m.group(1).lower()
    n, unit = parse_pack(option_text)
    if n:
        attrs["pack"] = f"{n}/{unit}"
    m = _VOLUME.search(option_text)
    if m:
        attrs["volume"] = f"{m.group(1)}{m.group(2).lower().replace(' ', '')}"
    m = _DIMENSIONS.search(option_text)
    if m:
        attrs["dimensions"] = re.sub(r"\s+", " ", m.group(0)).strip()
    return attrs


def extract_master_data(html: str) -> dict | None:
    marker = "window.masterData"
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("=", i) + 1
    while j < len(html) and html[j].isspace():
        j += 1
    try:
        raw, _ = json.JSONDecoder().raw_decode(html, j)
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def variants_from_master_data(data: dict) -> list[Variant]:
    out: list[Variant] = []
    for key, v in sorted(data.items(), key=lambda kv: (kv[1].get("position") or 0, kv[0])):
        if not isinstance(v, dict):
            continue
        sku = str(v.get("sku") or key).strip()
        option = strip_html(v.get("description")) or None
        name = strip_html(v.get("name")) or None
        pack_n, unit = parse_pack(option) if option else (None, None)
        if pack_n is None:
            pack_n, unit = parse_pack(name)
        if unit is None:
            unit = parse_container(option) or parse_container(name)
        mpn = v.get("manufacturer_part_number")
        out.append(Variant(
            sku=sku,
            name=name,
            manufacturer_code=str(mpn).strip() if mpn not in (None, "", 0, "0") else None,
            option_text=option,
            attributes=parse_option_attributes(option),
            price=parse_price(v.get("product_price")),
            currency="USD",
            # the label is the customer-facing state; the code is a fallback
            availability=normalize_availability(v.get("stock_availability_label") or v.get("stock_availability")),
            availability_label=v.get("stock_availability_label"),
            pack_size=pack_n,
            unit_of_measure=unit,
            source_variant_id=str(v.get("id")) if v.get("id") else None,
        ))
    return out
