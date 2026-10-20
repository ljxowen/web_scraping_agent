# Requirements checklist

This file compares each requirement in *Take-Home Test: Build an AI Agent for Product Scraping and Structured Catalog Extraction (POC)* with what this repository delivers.

**Legend:** ✅ met · 🟡 partly met (reason given) · ❌ not done

The figures come from the latest full crawl, run `20261020T044924Z-94f6af`, recorded in `output/crawl_report.json`. That run stored 148 products, dead-lettered 0, and took 211 s. The test figures come from `uv run pytest`: 103 passed, plus 3 live tests that are skipped unless `RUN_LIVE=1` is set.

**Summary (sections 1–8):** 67 ✅ · 1 🟡 · 0 ❌. The partial item is the LLM selector-repair suggestion (4.10), which the PDF lists as an optional example. Section 9 lists other scope notes, such as Magento "configurable" products, which do not occur in the two target categories. All of these are in README → Limitations.

---

## 1. Assignment capabilities

| # | Required | What we did | Evidence | Status |
|---|---|---|---|---|
| 1.1 | Discover product categories | The Navigator reads subcategories from the category page's JSON-LD `ItemList`, with an `<a href>` scan as fallback. 2 seeds yielded 14 subcategories (16 categories in total). | `agents/navigator.py`; test `test_category_discovery_from_jsonld`; `categories_discovered: 16` | ✅ |
| 1.2 | Traverse categories and extract all products from listing and detail pages | Listings are rendered with Playwright. Product references come from the storefront's own Algolia responses, and the crawler pages through them with the on-page "Next" control. Every product page is then fetched and extracted. | For all 16 listings, extracted count = site's `nbHits`. Examples: gloves 93/93 (8 pages), nitrile 62/62 (6 pages), sutures 55/55 (5 pages). See `listing_traversal` in `crawl_report.json`. | ✅ |
| 1.3 | Normalize into a clean structured format | Pydantic `Product` / `Variant` models with normalized prices (Decimal), availability (enum), pack size and unit, size, volume and dimensions. Spec names follow a controlled vocabulary of 27 canonical names. | `models.py`, `extractors/variants.py`, `extractors/spec_vocabulary.py`; `test_variants.py`, `test_spec_vocabulary.py` | ✅ |
| 1.4 | Store results so they can be queried or exported | SQLite is the source of truth. Data is exported to JSON, CSV and XLSX, and the CLI `export` / `stats` commands read from it. | `data/catalog.db`; snapshot `output/catalog.sqlite`; `export/exporters.py` | ✅ |
| 1.5 | Designed so it can later be hardened for production | See section 4 (production design), plus the Dockerfile and the README scaling plan | Section 4 below | ✅ |
| 1.6 | Target: Sutures & surgical products | All 55 product families and 350 SKUs | `products_per_root_category` | ✅ |
| 1.7 | Target: Dental Exam Gloves | All 93 product families and 392 SKUs | `products_per_root_category` | ✅ |
| 1.8 | Prove it *can* crawl to completion with the right runtime and controls | Went further than proving it: both categories were crawled completely, and a crash and resume was demonstrated on a real run | `crawl_runs` table: crashed run, then resume, then full re-crawl with 148/148 `unchanged` | ✅ |

## 2. Product fields ("capture as many as possible")

Coverage across 148 product families and 742 SKU variants:

| # | Required field | What we did | Coverage | Status |
|---|---|---|---|---|
| 2.1 | Product name | JSON-LD `name` | 148/148 | ✅ |
| 2.2 | Brand / manufacturer | JSON-LD `brand`, plus the manufacturer from the embedded variant data | 148/148 | ✅ |
| 2.3 | SKU / item number / product code | Family SKU, each variant's SKU (Safco item #), and the manufacturer part number | 148/148 family SKU · 742/742 variant SKU · 742/742 mfr # | ✅ |
| 2.4 | Category hierarchy | Breadcrumb path, plus every category whose listing showed the product | 148/148 (3 levels) | ✅ |
| 2.5 | Product URL | Canonical URL | 148/148 | ✅ |
| 2.6 | Price, if publicly visible | A per-SKU price, plus a family min/max with a `price_type` of range, exact or from | 742/742 variants · 148/148 products | ✅ |
| 2.7 | Unit / pack size, if available | Parsed from option text, e.g. "250/box" becomes 250 + box. Counts are never inferred. | Pack 664/742 · unit 707/742. The rest are options such as "15 x 20mm" with no stated count. | ✅ |
| 2.8 | Availability / stock indicator, if available | Per-SKU availability mapped to in stock, backorder, special order, drop-ship or not purchasable | 742/742 | ✅ |
| 2.9 | Description | Description section with bullet structure preserved | 148/148 | ✅ |
| 2.10 | Specifications / attributes | Deterministic `Key: value` lines and tables, plus evidence-checked LLM structuring of free-text bullets, mapped onto 27 canonical names. The long tail is kept in `extra_specifications`. Variant attributes include size, pack, volume and dimensions. | 118/148 products have specs; 27 spec names (down from 102) | ✅ |
| 2.11 | Image URL(s), if available | Gallery images with resize parameters removed | 148/148 | ✅ |
| 2.12 | Alternative products | The site's Adobe Recommendations for the product, kept only when they share a category with it. The source unit is recorded in `relation`. | 119/148 | ✅ |
| 2.13 | *Accuracy (implied)* | The script re-fetched a 16-product sample from the live site and checked every value occurs in the page | name 16/16 · SKU 16/16 · variant SKUs 51/51 · mfr codes 51/51 · prices 51/51 · breadcrumbs 48/48 · brand 16/16 · LLM spec values 58/58 (`output/verification.md`) | ✅ |

## 3. Structured output

| # | Required | What we did | Evidence | Status |
|---|---|---|---|---|
| 3.1 | Persist as CSV / JSON / Excel | All three formats, plus SQLite | `output/products.json`, `products.csv`, `variants.csv`, `products.xlsx`, `catalog.sqlite` | ✅ |
| 3.2 | Schema clearly documented | README field tables for Product and Variant, plus a machine-readable JSON Schema | README → Output schema; `schema/product.schema.json` | ✅ |

## 4. Agentic workflow

| # | Required / suggested | What we did | Evidence | Status |
|---|---|---|---|---|
| 4.1 | Agent-based design, not one monolithic script | Five agent classes, connected by a LangGraph `StateGraph` with conditional routing | `agents/*.py`, `graph.py` | ✅ |
| 4.2 | Navigator agent | Discovers categories, paginates listings, maintains the crawl queue (frontier) | `agents/navigator.py` | ✅ |
| 4.3 | Page classifier | Classifies pages by rules (status, title, body class, JSON-LD). The LLM is used only for ambiguous pages. | `agents/classifier.py`; 148/148 classified by rules; `test_classifier_rules`, `test_graph_not_a_product` | ✅ |
| 4.4 | Extractor agent | Three levels: JSON-LD, then DOM and embedded data, then LLM fallback. Each field records where it came from. | `agents/extractor.py`; `test_extract_grouped_product` | ✅ |
| 4.5 | Validator / deduplicator | Validator returns valid, valid_with_warnings, recoverable or fatal. Deduplication covers URLs, variants and upserts. | `agents/validator.py`; `test_price_out_of_range_and_duplicate_variants`, `test_upsert_is_idempotent` | ✅ |
| 4.6 | Retry / recovery logic | Recovery agent picks from a fixed set of actions: rerender, retry over HTTP, LLM extract, give up. Every loop is bounded. | `agents/recovery.py`; `test_recovery_policy_bounded`, `test_graph_*` (5 workflow tests) | ✅ |
| 4.7 | LLM use: page type detection | Ambiguous pages only; the answer must be one of the allowed page types | `classifier.py` | ✅ |
| 4.8 | LLM use: extraction fallback for irregular layouts | Runs only when a field is visibly on the page but couldn't be parsed. Every value must quote its source text. | Live test `test_live_llm_recovers_variants_after_simulated_rename` recovers all 5 SKUs with prices matching ground truth; `test_llm_fallback_rejects_fabricated_values` | ✅ |
| 4.9 | LLM use: reasoning over navigation decisions | Optional LLM chooser for recovery actions, restricted to the allowed actions. Off by default. | `recovery.py`; `test_llm_recovery_choice_restricted_to_allowed` | ✅ |
| 4.10 | LLM use: selector repair suggestions | **Not built.** The signals that would trigger it (LLM fallback counts, rejected values) are recorded per run. | README → Limitations | 🟡 |
| 4.11 | Use agents/LLMs only where they add value | Deterministic extraction comes first. The LLM is measured against a deterministic-only baseline in the same run. | Products with ≥1 spec 71 → 118 · spec values 92 → 801 · 55 unsupported values rejected (`verification.md`) | ✅ |

## 5. Production-minded design

| # | Required | What we did | Evidence | Status |
|---|---|---|---|---|
| 5.1 | Rate limiting | One token bucket per domain, shared by HTTP and the browser (1 req/s, burst 2). A 429 `Retry-After` pauses the whole domain. | `infra/rate_limit.py`, `infra/http.py` | ✅ |
| 5.2 | Retries | tenacity exponential backoff with jitter for timeouts, 5xx and 429. The workflow can also switch between browser and HTTP. | `infra/http.py`; `test_graph_stores_product_after_transient_failure` | ✅ |
| 5.3 | Error handling | Errors are typed as transient or permanent. 403/404 are never retried. Failures go to a dead-letter table. Each URL is isolated, so one failure never stops the run. | `test_graph_dead_letters_permanent_failure`; dead letters 0 in the final run | ✅ |
| 5.4 | Resumability / checkpointing | The SQLite `crawl_jobs` table tracks each URL as pending, in progress, done or failed. `resume` and `--retry-failed` use it. | Real crash: killed after 17 products, then resume re-queued the 3 in-progress and 128 pending jobs and skipped 17. Test `test_frontier_dedup_and_resume`. | ✅ |
| 5.5 | Logging | structlog: readable console output plus `logs/crawl.jsonl` with run id, stage, URL, timings and outcome | `infra/logging.py` | ✅ |
| 5.6 | Deduplication | Canonical URLs, `INSERT OR IGNORE` in the queue, and unique keys on product, variant and category | 148 duplicate listing hits skipped in the last run | ✅ |
| 5.7 | Idempotency | Upserts plus a `content_hash` over business fields | A full re-crawl gave 148/148 `unchanged`. After parser changes, only the affected records changed (47 and 17 `updated`). | ✅ |
| 5.8 | Config-driven execution | `config.yaml`, validated by Pydantic, with CLI overrides | `config.yaml`, `config.py` | ✅ |
| 5.9 | Secrets management | The API key is read only from the environment or `.env`, which is gitignored. `.env.example` is committed. | Scanned the repo and the DB snapshot: no key present | ✅ |
| 5.10 | Deployment path | Dockerfile using the Playwright base image and the uv lockfile; data, output and logs on volumes; a production deployment plan in the README | Image `safco-agent:poc` (2.69 GB) builds and runs; `docker run … --help` verified | ✅ |

## 6. Technical minimum bar

| # | Required | What we did | Evidence | Status |
|---|---|---|---|---|
| 6.1 | Python preferred | Python 3.12 | `pyproject.toml` | ✅ |
| 6.2 | A runnable project | `safco-agent` CLI with `crawl`, `resume`, `export` and `stats` | Several real full crawls completed | ✅ |
| 6.3 | Setup and workflow execution instructions | uv setup, every CLI command, and Docker usage | README → Quick start | ✅ |
| 6.4 | A sample output dataset | Full crawl of both categories | `output/` | ✅ |
| 6.5 | Clear notes on current limitations | 10 limitations documented | README → Limitations | ✅ |

## 7. Deliverables

| # | Required | What we did | Evidence | Status |
|---|---|---|---|---|
| 7.A | Git repository with all code needed to run | Source code, config template, schema, tests with fixtures, and scripts | Commits `ba7a700` and `403062e` | ✅ |
| 7.B1 | README: architecture overview | Diagram plus infrastructure description | README → Architecture overview | ✅ |
| 7.B2 | README: why this approach | Spike findings and the design decision each one led to | README → Why this approach | ✅ |
| 7.B3 | README: agent responsibilities | One table row per agent | README → Agent responsibilities | ✅ |
| 7.B4 | README: setup and execution | Commands for each step | README → Quick start | ✅ |
| 7.B5 | README: sample output schema | Field tables plus a JSON example | README → Output schema | ✅ |
| 7.B6 | README: limitations | — | README → Limitations | ✅ |
| 7.B7 | README: failure handling | Table of failure types and how each is handled | README → Failure handling | ✅ |
| 7.B8 | README: scaling to full-site crawling | 8-step production plan | README → Scaling to full-site crawling in production | ✅ |
| 7.B9 | README: monitoring data quality | What the crawl report contains, and proposed alert thresholds | README → Monitoring data quality | ✅ |
| 7.C1 | Sample of scraped products from 2 categories | All 148 products, not just a sample | `output/products.json` and the CSV/XLSX exports | ✅ |
| 7.C2 | Output files or database dump | Both | `output/*` plus `output/catalog.sqlite` | ✅ |
| 7.D | Within the 24-hour time guideline | Built in a single working session | Git history | ✅ |

## 8. "What a strong submission looks like"

| # | Criterion | What we did | Evidence | Status |
|---|---|---|---|---|
| 8.1 | Crawls category and product pages with product variations | 742 SKUs; 123 families have several variants, up to 48 for Perma Sharp sutures | `variants.csv` | ✅ |
| 8.2 | Handles dynamic content or pagination | Both: Algolia listings rendered client-side, 35 pages walked with "Next", lazily loaded recommendations captured | Live test `test_live_listing_pagination_via_algolia` | ✅ |
| 8.3 | Stores normalized data cleanly | Typed schema; product families kept separate from their variants; missing values stored as `null`, never guessed. Spec names come from a controlled vocabulary (102 free-form names reduced to 27), and categorical values are normalized (e.g. Powder Status is uniformly "Powder-free"). | `schema/product.schema.json` | ✅ |
| 8.4 | Separates navigation, extraction and validation | Separate agent classes wired together by the workflow | `agents/`, `graph.py` | ✅ |
| 8.5 | Documents how to scale to production | Plan covering a queue, workers, Postgres, incremental crawls and observability | README → Scaling | ✅ |
| 8.6 | Practical use of AI, not unnecessary complexity | LLM limited to three uses, each checked against page text and measured against a deterministic baseline | README → Where the LLM is used; `verification.md` | ✅ |

## 9. Known gaps and scope notes

| Item | Status | Note |
|---|---|---|
| LLM selector-repair suggestions | 🟡 Not built | Listed in the PDF as an example. The counters that would trigger it are already in the crawl report. |
| Magento "configurable" products (colour/size swatches) | 🟡 No dedicated parser | All 148 products in the target categories are "grouped". A configurable product would be flagged `missing_variants`, and the LLM fallback applies if its options are visible on the page. |
| Sitemap-based discovery fallback | Designed, not built | It wasn't needed because the Algolia path worked for all 16 listings. |
| Single-process design | By design for a POC | The distributed version is described in README → Scaling. |
