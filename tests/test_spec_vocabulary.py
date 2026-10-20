"""Spec-name vocabulary: synonyms collapse to one name, the long tail is kept separately, nothing is invented."""
import pytest

from safco_agent.extractors.spec_vocabulary import (canonical_key, normalize_specifications, normalize_value,
                                                    vocabulary_prompt)
from safco_agent.models import Product


def _p(specs, prov=None):
    return Product(product_id="safco:1", canonical_url="https://www.safcodental.com/product/x", name="X",
                   specifications=specs, field_provenance=prov or {})


@pytest.mark.parametrize("raw,canon", [
    ("Gloves per Box", "Count Per Pack"), ("Quantity Per Box", "Count Per Pack"), ("Count Per Box", "Count Per Pack"),
    ("Boxes per Case", "Packs Per Case"), ("Material Type", "Material"), ("Colour", "Color"),
    ("Latex Status", "Latex Content"), ("Protein Content", "Latex Protein Level"), ("Cuff Type", "Cuff Style"),
    ("Thickness at fingertips", "Thickness At Fingertip"), ("Resorption Time", "Absorption Time"),
    ("Available in 3 Sizes", "Sizes Available"), ("Tonicity", None), ("DEHP Content", None),
])
def test_canonical_key(raw, canon):
    assert canonical_key(raw) == canon


@pytest.mark.parametrize("key,raw,expected", [
    ("Material", "nitrile", "Nitrile"),
    ("Powder Status", "powder free", "Powder-free"),
    ("Powder Status", "Non-powdered", "Powder-free"),
    ("Sterility", "non sterile", "Non-sterile"),
    ("Latex Content", "Not made with natural rubber latex", "Latex-free"),
    ("Cuff Style", "beaded cuff", "Beaded"),
    ("Cuff Style", "Extended cuff", "Extended"),
    ("Thickness", "at palm 2.0 mils; at fingertip 2.8 mils", "at palm 2.0 mils; at fingertip 2.8 mils"),
])
def test_normalize_value(key, raw, expected):
    assert normalize_value(key, raw) == expected


def test_normalize_specifications_maps_keeps_and_drops():
    p = _p({"Gloves per Box": "200", "material": "nitrile", "Tonicity": "Hypotonic",
            "Brand": "Halyard", "Product Name": "X gloves"},
           {"specifications.Gloves per Box": "llm", "specifications.Tonicity": "llm"})
    normalize_specifications(p)
    assert p.specifications == {"Count Per Pack": "200", "Material": "Nitrile"}
    assert p.extra_specifications == {"Tonicity": "Hypotonic"}          # long tail kept, not dropped
    assert p.field_provenance == {"specifications.Count Per Pack": "llm", "extra_specifications.Tonicity": "llm"}


def test_conflicting_synonyms_keep_both_facts():
    p = _p({"Cuff Style": "Beaded cuff", "Cuff Type": "Extended cuff"})
    normalize_specifications(p)
    assert p.specifications["Cuff Style"] == "Beaded"
    assert p.extra_specifications == {"Cuff Type": "Extended cuff"}


def test_normalization_is_idempotent():
    p = _p({"Gloves per Box": "200", "Tonicity": "Hypotonic"})
    normalize_specifications(p)
    first = (dict(p.specifications), dict(p.extra_specifications))
    normalize_specifications(p)
    assert (p.specifications, p.extra_specifications) == first


def test_prompt_lists_every_canonical_name():
    prompt = vocabulary_prompt()
    for name in ("Material", "Count Per Pack", "Powder Status", "Latex Content", "Intended Use"):
        assert f"- {name}:" in prompt
