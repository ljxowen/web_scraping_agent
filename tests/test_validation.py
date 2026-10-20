"""Validator outcomes, evidence checks, LLM fallback (with a fake LLM), and recovery policy."""
from decimal import Decimal

import pytest

from safco_agent.agents.extractor import ExtractorAgent, evidence_supported
from safco_agent.agents.recovery import RecoveryAction, RecoveryAgent, RecoveryContext
from safco_agent.agents.validator import ValidatorAgent
from safco_agent.models import ExtractionResult, Product, Variant


def test_valid_product(lavender_page):
    v = ValidatorAgent().validate(ExtractorAgent().extract(lavender_page))
    assert v.status == "valid" and v.issues == []
    assert v.product.validation_status == "valid"


def test_layout_drift_is_recoverable_not_fatal(drifted_lavender_page):
    r = ExtractorAgent().extract(drifted_lavender_page)
    assert "name" in r.missing_critical and "price" in r.missing_critical
    # The page still visibly shows the name (h1) and a price ("From $38.99")
    assert {"name", "price"} <= set(r.visible_but_unparsed)
    assert ValidatorAgent().validate(r).status == "recoverable"


def test_missing_identity_not_visible_is_fatal():
    p = Product(product_id="url:x", canonical_url="https://www.safcodental.com/product/x")
    r = ExtractionResult(product=p, missing_critical=["name", "sku", "price"], visible_but_unparsed=[])
    assert ValidatorAgent().validate(r).status == "fatal"


def test_price_out_of_range_and_duplicate_variants():
    p = Product(product_id="safco:1", canonical_url="https://www.safcodental.com/product/x", name="X",
                parent_sku="X1", category_path=["A"], description="d", image_urls=["i"],
                variants=[Variant(sku="A", price=Decimal("-1")), Variant(sku="A", price=Decimal("5"))])
    v = ValidatorAgent().validate(ExtractionResult(product=p))
    assert len(v.product.variants) == 1  # deduplicated by SKU
    assert any(i.startswith("variant_price_out_of_range") for i in v.issues)


def test_price_hidden_on_page_is_warning_not_failure():
    p = Product(product_id="safco:1", canonical_url="https://www.safcodental.com/product/x", name="X",
                parent_sku="X1", category_path=["A"], description="d", image_urls=["i"])
    v = ValidatorAgent().validate(ExtractionResult(product=p, missing_critical=["price"], visible_but_unparsed=[]))
    assert v.status == "valid_with_warnings" and "missing_price" in v.issues


@pytest.mark.parametrize("value,evidence,ok", [
    ("38.99", "From $38.99", True),
    ("12.50", "From $12.50", False),           # quote not in source -> fabricated
    ("Lavender Nitrile", "Lavender Nitrile", True),
    ("Purple Nitrile", "Lavender Nitrile", False),  # value not in quote
    ("", "From $38.99", False),
])
def test_evidence_supported(value, evidence, ok):
    source = "HALYARD\nLavender Nitrile\nFrom $38.99\nPowder-free nitrile exam gloves."
    assert evidence_supported(value, evidence, source) is ok


async def test_llm_fallback_recovers_visible_fields(drifted_lavender_page, fake_llm_factory):
    llm = fake_llm_factory({"extract_fallback": {
        "name": {"value": "Lavender Nitrile", "evidence": "Lavender Nitrile"},
        "price": {"value": 38.99, "evidence": "$38.99"},
    }})
    ex = ExtractorAgent(llm)
    r = ex.extract(drifted_lavender_page)
    r = await ex.llm_fill_missing(drifted_lavender_page, r)
    p = r.product
    assert p.name == "Lavender Nitrile" and p.price_min == Decimal("38.99")
    assert p.field_provenance["name"] == "llm" and p.field_provenance["price"] == "llm"
    assert p.extraction_method == "deterministic+llm"
    # The prompt marks page text as untrusted and only asks for missing fields
    task, prompt = llm.calls[0]
    assert task == "extract_fallback" and "name" in prompt and "<<<" in prompt
    assert ValidatorAgent().validate(r).status in ("valid", "valid_with_warnings")


async def test_llm_fallback_rejects_fabricated_values(drifted_lavender_page, fake_llm_factory):
    llm = fake_llm_factory({"extract_fallback": {
        "name": {"value": "Lavender Nitrile Pro Max", "evidence": "Lavender Nitrile Pro Max"},
        "price": {"value": 12.5, "evidence": "Only $12.50 today"},
    }})
    ex = ExtractorAgent(llm)
    r = await ex.llm_fill_missing(drifted_lavender_page, ex.extract(drifted_lavender_page))
    assert r.product.name is None and r.product.price_min is None
    assert ex.stats["llm_fallback_rejected_values"] == 2
    assert ValidatorAgent().validate(r).status == "recoverable"


async def test_llm_not_called_when_fields_absent_from_page(fake_llm_factory):
    llm = fake_llm_factory({})
    p = Product(product_id="safco:1", canonical_url="https://www.safcodental.com/product/x", name="X")
    r = ExtractionResult(product=p, missing_critical=["price"], visible_but_unparsed=[])
    from safco_agent.models import FetchedPage
    await ExtractorAgent(llm).llm_fill_missing(FetchedPage(url=p.canonical_url, html="<h1>X</h1>"), r)
    assert llm.calls == []


async def test_llm_spec_structuring_keeps_only_quoted_values(lavender_page, fake_llm_factory):
    llm = fake_llm_factory({"structure_specs": {"specifications": [
        {"name": "Color", "value": "Lavender", "evidence": "Lavender color"},
        {"name": "Count Per Box", "value": "250 gloves", "evidence": "250 gloves per box (230 for x-large)"},
        {"name": "Latex Free", "value": "Yes, certified", "evidence": "Certified latex free"},  # hallucinated
    ]}})
    ex = ExtractorAgent(llm)
    p = ex.extract(lavender_page).product
    await ex.llm_structure_specs(p)
    assert p.specifications["Color"] == "Lavender"
    assert p.specifications["Count Per Pack"] == "250 gloves"  # mapped onto the vocabulary
    assert "Latex Free" not in p.specifications
    assert p.specifications["Thickness"].startswith("at palm")  # deterministic value kept
    assert p.field_provenance["specifications.Color"] == "llm"
    assert ex.stats["llm_specs_rejected"] == 1


async def test_recovery_policy_bounded():
    agent = RecoveryAgent()
    ctx = RecoveryContext(url="u", stage="validate", visible_missing=["price"], llm_available=True)
    assert await agent.decide(ctx) == RecoveryAction.RERENDER
    ctx.tried = ["rerender"]
    assert await agent.decide(ctx) == RecoveryAction.LLM_EXTRACT
    ctx.tried = ["rerender", "llm_extract"]
    assert await agent.decide(ctx) == RecoveryAction.GIVE_UP
    fetch_ctx = RecoveryContext(url="u", stage="fetch", transient=False)
    assert await agent.decide(fetch_ctx) == RecoveryAction.GIVE_UP  # 403/404 never retried
    exhausted = RecoveryContext(url="u", stage="fetch", attempts=3, max_attempts=3)
    assert await agent.decide(exhausted) == RecoveryAction.GIVE_UP


async def test_llm_recovery_choice_restricted_to_allowed(fake_llm_factory):
    llm = fake_llm_factory({"recovery_decision": {"action": "delete_database"}})
    agent = RecoveryAgent(llm, use_llm_decisions=True)
    ctx = RecoveryContext(url="u", stage="validate", visible_missing=["price"], llm_available=True)
    assert await agent.decide(ctx) in (RecoveryAction.RERENDER, RecoveryAction.LLM_EXTRACT)


def test_grouped_product_without_sellable_children_is_kept_with_warning():
    """Real page (R.T.R. Membrane): masterData is "[]" and the Products table is empty even when rendered.
    Missing variants are legitimately absent, so no LLM call and no dead letter."""
    from conftest import read
    from safco_agent.models import FetchedPage, PriceType
    page = FetchedPage(url="https://www.safcodental.com/product/r-t-r-membrane",
                       html=read("product_grouped_no_children.html"), via="browser")
    r = ExtractorAgent().extract(page)
    assert r.missing_critical == ["variants"] and r.visible_but_unparsed == []
    v = ValidatorAgent().validate(r)
    assert v.status == "valid_with_warnings" and "missing_variants" in v.issues
    assert v.product.price_min == Decimal("108.99") and v.product.price_type == PriceType.FROM


@pytest.mark.parametrize("evidence,ok", [
    ("3020800\nLavender gloves x-small\n$38.99", True),   # row fragments in order, within window
    ("$38.99\n3020800", False),                           # wrong order
    ("3020800\nLavender gloves x-small\n$12.00", False),  # one fragment not in source
])
def test_multi_fragment_evidence(evidence, ok):
    source = "3020800\nPromo\nLavender gloves x-small\nIn stock\n" + "long tooltip text " * 5 + "\n$38.99\nAdd"
    assert evidence_supported("3020800", evidence, source) is ok


def test_multi_fragment_evidence_window_is_bounded():
    source = "3020800\n" + "x " * 1000 + "\n$38.99"
    assert evidence_supported("3020800", "3020800\n$38.99", source) is False


@pytest.mark.parametrize("value,ok", [
    ("Lavender", True),                                  # verbatim sub-phrase
    ("Nitrile, powder-free", True),                      # list of verbatim items
    ("Powder-free nitrile", False),                      # same tokens, recombined -> rejected
])
def test_values_must_be_copied_not_recombined(value, ok):
    source = evidence = "Lavender color. Nitrile exam gloves, powder-free."
    assert evidence_supported(value, evidence, source) is ok


def test_llm_values_are_stored_as_exact_source_spans():
    from safco_agent.agents.extractor import verbatim_span
    src = "Both bags are not manufactured with latex, PVC or DEHP. Click here."
    assert verbatim_span("Not manufactured with latex, PVC, or DEHP", src) == "not manufactured with latex, PVC or DEHP"
    assert verbatim_span("Lavender", "Lavender color") == "Lavender"
    assert verbatim_span("Purple", "Lavender color") is None


def test_verbatim_span_keeps_surrounding_punctuation():
    from safco_agent.agents.extractor import verbatim_span
    src = "Ambidextrous\n250 gloves per box (230 for x-large)\nOrder 10 boxes"
    assert verbatim_span("250 gloves per box (230 for x-large)", src) == "250 gloves per box (230 for x-large)"
    assert verbatim_span("(230 for x-large)", src) == "(230 for x-large)"
