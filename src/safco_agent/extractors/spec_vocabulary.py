"""Controlled vocabulary for product specifications.

Free-form spec names ("Gloves per Box", "Quantity Per Box", "Count Per Pack") are mapped onto one
canonical name so the catalog can be filtered and compared across products. Facts that fit no
canonical name are kept, under their own name, in `extra_specifications` rather than dropped.

Value normalization is limited to categorical keys (casing and a few unambiguous enums); measured
values such as thickness or length are kept exactly as written on the page.
"""
from __future__ import annotations

import re

from ..models import Product

# canonical name -> (description given to the LLM, aliases matched after normalization)
VOCABULARY: dict[str, tuple[str, tuple[str, ...]]] = {
    "Material": ("what the product is made of", (
        "material type", "material composition", "glove material", "blade material", "composition",
        "made of", "raw material")),
    "Color": ("color of the product", ("colour", "glove color", "shield color")),
    "Texture": ("surface texture or grip finish", (
        "finish", "texture finish", "surface texture", "fingertip finish", "shield texture", "grip")),
    "Powder Status": ("powdered or powder-free", ("powder", "powdered", "powder free")),
    "Latex Content": ("whether it contains natural rubber latex", (
        "latex", "latex status", "latex free", "natural rubber latex")),
    "Latex Protein Level": ("measured latex protein level", ("latex protein content", "protein content",
                                                           "protein level")),
    "Sterility": ("sterile or non-sterile", ("sterile", "sterile status", "sterilization")),
    "Cuff Style": ("glove cuff style", ("cuff type", "cuff")),
    "Thickness": ("overall thickness", ("gauge thickness", "wall thickness")),
    "Thickness At Palm": ("thickness measured at the palm", ("palm thickness",)),
    "Thickness At Fingertip": ("thickness measured at the fingertip", (
        "thickness at fingertips", "fingertip thickness")),
    "Length": ("overall length", ("glove length", "overall length")),
    "Fit": ("hand fit", ("handedness", "hand")),
    "Count Per Pack": ("number of items in one sellable package (box, bag, pack)", (
        "count per box", "gloves per box", "quantity per box", "count per bag", "units per box",
        "pieces per box", "items per box", "quantity", "count")),
    "Packs Per Case": ("number of packages in a case", ("boxes per case", "count per case", "case pack",
                                                       "case quantity")),
    "Packaging": ("packaging or dispenser format", ("packaging type", "dispenser type", "dispensing",
                                                    "package", "package type")),
    "Coating": ("coating, lining or skin-care additive", (
        "inner coating", "lining type", "lining", "skin care ingredient", "special additives", "additives")),
    "Product Type": ("what kind of product it is", ("type", "glove type", "suture type", "category")),
    "Sizes Available": ("sizes offered", ("available sizes", "sizes", "size range")),
    "Standards": ("standards, certifications or test methods", (
        "standard", "standards compliance", "testing standard", "sterilization standard",
        "chemotherapy approval", "certification", "certifications", "compliance")),
    "AQL": ("acceptable quality level", ("acceptable quality level", "aql rating")),
    "Absorbability": ("absorbable / resorbable or not", ("resorbability", "absorbable", "resorbable")),
    "Absorption Time": ("time to absorb or resorb", (
        "resorption time", "absorbability timeframe", "absorption timeframe", "resorption timeframe")),
    "Absorption Capacity": ("how much fluid it absorbs", ("absorbency",)),
    "Form": ("physical form or construction", ("structure", "construction", "physical form")),
    "Particle Size": ("graft particle size", ("granule size",)),
    "Intended Use": ("indications or intended use", (
        "use case", "indications", "indications include", "intended area", "industries", "application",
        "uses")),
}

# Keys that duplicate top-level product fields; never stored as specifications.
DROP_KEYS = {"brand", "product name", "name", "manufacturer", "sku", "price", "item number"}

_SIZE_WORD = re.compile(r"^available in \d+ sizes?$")

_ALIAS_INDEX: dict[str, str] = {}
for canon, (_, aliases) in VOCABULARY.items():
    for a in (canon, *aliases):
        _ALIAS_INDEX[re.sub(r"[^a-z0-9]+", " ", a.lower()).strip()] = canon


def _key_norm(name: str) -> str:
    n = name.lower().replace("colour", "color")
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def canonical_key(name: str) -> str | None:
    """Canonical spec name, or None when the name fits no vocabulary entry."""
    k = _key_norm(name)
    if k in _ALIAS_INDEX:
        return _ALIAS_INDEX[k]
    if _SIZE_WORD.match(k):
        return "Sizes Available"
    return None


def is_dropped(name: str) -> bool:
    return _key_norm(name) in DROP_KEYS


def _cap(v: str) -> str:
    return v[:1].upper() + v[1:] if v else v


def normalize_value(key: str, value: str) -> str:
    """Categorical keys get consistent casing/enums; everything else is kept as written."""
    v = " ".join(value.split())
    low = v.lower()
    if key == "Powder Status":
        if re.search(r"powder[\s-]*free|non[\s-]*powdered", low):
            return "Powder-free"
        if "powder" in low:
            return "Powdered"
    if key == "Sterility":
        if re.search(r"non[\s-]*sterile|not sterile", low):
            return "Non-sterile"
        if "sterile" in low:
            return "Sterile"
    if key == "Latex Content":
        if re.search(r"latex[\s-]*free|not made with|no latex|without latex|non[\s-]*latex", low):
            return "Latex-free"
        if low in ("latex", "natural rubber latex", "contains latex", "contains natural rubber latex"):
            return "Contains latex"
    if key == "Cuff Style":
        # the key already says "cuff": "Beaded cuff" and "beaded" are the same value
        v = re.sub(r"\s+cuffs?$", "", v, flags=re.I) or v
    if key in ("Material", "Color", "Texture", "Cuff Style", "Fit", "Product Type", "Form", "Absorbability",
               "Coating", "Packaging"):
        return _cap(v)
    return v


def vocabulary_prompt() -> str:
    return "\n".join(f"- {k}: {desc}" for k, (desc, _) in VOCABULARY.items())


def normalize_specifications(product: Product) -> Product:
    """Map spec names to the vocabulary; move the long tail to extra_specifications. Idempotent."""
    prov = product.field_provenance
    specs: dict[str, str] = {}
    extras: dict[str, str] = dict(product.extra_specifications)
    for raw_key, raw_val in list(product.specifications.items()):
        if not raw_key or not raw_val or is_dropped(raw_key):
            prov.pop(f"specifications.{raw_key}", None)
            continue
        source = prov.pop(f"specifications.{raw_key}", None)
        canon = canonical_key(raw_key)
        if canon is None:
            if raw_key not in extras:
                extras[raw_key] = " ".join(raw_val.split())
                if source:
                    prov[f"extra_specifications.{raw_key}"] = source
            continue
        value = normalize_value(canon, raw_val)
        if canon not in specs:
            specs[canon] = value
            if source:
                prov[f"specifications.{canon}"] = source
        elif specs[canon] != value and raw_key not in extras:
            # Two different facts mapped to one name (e.g. "Cuff Style" and "Cuff Type"): keep both.
            extras[raw_key] = " ".join(raw_val.split())
            if source:
                prov[f"extra_specifications.{raw_key}"] = source
    product.specifications = specs
    product.extra_specifications = {k: v for k, v in extras.items() if not is_dropped(k)}
    return product
