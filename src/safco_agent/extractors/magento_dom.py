"""Level-2 extraction from Magento/Hyvä DOM structures (fields JSON-LD does not represent fully)."""
from __future__ import annotations

import html as htmllib
import re
from urllib.parse import urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

_IMG_FULL = re.compile(r'"full"\s*:\s*"([^"]+)"')
_SPEC_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /&().%-]{1,40}?)\s*:\s*(.{1,200})$")


def canonical_image(url: str) -> str:
    """Drop resize query params so the same image isn't listed once per rendition."""
    url = htmllib.unescape(url.replace("\\/", "/"))
    s = urlsplit(url)
    return urlunsplit((s.scheme or "https", s.netloc, s.path, "", ""))


def product_type(tree: LexborHTMLParser) -> str | None:
    body = tree.body
    cls = (body.attributes.get("class") if body else "") or ""
    m = re.search(r"page-product-(\w+)", cls)
    return m.group(1) if m else None


def source_product_id(tree: LexborHTMLParser) -> str | None:
    node = tree.css_first('#product_addtocart_form input[name="product"]') or tree.css_first('input[name="product"]')
    val = node.attributes.get("value") if node else None
    return val.strip() if val and val.strip().isdigit() else None


def form_sku(tree: LexborHTMLParser) -> str | None:
    node = tree.css_first("#product_addtocart_form")
    return (node.attributes.get("data-sku") or None) if node else None


def heading(tree: LexborHTMLParser) -> str | None:
    h1 = tree.css_first("main h1") or tree.css_first("h1")
    t = h1.text(strip=True) if h1 else ""
    return t or None


def description_block(tree: LexborHTMLParser):
    return tree.css_first("section#description .prose") or tree.css_first("section#description")


def description_lines(tree: LexborHTMLParser) -> list[str]:
    """Description as ordered lines (paragraphs and bullet items), preserving list structure."""
    block = description_block(tree)
    if block is None:
        return []
    lines: list[str] = []
    for node in block.css("p, li, h3, h4, td"):
        # skip containers whose text is already captured by a nested li/p
        # lexbor's node.css() includes the node itself, hence "> 1"
        if node.tag in ("p", "td") and len(node.css("li, p")) > 1:
            continue
        t = re.sub(r"\s+", " ", node.text(deep=True, separator=" ")).strip()
        if t and t not in lines:
            lines.append(t)
    if not lines:
        t = re.sub(r"\s+", " ", block.text(separator=" ")).strip()
        if t and t.lower() != "description":
            lines = [t]
    return lines


def specs_from_lines(lines: list[str]) -> dict[str, str]:
    """Deterministic 'Key: value' spec lines (e.g. 'Thickness: at palm 2.0 mils')."""
    specs: dict[str, str] = {}
    for line in lines:
        m = _SPEC_LINE.match(line)
        if m and not m.group(1).lower().startswith(
                ("note", "please", "http", "warning", "caution", "important", "attention")):
            specs.setdefault(m.group(1).strip(), m.group(2).strip())
    return specs


def spec_table(tree: LexborHTMLParser) -> dict[str, str]:
    specs: dict[str, str] = {}
    for table in tree.css("#product-attribute-specs-table, section#specifications table, .additional-attributes"):
        for row in table.css("tr"):
            th, td = row.css_first("th"), row.css_first("td")
            if th and td:
                k, v = th.text(strip=True), td.text(strip=True)
                if k and v:
                    specs[k] = v
    return specs


def gallery_images(html: str) -> list[str]:
    seen: list[str] = []
    for m in _IMG_FULL.finditer(html):
        u = canonical_image(m.group(1))
        if "/media/catalog/product/" in u and u not in seen:
            seen.append(u)
    return seen


PRODUCT_TABLE_SELECTORS = "section#product-grouped-info, #super-product-table, .grouped-items, "                           "#product-options-wrapper"


def visible_text(tree: LexborHTMLParser, max_chars: int = 6000, selector: str = "main") -> str:
    """Visible text of a region (default: main content): scripts/styles removed, whitespace collapsed."""
    root = tree.css_first(selector) if selector != "main" else (tree.css_first("main") or tree.body)
    if root is None:
        return ""
    clone = LexborHTMLParser(root.html or "")
    for n in clone.css("script, style, svg, template, noscript, form input"):
        n.decompose()
    text = clone.body.text(separator="\n") if clone.body else ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:max_chars]


def page_title(tree: LexborHTMLParser) -> str:
    t = tree.css_first("title")
    return t.text(strip=True) if t else ""
