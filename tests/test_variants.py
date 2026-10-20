"""Variant (SKU) parsing and value normalization."""
from decimal import Decimal

import pytest

from safco_agent.extractors.variants import (extract_master_data, normalize_availability, parse_option_attributes,
                                             parse_pack, parse_price, strip_html, variants_from_master_data)
from safco_agent.models import Availability


def test_master_data_variants(lavender_html):
    md = extract_master_data(lavender_html)
    vs = variants_from_master_data(md)
    assert [v.sku for v in vs] == ["3020800", "3020810", "3020815", "3020822", "3020830"]
    first = vs[0]
    assert first.name == "Lavender Nitrile gloves x-small 250/box"
    assert first.price == Decimal("38.99")
    assert first.manufacturer_code == "52816"
    assert first.availability == Availability.IN_STOCK and first.availability_label == "In stock"
    assert first.attributes == {"size": "x-small", "pack": "250/box"}
    assert (first.pack_size, first.unit_of_measure) == (250, "box")
    assert vs[3].attributes["size"] == "large"


def test_missing_master_data_returns_none():
    assert extract_master_data("<html>no data</html>") is None
    assert extract_master_data('<script>window.masterData = "not json</script>') is None


@pytest.mark.parametrize("raw,expected", [
    ("38.990000", Decimal("38.99")), ("$1,234.5", Decimal("1234.50")), (12, Decimal("12.00")),
    ("0", None), ("", None), (None, None), ("Call for price", None),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize("text,expected", [
    ("X-small, 250/box", (250, "box")),
    ("Box of 12", (12, "box")),
    ("1/2\" x 2\" sterile packets, 12/Box", (12, "box")),
    ("100/pk", (100, "pack")),
    ("250 gloves per box", (250, "box")),
    ("3-0, 18\" black silk", (None, None)),
])
def test_parse_pack(text, expected):
    assert parse_pack(text) == expected


@pytest.mark.parametrize("raw,expected", [
    ("https://schema.org/InStock", Availability.IN_STOCK), ("in_stock", Availability.IN_STOCK),
    ("Out of stock", Availability.OUT_OF_STOCK), ("BackOrder", Availability.BACKORDER),
    ("Special order", Availability.SPECIAL_ORDER), ("Direct from manufacturer", Availability.DROP_SHIP),
    ("Non-purchasable", Availability.NOT_PURCHASABLE),
    (None, Availability.UNKNOWN), ("weird", Availability.UNKNOWN),
])
def test_normalize_availability(raw, expected):
    assert normalize_availability(raw) == expected


def test_option_attributes_only_literal_values():
    assert parse_option_attributes("Medium, 100/box") == {"size": "medium", "pack": "100/box"}
    assert parse_option_attributes("Sterile, 4-0 PGA") == {}  # nothing inferred


def test_strip_html():
    assert strip_html('<div data-x="1">1/2&quot; x 2&quot; packets</div>') == '1/2" x 2" packets'


@pytest.mark.parametrize("text,expected", [
    ("5-pack", (5, "pack")), ("OraSoothe gel 25-pack", (25, "pack")), ("0.5cc jar", (None, None)),
])
def test_parse_pack_dash_and_no_inference(text, expected):
    assert parse_pack(text) == expected


def test_volume_dimensions_and_container():
    from safco_agent.extractors.variants import parse_container
    assert parse_option_attributes("0.5cc jar") == {"volume": "0.5cc"}
    assert parse_option_attributes("15 x 20mm") == {"dimensions": "15 x 20mm"}
    assert parse_option_attributes("Oral Coating Rinse - Hygiene, 3.4 oz Bottle")["volume"] == "3.4oz"
    assert parse_container("0.5cc jar") == "jar"
    assert parse_container("15 x 20mm") is None
