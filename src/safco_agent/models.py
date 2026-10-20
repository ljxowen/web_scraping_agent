"""Domain models. Product = a product family (one page); Variant = a purchasable SKU."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_serializer


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PageType(str, Enum):
    CATEGORY = "category"
    PRODUCT = "product"
    ERROR = "error"
    OTHER = "other"


class Availability(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    BACKORDER = "backorder"
    SPECIAL_ORDER = "special_order"          # orderable, sourced on demand
    DROP_SHIP = "drop_ship"                  # "Direct from manufacturer"
    NOT_PURCHASABLE = "not_purchasable"      # listed but not sellable online
    DISCONTINUED = "discontinued"
    UNKNOWN = "unknown"


class PriceType(str, Enum):
    EXACT = "exact"          # single purchasable SKU price
    RANGE = "range"          # min/max derived from variant prices
    FROM = "from"            # advertised "From $X" with no per-variant evidence


class Category(BaseModel):
    category_id: str
    name: str
    canonical_url: str
    parent_category_id: str | None = None
    root_category_id: str | None = None
    depth: int = 0
    discovered_at: str = Field(default_factory=utcnow)


class ListingHit(BaseModel):
    """A product reference discovered on a category listing (from Algolia or DOM)."""
    url: str
    source_product_id: str | None = None
    name: str | None = None
    type_id: str | None = None
    category_url: str
    source: str = "algolia"  # algolia | dom | sitemap
    raw: dict[str, Any] = Field(default_factory=dict)


class AlternativeProduct(BaseModel):
    relation: str               # e.g. "Safco Brand Recommendation", "Related products"
    name: str | None = None
    sku: str | None = None
    url: str | None = None


class Variant(BaseModel):
    sku: str
    name: str | None = None
    manufacturer_code: str | None = None
    option_text: str | None = None          # raw option label e.g. "X-small, 250/box"
    attributes: dict[str, str] = Field(default_factory=dict)
    price: Decimal | None = None
    currency: str | None = "USD"
    availability: Availability = Availability.UNKNOWN
    availability_label: str | None = None
    pack_size: int | None = None
    unit_of_measure: str | None = None
    source_variant_id: str | None = None

    @field_serializer("price")
    def _ser_price(self, v: Decimal | None):
        return None if v is None else str(v)


class Product(BaseModel):
    product_id: str                          # stable: "safco:<magento entity id>" or url hash
    source_product_id: str | None = None
    canonical_url: str
    name: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    parent_sku: str | None = None
    product_type: str | None = None          # grouped | configurable | simple
    category_path: list[str] = Field(default_factory=list)
    description: str | None = None
    specifications: dict[str, str] = Field(default_factory=dict)        # canonical vocabulary names
    extra_specifications: dict[str, str] = Field(default_factory=dict)  # facts outside the vocabulary
    image_urls: list[str] = Field(default_factory=list)
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    price_type: PriceType | None = None
    currency: str | None = None
    availability: Availability = Availability.UNKNOWN
    rating_value: float | None = None
    review_count: int | None = None
    alternative_products: list[AlternativeProduct] = Field(default_factory=list)
    variants: list[Variant] = Field(default_factory=list)
    extraction_method: str = "deterministic"   # deterministic | deterministic+llm | llm
    field_provenance: dict[str, str] = Field(default_factory=dict)  # field -> jsonld|master_data|dom|llm|recs
    validation_status: str = "pending"          # valid | valid_with_warnings | invalid
    validation_issues: list[str] = Field(default_factory=list)
    scraped_at: str = Field(default_factory=utcnow)

    @field_serializer("price_min", "price_max")
    def _ser_price(self, v: Decimal | None):
        return None if v is None else str(v)

    def content_hash(self) -> str:
        """Hash of normalized business fields only (no timestamps/provenance) for change detection."""
        payload = self.model_dump(
            mode="json",
            exclude={"scraped_at", "field_provenance", "validation_status", "validation_issues",
                     "extraction_method"},
        )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class FetchedPage(BaseModel):
    url: str
    final_url: str | None = None
    status: int | None = None
    html: str = ""
    via: str = "http"  # http | browser
    captured: dict[str, Any] = Field(default_factory=dict)  # e.g. {"recommendations": {...}}


class ExtractionResult(BaseModel):
    product: Product | None = None
    missing_critical: list[str] = Field(default_factory=list)
    visible_but_unparsed: list[str] = Field(default_factory=list)  # fields evidently on page
    notes: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    status: str                    # valid | valid_with_warnings | recoverable | fatal
    issues: list[str] = Field(default_factory=list)
    product: Product | None = None
