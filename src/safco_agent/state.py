"""Per-URL workflow state carried through the LangGraph product pipeline."""
from __future__ import annotations

from typing import Any, TypedDict

from .models import ExtractionResult, FetchedPage, PageType, ValidationResult


class ProductState(TypedDict, total=False):
    url: str
    run_id: str
    fetch_mode: str                 # browser | http
    attempts: int                   # fetch attempts used (bounded by crawl.max_attempts)
    recoveries: int                 # recovery decisions taken (bounded)
    tried: list[str]                # recovery actions already applied
    page: FetchedPage | None
    page_type: PageType | None
    classified_by: str | None
    extraction: ExtractionResult | None
    validation: ValidationResult | None
    stage: str                      # where the last failure happened: fetch | classify | validate
    error: str | None
    error_transient: bool
    next_action: str | None
    outcome: str | None             # stored | dead_letter
    store_outcome: str | None       # inserted | updated | unchanged
    timings: dict[str, Any]
