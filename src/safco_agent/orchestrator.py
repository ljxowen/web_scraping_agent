"""Run orchestration: category discovery -> listing traversal -> product pipeline -> export.

State lives in SQLite (crawl_jobs frontier), so an interrupted run can be resumed: completed URLs are
skipped, in-progress ones are re-queued, and all writes are idempotent upserts.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

import structlog

from .agents.classifier import PageClassifier
from .agents.extractor import ExtractorAgent
from .agents.navigator import NavigatorAgent, canonical_url, category_id_for
from .agents.recovery import RecoveryAgent
from .agents.validator import ValidatorAgent
from .config import AppConfig
from .export.exporters import export_all
from .graph import ProductPipeline
from .infra.browser import BrowserPool
from .infra.http import FetchError, HttpFetcher
from .infra.llm import LLMClient
from .infra.logging import get_logger
from .infra.rate_limit import TokenBucket
from .infra.storage import Storage
from .models import Category

log = get_logger("orchestrator")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


class Orchestrator:
    def __init__(self, cfg: AppConfig, *, resume: bool = False, retry_failed: bool = False,
                 max_products_per_category: int | None = None, llm_enabled: bool | None = None,
                 export: bool = True):
        self.cfg = cfg
        self.resume, self.retry_failed, self.export = resume, retry_failed, export
        self.limit = max_products_per_category if max_products_per_category is not None \
            else cfg.crawl.max_products_per_category
        if llm_enabled is not None:
            cfg.llm.enabled = llm_enabled
        self.run_id = new_run_id()
        self.storage = Storage(cfg.storage.db_path)
        self.limiter = TokenBucket(cfg.rate_limit.requests_per_second, cfg.rate_limit.burst)
        self.fetcher = HttpFetcher(cfg, self.limiter)
        self.browser: BrowserPool | None = None
        self.llm = LLMClient(cfg.llm, self.storage, self.run_id)
        self.counters = {"categories_discovered": 0, "listing_pages": 0, "product_urls_discovered": 0,
                         "duplicate_urls_skipped": 0, "products_processed": 0, "products_stored": 0,
                         "products_dead_lettered": 0, "inserted": 0, "updated": 0, "unchanged": 0}
        self.latencies: list[float] = []

    # ------------------------------------------------------------------ main
    async def run(self) -> dict:
        structlog.contextvars.bind_contextvars(run_id=self.run_id)
        mode = "resume" if self.resume else ("sample" if self.limit else "full")
        self.storage.start_run(self.run_id, self.cfg.redacted(), mode)
        log.info("run_started", mode=mode, limit=self.limit, llm=self.llm.available,
                 fetch_mode=self.cfg.crawl.product_fetch_mode)
        t0 = time.monotonic()
        status = "completed"
        try:
            await self.fetcher.start()
            self.browser = BrowserPool(self.cfg, self.limiter, self.fetcher.robots)
            await self.browser.start()
            # A run still marked 'running' was killed without cleanup (single-process POC assumption).
            self.storage.conn.execute(
                "UPDATE crawl_runs SET status='crashed' WHERE status='running' AND run_id<>?", (self.run_id,))
            if self.resume:
                n =self.storage.recover_in_progress()
                log.info("resume_recovered_in_progress", jobs=n)
                if self.retry_failed:
                    self.storage.conn.execute(
                        "UPDATE crawl_jobs SET status='pending', attempts=0 WHERE status='failed'")
            else:
                self.storage.reset_for_new_run("category", self.run_id)
                self.storage.park_jobs("product")
            await self.discover_categories()
            await self.traverse_listings()
            await self.process_products()
        except (KeyboardInterrupt, asyncio.CancelledError):
            status = "interrupted"
            log.warning("run_interrupted")
            raise
        except Exception as e:
            status = "failed"
            log.exception("run_failed", error=str(e))
            raise
        finally:
            elapsed = round(time.monotonic() - t0, 1)
            metrics = self.metrics(elapsed)
            self.storage.finish_run(self.run_id, status, metrics)
            if self.browser:
                await self.browser.close()
            await self.fetcher.close()
            if self.export and status == "completed":
                export_all(self.storage, self.cfg.storage.output_dir, metrics)
            log.info("run_finished", status=status, elapsed_s=elapsed)
            self.storage.close()
        return metrics

    # ------------------------------------------------------------- discovery
    async def discover_categories(self) -> None:
        nav = NavigatorAgent(self.browser, self.cfg.site.base_url)
        for seed in self.cfg.seeds:
            url = canonical_url(seed.url)
            root_id = category_id_for(url)
            try:
                page = await self.fetcher.fetch(url)
            except FetchError as e:
                log.error("seed_fetch_failed", url=url, error=str(e))
                self.storage.dead_letter(url, "discovery", "fetch_error", str(e), 1, self.run_id)
                continue
            root = Category(category_id=root_id, name=nav.category_name_from_html(page.html, seed.name),
                            canonical_url=url, root_category_id=root_id, depth=0)
            self.storage.upsert_category(root)
            self.storage.enqueue(url, "category", self.run_id, root_id, {"depth": 0})
            self.counters["categories_discovered"] += 1
            if not self.cfg.crawl.traverse_subcategories:
                continue
            subs = nav.discover_subcategories(root, page.html, root_id)
            for sub in subs:
                self.storage.upsert_category(sub)
                self.storage.enqueue(sub.canonical_url, "category", self.run_id, sub.category_id,
                                     {"depth": sub.depth})
            self.counters["categories_discovered"] += len(subs)
            log.info("categories_discovered", root=root.name, subcategories=[s.name for s in subs])

    # -------------------------------------------------------------- listings
    async def traverse_listings(self) -> None:
        nav = NavigatorAgent(self.browser, self.cfg.site.base_url)
        jobs = self.storage.pending_jobs("category", self.cfg.crawl.max_attempts)
        cats = {c["category_id"]: c for c in self.storage.categories()}
        # Root listings first: they hold every product of the subtree; subcategories add membership.
        jobs = sorted(jobs, key=lambda j: json.loads(j["meta_json"] or "{}").get("depth", 0))
        enqueued_per_root: dict[str, int] = {}
        for job in jobs:
            cat_row = cats.get(job["category_id"])
            if cat_row is None:
                continue
            cat = Category(**{k: cat_row[k] for k in ("category_id", "name", "canonical_url",
                                                       "parent_category_id", "root_category_id", "depth")})
            root = cat.root_category_id or cat.category_id
            is_root = cat.depth == 0
            # Sample mode: root listing stops at the limit; subcategory listings only need page 1 for membership.
            max_products = self.limit if (self.limit and is_root) else None
            max_pages = 1 if (self.limit and not is_root) else None
            self.storage.mark_job(job["canonical_url"], "in_progress", bump_attempt=True)
            try:
                trav = await nav.traverse_listing(cat, max_products=max_products, max_pages=max_pages)
            except FetchError as e:
                log.warning("listing_failed", category=cat.category_id, error=str(e))
                status = "pending" if e.transient else "failed"
                self.storage.mark_job(job["canonical_url"], status, str(e))
                if status == "failed":
                    self.storage.dead_letter(job["canonical_url"], "listing", "fetch_error", str(e),
                                             job["attempts"] + 1, self.run_id)
                continue
            self.counters["listing_pages"] += trav.pages_seen
            for hit in trav.hits:
                self.storage.record_listing_hit(hit.url, cat.category_id, hit.source, hit.source_product_id)
                existing = self.storage.job(hit.url)
                in_run = existing is not None and existing["run_id"] == self.run_id and existing["status"] != "idle"
                if in_run:
                    self.counters["duplicate_urls_skipped"] += 1   # seen in another listing this run
                    continue
                if self.limit and enqueued_per_root.get(root, 0) >= self.limit:
                    continue                                       # sample mode: out of scope
                meta = {"name": hit.name, "type_id": hit.type_id, "source_product_id": hit.source_product_id}
                if self.resume:
                    # never re-open URLs the interrupted run already completed
                    added = self.storage.enqueue(hit.url, "product", self.run_id, cat.category_id, meta)
                    outcome = "new" if added else "duplicate"
                else:
                    outcome = self.storage.schedule(hit.url, "product", self.run_id, cat.category_id, meta)
                if outcome != "duplicate":
                    self.counters["product_urls_discovered"] += 1
                    enqueued_per_root[root] = enqueued_per_root.get(root, 0) + 1
            self.storage.mark_job(job["canonical_url"], "done")
            meta = {"nb_hits": trav.nb_hits, "nb_pages": trav.nb_pages, "pages_seen": trav.pages_seen,
                    "hits": len(trav.hits), "source": trav.source, "stopped": trav.stopped_reason,
                    "depth": cat.depth}
            self.storage.conn.execute("UPDATE crawl_jobs SET meta_json=? WHERE canonical_url=?",
                                      (json.dumps(meta), job["canonical_url"]))
            log.info("listing_done", category=cat.category_id, **meta)

    # -------------------------------------------------------------- products
    async def process_products(self) -> None:
        pipeline = ProductPipeline(
            fetcher=self.fetcher, browser=self.browser,
            classifier=PageClassifier(self.llm, self.cfg.llm.ambiguous_classification),
            extractor=ExtractorAgent(self.llm, self.cfg.llm.extraction_fallback, self.cfg.llm.spec_extraction),
            validator=ValidatorAgent(), recovery=RecoveryAgent(self.llm, self.cfg.llm.recovery_decisions),
            storage=self.storage, max_attempts=self.cfg.crawl.max_attempts, llm_available=self.llm.available,
        )
        self.pipeline = pipeline
        jobs = self.storage.pending_jobs("product", self.cfg.crawl.max_attempts)
        log.info("product_phase_started", pending=len(jobs))
        queue: asyncio.Queue = asyncio.Queue()
        for j in jobs:
            queue.put_nowait(j["canonical_url"])

        async def worker(n: int) -> None:
            while True:
                try:
                    url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                t0 = time.monotonic()
                try:
                    state = await pipeline.run(url, self.run_id, self.cfg.crawl.product_fetch_mode)
                except Exception as e:  # last-resort guard: one bad page never kills the run
                    log.exception("pipeline_crashed", url=url, error=str(e))
                    self.storage.dead_letter(url, "pipeline", type(e).__name__, str(e), 1, self.run_id)
                    self.storage.mark_job(url, "failed", str(e))
                    state = {"outcome": "dead_letter"}
                self.latencies.append(time.monotonic() - t0)
                self.counters["products_processed"] += 1
                if state.get("outcome") == "stored":
                    self.counters["products_stored"] += 1
                    self.counters[state.get("store_outcome") or "inserted"] += 1
                else:
                    self.counters["products_dead_lettered"] += 1
                done = self.counters["products_processed"]
                if done % 10 == 0:
                    log.info("progress", processed=done, remaining=queue.qsize())

        await asyncio.gather(*(worker(i) for i in range(max(1, self.cfg.crawl.concurrency))))

    # --------------------------------------------------------------- metrics
    def metrics(self, elapsed_s: float) -> dict:
        p = getattr(self, "pipeline", None)
        lat = sorted(self.latencies)
        pct = (lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))], 2)) if lat else (lambda q: None)
        return {
            "run_id": self.run_id,
            "elapsed_s": elapsed_s,
            "counters": self.counters,
            "http": self.fetcher.stats,
            "browser": self.browser.stats if self.browser else {},
            "agents": {
                "classifier": p.classifier.stats if p else {},
                "extractor": p.extractor.stats if p else {},
                "validator": p.validator.stats if p else {},
                "recovery": p.recovery.stats if p else {},
            },
            "llm": {**self.llm.stats, "available": self.llm.available, "model": self.cfg.llm.model},
            "product_latency_s": {"p50": pct(0.5), "p90": pct(0.9), "max": lat[-1] if lat else None},
        }
