"""Deterministic extraction from real saved Safco pages (JSON-LD, DOM, listing data, classifier)."""
import json
from decimal import Decimal

from selectolax.lexbor import LexborHTMLParser

from conftest import FIX, read
from safco_agent.agents.classifier import PageClassifier
from safco_agent.agents.extractor import ExtractorAgent
from safco_agent.agents.navigator import NavigatorAgent, canonical_url, static_listing_hits
from safco_agent.extractors.jsonld import breadcrumb_names, parse_jsonld, product_node
from safco_agent.extractors.magento_dom import canonical_image, specs_from_lines
from safco_agent.infra.browser import parse_algolia_listing
from safco_agent.models import Availability, Category, FetchedPage, PageType, PriceType


def test_jsonld_product_and_breadcrumbs(lavender_html):
    ld = parse_jsonld(LexborHTMLParser(lavender_html))
    p = product_node(ld)
    assert p["name"] == "Lavender Nitrile"
    assert p["sku"] == "DRCAS"
    assert [n for n, _ in breadcrumb_names(ld)] == [
        "Dental Supplies", "Dental Exam Gloves", "Nitrile gloves", "Lavender Nitrile"]


def test_extract_grouped_product(lavender_page):
    r = ExtractorAgent().extract(lavender_page)
    p = r.product
    assert r.missing_critical == []
    assert p.product_id == "safco:95833"
    assert p.brand == "Halyard" and p.manufacturer == "Halyard"
    assert p.product_type == "grouped"
    assert p.category_path == ["Dental Supplies", "Dental Exam Gloves", "Nitrile gloves"]
    assert p.description.startswith("Powder-free nitrile exam gloves.")
    assert "Textured fingertips" in p.description.split("\n")
    assert p.specifications["Thickness"] == "at palm 2.0 mils; at fingertip 2.8 mils"
    assert p.image_urls == ["https://www.safcodental.com/media/catalog/product/d/r/drcas.jpg"]
    assert p.availability == Availability.IN_STOCK
    assert p.rating_value == 4.71 and p.review_count == 7
    assert len(p.variants) == 5
    assert p.price_min == p.price_max == Decimal("38.99")
    assert p.price_type == PriceType.RANGE
    assert p.field_provenance["name"] == "jsonld" and p.field_provenance["variants"] == "master_data"


def test_alternatives_are_category_filtered(lavender_page):
    p = ExtractorAgent().extract(lavender_page).product
    names = [a.name for a in p.alternative_products]
    assert "Safco Nitrilex Prevail Gloves" in names
    assert all(a.relation == "Safco Brand Recommendation" for a in p.alternative_products)  # no "Trending"
    assert "Purple Nitrile" not in names  # trending-only item


def test_single_variant_grouped_product_has_exact_price(surgicel_page):
    p = ExtractorAgent().extract(surgicel_page).product
    assert p.category_path[-1] == "Surgical medicaments and packing"
    assert len(p.variants) == 1
    v = p.variants[0]
    assert v.sku == "2580213" and v.manufacturer_code == "1955S"
    assert v.option_text == '1/2" x 2" sterile packets, 12/box'  # Page Builder HTML stripped
    assert (v.pack_size, v.unit_of_measure) == (12, "box")
    assert p.price_type == PriceType.EXACT


def test_specs_from_lines_skips_notes():
    specs = specs_from_lines(["Thickness: 3 mil", "Please note: the name changed", "Plain line"])
    assert specs == {"Thickness": "3 mil"}


def test_canonical_image_drops_resize_params():
    u = r"https:\/\/www.safcodental.com\/media\/catalog\/product\/d\/r\/drcas.jpg?optimize=medium&amp;height=700"
    assert canonical_image(u) == "https://www.safcodental.com/media/catalog/product/d/r/drcas.jpg"


def test_category_discovery_from_jsonld():
    root = Category(category_id="catalog/gloves", name="Dental Exam Gloves",
                    canonical_url="https://www.safcodental.com/catalog/gloves")
    subs = NavigatorAgent(None, "https://www.safcodental.com").discover_subcategories(
        root, read("category_gloves.html"), "catalog/gloves")
    assert [s.name for s in subs] == ["Chloroprene gloves", "Glove Holder", "Latex gloves", "Nitrile gloves",
                                     "Surgical gloves", "Vinyl gloves"]
    assert all(s.parent_category_id == "catalog/gloves" and s.depth == 1 for s in subs)


def test_static_listing_fallback():
    hits = static_listing_hits(read("category_gloves.html"), "https://www.safcodental.com/catalog/gloves")
    assert len(hits) == 12 and hits[0].url == "https://www.safcodental.com/product/lavender-nitrile"


def test_algolia_listing_response_parsing():
    fx = json.loads((FIX / "algolia_listing_sutures_p0.json").read_text(encoding="utf-8"))
    res = parse_algolia_listing(json.dumps(fx["request"]), json.dumps(fx["response"]))
    assert res["nbHits"] == 55 and res["nbPages"] == 5 and res["page"] == 0
    assert len(res["hits"]) == 12
    assert "Sutures & surgical products" in res["_facetFilters"]


def test_canonical_url():
    assert canonical_url("/catalog/gloves/?page=2#x") == "https://www.safcodental.com/catalog/gloves"
    assert canonical_url("HTTPS://WWW.SAFCODENTAL.COM/product/a//b/") == "https://www.safcodental.com/product/a/b"


def test_classifier_rules(lavender_page):
    c = PageClassifier()
    tree = LexborHTMLParser(lavender_page.html)
    assert c.classify_rules(lavender_page, tree) == PageType.PRODUCT
    cat = FetchedPage(url="https://www.safcodental.com/catalog/gloves", html=read("category_gloves.html"))
    assert c.classify_rules(cat, LexborHTMLParser(cat.html)) == PageType.CATEGORY
    err = FetchedPage(url="https://www.safcodental.com/product/x", html="<title>404 Not Found</title>", status=404)
    assert c.classify_rules(err, LexborHTMLParser(err.html)) == PageType.ERROR
