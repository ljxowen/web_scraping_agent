"""LangGraph StateGraph for processing one product URL with explicit conditional routing.

    START -> fetch -> classify -> extract -> validate --valid--> enrich -> store -> END
               ^          |                      |
               |          +--not product--> dead_letter -> END
               |                                 +--recoverable--> recover
               +------------ rerender / retry_http ------------------+
                                                 llm_extract <-------+ -> validate
                                                 give_up ------------+ -> degrade (identity ok) -> enrich
                                                                         -> dead_letter (otherwise)

Every loop is bounded (fetch attempts and recovery count), so each URL always reaches END.
"""
from __future__ import annotations

import time

from langgraph.graph import END, START, StateGraph
from selectolax.lexbor import LexborHTMLParser

from .agents.classifier import PageClassifier
from .agents.extractor import ExtractorAgent
from .agents.recovery import RecoveryAction, RecoveryAgent, RecoveryContext
from .agents.validator import ValidatorAgent
from .infra.browser import BrowserPool
from .infra.http import FetchError, HttpFetcher
from .infra.logging import get_logger
from .infra.storage import Storage
from .models import PageType
from .state import ProductState

log = get_logger("graph")

MAX_RECOVERIES = 4


class ProductPipeline:
    def __init__(self, *, fetcher: HttpFetcher, browser: BrowserPool | None, classifier: PageClassifier,
                 extractor: ExtractorAgent, validator: ValidatorAgent, recovery: RecoveryAgent,
                 storage: Storage, max_attempts: int, llm_available: bool):
        self.fetcher, self.browser = fetcher, browser
        self.classifier, self.extractor, self.validator, self.recovery = classifier, extractor, validator, recovery
        self.storage, self.max_attempts, self.llm_available = storage, max_attempts, llm_available
        self.graph = self._build()

    # ------------------------------------------------------------------ nodes
    async def fetch(self, s: ProductState) -> ProductState:
        t0 = time.monotonic()
        attempts = s.get("attempts", 0) + 1
        mode = s.get("fetch_mode", "browser")
        try:
            if mode == "browser" and self.browser is not None:
                page = await self.browser.render_product(s["url"])
            else:
                page = await self.fetcher.fetch(s["url"])
            out = {"page": page, "attempts": attempts, "error": None, "stage": "fetch"}
        except FetchError as e:
            log.warning("fetch_failed", url=s["url"], mode=mode, attempt=attempts, error=str(e), transient=e.transient)
            out = {"page": None, "attempts": attempts, "error": str(e), "error_transient": e.transient,
                   "stage": "fetch"}
        self.storage.mark_job(s["url"], "in_progress", out.get("error"), bump_attempt=True)
        return {**out, "timings": {**s.get("timings", {}), "fetch_s": round(time.monotonic() - t0, 2)}}

    async def classify(self, s: ProductState) -> ProductState:
        tree = LexborHTMLParser(s["page"].html)
        pt, how = await self.classifier.classify(s["page"], tree)
        return {"page_type": pt, "classified_by": how,
                "stage": "classify", "error": None if pt == PageType.PRODUCT else f"page classified as {pt.value}"}

    async def extract(self, s: ProductState) -> ProductState:
        t0 = time.monotonic()
        result = self.extractor.extract(s["page"])
        return {"extraction": result,
                "timings": {**s.get("timings", {}), "extract_s": round(time.monotonic() - t0, 3)}}

    async def validate(self, s: ProductState) -> ProductState:
        v = self.validator.validate(s["extraction"])
        log.info("validated", url=s["url"], status=v.status, issues=v.issues[:6],
                 method=v.product.extraction_method if v.product else None)
        return {"validation": v, "stage": "validate",
                "error": None if v.status in ("valid", "valid_with_warnings") else "; ".join(v.issues)[:500]}

    async def recover(self, s: ProductState) -> ProductState:
        ex = s.get("extraction")
        ctx = RecoveryContext(
            url=s["url"], stage=s.get("stage", "fetch"), error=s.get("error"),
            transient=s.get("error_transient", True),
            missing=ex.missing_critical if ex else [], visible_missing=ex.visible_but_unparsed if ex else [],
            attempts=s.get("attempts", 0) if s.get("stage") == "fetch" else s.get("recoveries", 0),
            max_attempts=self.max_attempts, tried=list(s.get("tried", [])), llm_available=self.llm_available,
        )
        if s.get("recoveries", 0) >= MAX_RECOVERIES:
            action = RecoveryAction.GIVE_UP
        else:
            action = await self.recovery.decide(ctx)
        upd: ProductState = {"next_action": action.value, "recoveries": s.get("recoveries", 0) + 1,
                             "tried": [*s.get("tried", []), action.value]}
        if action == RecoveryAction.RERENDER:
            upd["fetch_mode"] = "browser" if self.browser is not None else "http"
        elif action == RecoveryAction.RETRY_DETERMINISTIC:
            upd["fetch_mode"] = "http"
        return upd

    async def llm_extract(self, s: ProductState) -> ProductState:
        result = await self.extractor.llm_fill_missing(s["page"], s["extraction"])
        return {"extraction": result}

    async def enrich(self, s: ProductState) -> ProductState:
        p = s["validation"].product
        await self.extractor.llm_structure_specs(p)
        return {}

    async def store(self, s: ProductState) -> ProductState:
        p = s["validation"].product
        outcome = self.storage.upsert_product(p, s["run_id"])
        # Category membership: listing observations + breadcrumb trail
        for hit in self.storage.listing_categories_for(p.canonical_url):
            self.storage.link_product_category(p.product_id, hit["category_id"], f"listing:{hit['source']}")
        for name in p.category_path:
            for c in self.storage.category_by_name(name):
                self.storage.link_product_category(p.product_id, c["category_id"], "breadcrumb")
        self.storage.mark_job(s["url"], "done")
        self.storage.clear_dead_letter(s["url"])
        log.info("stored", url=s["url"], product_id=p.product_id, outcome=outcome, variants=len(p.variants),
                 status=p.validation_status, method=p.extraction_method, timings=s.get("timings"))
        return {"outcome": "stored", "store_outcome": outcome}

    async def dead_letter(self, s: ProductState) -> ProductState:
        stage = s.get("stage", "unknown")
        etype = "fetch_error" if stage == "fetch" else (
            "not_a_product" if stage == "classify" else "validation_failed")
        self.storage.dead_letter(s["url"], stage, etype, s.get("error") or "", s.get("attempts", 0), s["run_id"])
        self.storage.mark_job(s["url"], "failed", s.get("error"))
        log.warning("dead_lettered", url=s["url"], stage=stage, error=(s.get("error") or "")[:300])
        return {"outcome": "dead_letter"}

    # ---------------------------------------------------------------- routing
    @staticmethod
    def after_fetch(s: ProductState) -> str:
        return "recover" if s.get("page") is None else "classify"

    @staticmethod
    def after_classify(s: ProductState) -> str:
        return "extract" if s.get("page_type") == PageType.PRODUCT else "dead_letter"

    @staticmethod
    def after_validate(s: ProductState) -> str:
        status = s["validation"].status
        if status in ("valid", "valid_with_warnings"):
            return "enrich"
        return "recover" if status == "recoverable" else "dead_letter"

    @staticmethod
    def after_recover(s: ProductState) -> str:
        nxt = {
            RecoveryAction.RERENDER.value: "fetch",
            RecoveryAction.RETRY_DETERMINISTIC.value: "fetch",
            RecoveryAction.LLM_EXTRACT.value: "llm_extract",
        }.get(s.get("next_action"))
        if nxt:
            return nxt
        # Recovery exhausted. A record with intact identity (name + SKU, no hard errors) is still
        # useful: keep it, flagged, rather than discarding everything we did extract.
        v = s.get("validation")
        if s.get("stage") == "validate" and v and v.product and v.product.name and                 (v.product.parent_sku or v.product.variants) and not any("out_of_range" in i or "min_gt" in i
                                                                       for i in v.issues):
            return "degrade"
        return "dead_letter"

    async def degrade(self, s: ProductState) -> ProductState:
        v = s["validation"]
        v.product.validation_status = "valid_with_warnings"
        v.product.validation_issues = [*v.issues, "unresolved_after_recovery"]
        self.validator.stats["degraded"] = self.validator.stats.get("degraded", 0) + 1
        log.info("degraded_to_warnings", url=s["url"], issues=v.product.validation_issues)
        return {"validation": v}

    def _build(self):
        g = StateGraph(ProductState)
        for name in ("fetch", "classify", "extract", "validate", "recover", "llm_extract", "degrade", "enrich",
                     "store", "dead_letter"):
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "fetch")
        g.add_conditional_edges("fetch", self.after_fetch, {"recover": "recover", "classify": "classify"})
        g.add_conditional_edges("classify", self.after_classify, {"extract": "extract", "dead_letter": "dead_letter"})
        g.add_edge("extract", "validate")
        g.add_conditional_edges("validate", self.after_validate,
                                {"enrich": "enrich", "recover": "recover", "dead_letter": "dead_letter"})
        g.add_conditional_edges("recover", self.after_recover,
                                {"fetch": "fetch", "llm_extract": "llm_extract", "degrade": "degrade",
                                 "dead_letter": "dead_letter"})
        g.add_edge("degrade", "enrich")
        g.add_edge("llm_extract", "validate")
        g.add_edge("enrich", "store")
        g.add_edge("store", END)
        g.add_edge("dead_letter", END)
        return g.compile()

    async def run(self, url: str, run_id: str, fetch_mode: str) -> ProductState:
        return await self.graph.ainvoke(
            {"url": url, "run_id": run_id, "fetch_mode": fetch_mode, "attempts": 0, "recoveries": 0, "tried": [],
             "timings": {}},
            {"recursion_limit": 40},
        )
