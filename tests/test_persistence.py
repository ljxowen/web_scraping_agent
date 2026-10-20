"""SQLite persistence: idempotent upserts, dedup, frontier/resume semantics, exports, and the graph."""
import csv
import json
from decimal import Decimal

from safco_agent.agents.classifier import PageClassifier
from safco_agent.agents.extractor import ExtractorAgent
from safco_agent.agents.recovery import RecoveryAgent
from safco_agent.agents.validator import ValidatorAgent
from safco_agent.export.exporters import export_all
from safco_agent.graph import ProductPipeline
from safco_agent.infra.http import FetchError
from safco_agent.models import Category, Variant


def _product(page):
    return ValidatorAgent().validate(ExtractorAgent().extract(page)).product


def test_upsert_is_idempotent(storage, lavender_page):
    p = _product(lavender_page)
    assert storage.upsert_product(p, "r1") == "inserted"
    assert storage.upsert_product(p, "r2") == "unchanged"
    p.variants[0].price = Decimal("40.00")
    assert storage.upsert_product(p, "r3") == "updated"
    assert storage.query("SELECT COUNT(*) n FROM products")[0]["n"] == 1
    assert storage.query("SELECT COUNT(*) n FROM variants")[0]["n"] == 5


def test_removed_variants_are_deleted(storage, lavender_page):
    p = _product(lavender_page)
    storage.upsert_product(p, "r1")
    p.variants = p.variants[:2]
    storage.upsert_product(p, "r2")
    assert [r["sku"] for r in storage.variants_for(p.product_id)] == ["3020800", "3020810"]


def test_content_hash_ignores_volatile_fields(lavender_page):
    a, b = _product(lavender_page), _product(lavender_page)
    b.scraped_at = "2030-01-01T00:00:00+00:00"
    b.field_provenance = {}
    assert a.content_hash() == b.content_hash()


def test_frontier_dedup_and_resume(storage):
    assert storage.enqueue("https://x/p1", "product", "r1") is True
    assert storage.enqueue("https://x/p1", "product", "r1") is False      # duplicate discovery
    storage.mark_job("https://x/p1", "in_progress", bump_attempt=True)
    assert storage.pending_jobs("product", 3) == []
    assert storage.recover_in_progress() == 1                            # crash recovery
    assert len(storage.pending_jobs("product", 3)) == 1
    storage.mark_job("https://x/p1", "done")
    storage.park_jobs("product")                                          # new fresh run
    assert storage.schedule("https://x/p1", "product", "r2") == "rescheduled"
    assert storage.schedule("https://x/p1", "product", "r2") == "duplicate"
    assert storage.schedule("https://x/p2", "product", "r2") == "new"


def test_export_files(storage, lavender_page, surgicel_page, tmp_path):
    storage.upsert_category(Category(category_id="catalog/gloves", name="Dental Exam Gloves",
                                     canonical_url="https://www.safcodental.com/catalog/gloves",
                                     root_category_id="catalog/gloves"))
    for page in (lavender_page, surgicel_page):
        p = _product(page)
        storage.upsert_product(p, "r1")
    storage.link_product_category("safco:95833", "catalog/gloves", "listing:algolia")
    paths = export_all(storage, str(tmp_path / "out"))
    data = json.loads((tmp_path / "out" / "products.json").read_text(encoding="utf-8"))
    assert len(data) == 2
    lav = next(d for d in data if d["product_id"] == "safco:95833")
    assert lav["root_categories"] == ["Dental Exam Gloves"] and len(lav["variants"]) == 5
    with open(paths["variants.csv"], encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 6 and rows[0]["sku"]
    report = json.loads((tmp_path / "out" / "crawl_report.json").read_text(encoding="utf-8"))
    assert report["totals"]["product_families"] == 2 and report["totals"]["sku_variants"] == 6
    assert report["field_completeness_products"]["name"] == 1.0


class _FakeFetcher:
    """Serves fixture pages; can fail the first N calls to exercise retry routing."""

    def __init__(self, pages, fail_first=0, permanent=False):
        self.pages, self.fail_first, self.permanent, self.calls = pages, fail_first, permanent, 0

    async def fetch(self, url):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise FetchError(url, "HTTP 503" if not self.permanent else "HTTP 404", transient=not self.permanent)
        return self.pages[url]


def _pipeline(storage, fetcher, llm=None):
    return ProductPipeline(fetcher=fetcher, browser=None, classifier=PageClassifier(), extractor=ExtractorAgent(llm),
                           validator=ValidatorAgent(), recovery=RecoveryAgent(), storage=storage, max_attempts=3,
                           llm_available=llm is not None)


async def test_graph_stores_product_after_transient_failure(storage, lavender_page):
    storage.enqueue(lavender_page.url, "product", "r1")
    fetcher = _FakeFetcher({lavender_page.url: lavender_page}, fail_first=1)
    state = await _pipeline(storage, fetcher).run(lavender_page.url, "r1", "http")
    assert state["outcome"] == "stored" and fetcher.calls == 2
    assert state["tried"] == ["rerender"]  # switch transport after an HTTP failure
    assert storage.job(lavender_page.url)["status"] == "done"


async def test_graph_dead_letters_permanent_failure(storage, lavender_page):
    storage.enqueue(lavender_page.url, "product", "r1")
    fetcher = _FakeFetcher({}, fail_first=99, permanent=True)
    state = await _pipeline(storage, fetcher).run(lavender_page.url, "r1", "http")
    assert state["outcome"] == "dead_letter" and fetcher.calls == 1  # 404 is not retried
    dl = storage.dead_letters()
    assert dl[0]["stage"] == "fetch" and dl[0]["error_type"] == "fetch_error"


async def test_graph_llm_recovery_path(storage, drifted_lavender_page, fake_llm_factory):
    """Layout drift -> validate recoverable -> recover (rerender=http here) -> llm_extract -> store."""
    llm = fake_llm_factory({"extract_fallback": {
        "name": {"value": "Lavender Nitrile", "evidence": "Lavender Nitrile"},
        "price": {"value": 38.99, "evidence": "$38.99"}}})
    storage.enqueue(drifted_lavender_page.url, "product", "r1")
    fetcher = _FakeFetcher({drifted_lavender_page.url: drifted_lavender_page})
    state = await _pipeline(storage, fetcher, llm).run(drifted_lavender_page.url, "r1", "http")
    assert state["outcome"] == "stored"
    assert "llm_extract" in state["tried"]
    row = storage.query("SELECT * FROM products")[0]
    assert row["name"] == "Lavender Nitrile" and row["extraction_method"] == "deterministic+llm"


async def test_graph_not_a_product(storage):
    from safco_agent.models import FetchedPage
    url = "https://www.safcodental.com/product/gone"
    storage.enqueue(url, "product", "r1")
    page = FetchedPage(url=url, html="<title>404 Not Found</title><h1>Whoops</h1>", status=200)
    state = await _pipeline(storage, _FakeFetcher({url: page})).run(url, "r1", "http")
    assert state["outcome"] == "dead_letter"
    assert storage.dead_letters()[0]["error_type"] == "not_a_product"


async def test_graph_degrades_when_recovery_exhausted(storage, lavender_html, fake_llm_factory):
    """Price visible but unparsable, LLM unavailable/failing -> bounded retries -> stored with warnings."""
    from safco_agent.infra.llm import LLMError
    from safco_agent.models import FetchedPage
    url = "https://www.safcodental.com/product/lavender-nitrile"
    html = lavender_html.replace("window.masterData", "window.__x").replace('"price":"38.99"', '"price":""')
    page = FetchedPage(url=url, html=html, via="http")
    llm = fake_llm_factory({"extract_fallback": LLMError("provider down")})
    storage.enqueue(url, "product", "r1")
    state = await _pipeline(storage, _FakeFetcher({url: page}), llm).run(url, "r1", "http")
    assert state["outcome"] == "stored"
    assert state["tried"] == ["rerender", "llm_extract", "give_up"]
    row = storage.query("SELECT validation_status, validation_issues_json FROM products")[0]
    assert row["validation_status"] == "valid_with_warnings"
    assert "unresolved_after_recovery" in row["validation_issues_json"]
