"""Opt-in live checks against the real site and LLM (RUN_LIVE=1 uv run pytest -m live)."""
import pytest

from safco_agent.agents.extractor import ExtractorAgent
from safco_agent.agents.navigator import NavigatorAgent
from safco_agent.config import load_config
from safco_agent.infra.browser import BrowserPool
from safco_agent.infra.http import HttpFetcher
from safco_agent.infra.llm import LLMClient
from safco_agent.infra.rate_limit import TokenBucket
from safco_agent.models import Category

pytestmark = pytest.mark.live


async def test_live_listing_pagination_via_algolia():
    cfg = load_config()
    limiter = TokenBucket(1.0, 2)
    fetcher = HttpFetcher(cfg, limiter)
    await fetcher.start()
    browser = BrowserPool(cfg, limiter, fetcher.robots)
    await browser.start()
    try:
        cat = Category(category_id="catalog/gloves/latex-gloves", name="Latex gloves",
                       canonical_url="https://www.safcodental.com/catalog/gloves/latex-gloves")
        trav = await NavigatorAgent(browser, cfg.site.base_url).traverse_listing(cat)
        assert trav.source == "algolia"
        assert trav.nb_pages and trav.nb_pages > 1 and trav.pages_seen == trav.nb_pages  # paginated
        assert len(trav.hits) == trav.nb_hits
    finally:
        await browser.close()
        await fetcher.close()


async def test_live_llm_spec_structuring(lavender_page, storage):
    cfg = load_config()
    llm = LLMClient(cfg.llm, storage, "live-test")
    if not llm.available:
        pytest.skip("no LLM key configured")
    ex = ExtractorAgent(llm)
    p = ex.extract(lavender_page).product
    await ex.llm_structure_specs(p)
    llm_specs = [k for k, v in p.field_provenance.items() if k.startswith("specifications.") and v == "llm"]
    assert llm_specs, "expected at least one evidence-backed spec from the LLM"


async def test_live_llm_recovers_variants_after_simulated_rename(storage):
    """Realistic drift: the storefront renames its variant blob. The table still renders (from the JS),
    our deterministic parser finds no variants, and the evidence-checked LLM fallback recovers them
    from the visible rows. Ground truth = the deterministic parse of the same rendered page."""
    cfg = load_config()
    llm = LLMClient(cfg.llm, storage, "live-test")
    if not llm.available:
        pytest.skip("no LLM key configured")
    limiter = TokenBucket(1.0, 2)
    fetcher = HttpFetcher(cfg, limiter)
    await fetcher.start()
    browser = BrowserPool(cfg, limiter, fetcher.robots)
    await browser.start()
    try:
        page = await browser.render_product("https://www.safcodental.com/product/lavender-nitrile")
    finally:
        await browser.close()
        await fetcher.close()
    truth = ExtractorAgent().extract(page).product
    drifted = page.model_copy(update={"html": page.html.replace("window.masterData", "window.productRows")})
    ex = ExtractorAgent(llm)
    r = ex.extract(drifted)
    assert r.product.variants == [] and "variants" in r.visible_but_unparsed
    r = await ex.llm_fill_missing(drifted, r)
    got = {v.sku: v.price for v in r.product.variants}
    want = {v.sku: v.price for v in truth.variants}
    assert got == want, (got, want)
    assert r.product.field_provenance["variants"] == "llm"
