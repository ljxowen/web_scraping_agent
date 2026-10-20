"""Validator agent: schema + business-rule checks and normalization.

Outcomes:
  valid               - all critical fields present and consistent
  valid_with_warnings - stored, but with quality issues (e.g. price legitimately not shown)
  recoverable         - critical data missing although the page visibly contains it -> Recovery
  fatal               - not a usable product record -> dead letter
"""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlsplit

from ..models import ExtractionResult, Product, ValidationResult

MAX_REASONABLE_PRICE = Decimal("100000")


class ValidatorAgent:
    def __init__(self, allowed_host: str = "www.safcodental.com"):
        self.allowed_host = allowed_host
        self.stats = {"valid": 0, "valid_with_warnings": 0, "recoverable": 0, "fatal": 0}

    def normalize(self, p: Product) -> Product:
        p.name = " ".join(p.name.split()) if p.name else None
        p.brand = p.brand.strip() if p.brand else None
        p.image_urls = list(dict.fromkeys(p.image_urls))
        p.category_path = [c.strip() for c in p.category_path if c and c.strip()]
        p.specifications = {k.strip(): " ".join(v.split()) for k, v in p.specifications.items() if k and v}
        # de-duplicate variants by SKU, keeping first occurrence
        seen, variants = set(), []
        for v in p.variants:
            v.sku = v.sku.strip()
            if v.sku and v.sku not in seen:
                seen.add(v.sku)
                variants.append(v)
        p.variants = variants
        return p

    def validate(self, result: ExtractionResult) -> ValidationResult:
        p = result.product
        if p is None:
            self.stats["fatal"] += 1
            return ValidationResult(status="fatal", issues=["no_product_extracted"])
        p = self.normalize(p)
        issues: list[str] = []
        errors: list[str] = []

        if urlsplit(p.canonical_url).netloc != self.allowed_host:
            errors.append("url_off_domain")
        for f in result.missing_critical:
            issues.append(f"missing_{f}")
        for v in p.variants:
            if v.price is not None and not (Decimal("0") < v.price < MAX_REASONABLE_PRICE):
                errors.append(f"variant_price_out_of_range:{v.sku}")
            if v.price is None:
                issues.append(f"variant_price_missing:{v.sku}")
        if p.price_min is not None and p.price_max is not None and p.price_min > p.price_max:
            errors.append("price_min_gt_max")
        if not p.category_path:
            issues.append("missing_category_path")
        if not p.description:
            issues.append("missing_description")
        if not p.image_urls:
            issues.append("missing_images")

        recoverable = [f for f in result.missing_critical if f in result.visible_but_unparsed]
        if errors:
            status = "fatal" if "url_off_domain" in errors else "recoverable"
            issues = errors + issues
        elif recoverable:
            status = "recoverable"
        elif "name" in result.missing_critical or ("sku" in result.missing_critical and not p.variants):
            # identity fields missing and not visible: not a usable record
            status = "fatal"
        elif issues:
            status = "valid_with_warnings"
        else:
            status = "valid"
        p.validation_status = status if status in ("valid", "valid_with_warnings") else "invalid"
        p.validation_issues = issues
        self.stats[status] += 1
        return ValidationResult(status=status, issues=issues, product=p)
