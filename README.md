# Safco Dental: agent-based product catalog scraper (POC)

This is a runnable prototype that discovers categories, walks product listings, extracts product families together with their purchasable SKU variants, validates and normalizes the data, and stores it in SQLite with JSON, CSV and XLSX exports. It targets two Safco Dental Supply categories:

* **Sutures & Surgical Products**: `https://www.safcodental.com/catalog/sutures-surgical-products`
* **Dental Exam Gloves**: `https://www.safcodental.com/catalog/gloves`

The committed sample in [`output/`](output/) is a **full crawl of both categories**. It is real scraped data, and nothing in it is synthetic. See [Sample output](#sample-output) for the numbers.

---

## Contents
1. [Quick start](#quick-start)
2. [Architecture overview](#architecture-overview)
3. [Why this approach](#why-this-approach)
4. [Agent responsibilities](#agent-responsibilities)
5. [Where the LLM is used (and where it is not)](#where-the-llm-is-used-and-where-it-is-not)
6. [Output schema](#output-schema)
7. [Sample output](#sample-output)
8. [Failure handling](#failure-handling)
9. [Production-minded design](#production-minded-design)
10. [Scaling to full-site crawling](#scaling-to-full-site-crawling-in-production)
11. [Monitoring data quality](#monitoring-data-quality)
12. [Limitations](#limitations)
13. [Testing](#testing)
14. [Project layout](#project-layout)

---

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/) (it provides Python 3.12 and the virtualenv). On Windows you can install it with `winget install astral-sh.uv`.

```bash
uv sync                                   # create .venv and install locked deps
uv run playwright install chromium        # headless browser for dynamic listings
cp .env.example .env                      # optional: add MINIMAX_API_KEY to enable LLM steps
```

Run:

```bash
# Sample mode: N products per seed category (subcategory membership still recorded)
uv run safco-agent crawl --max-products-per-category 20

# Full mode: every product in both categories
uv run safco-agent crawl --full

# Deterministic-only (no LLM), e.g. for comparison runs
uv run safco-agent crawl --full --no-llm

# Continue an interrupted run (completed URLs are skipped, in-flight ones re-queued)
uv run safco-agent resume
uv run safco-agent resume --retry-failed   # also re-try dead-lettered URLs

# Re-export from the database, and print quality metrics
uv run safco-agent export --format json,csv,xlsx
uv run safco-agent stats
```

### Docker

The image is built on Microsoft's Playwright image with the uv lockfile. It has been tested end to end: build, `--help`, a sample crawl with LLM calls, and `stats` against the mounted volume. The secret is passed at runtime and is not stored in the image.

```bash
docker build -t safco-agent:poc .
docker run --rm --env-file .env -v "$PWD/data:/app/data" -v "$PWD/output:/app/output" -v "$PWD/logs:/app/logs" safco-agent:poc crawl --max-products-per-category 3
docker run --rm -v "$PWD/data:/app/data" safco-agent:poc stats
```

The default command is `crawl --full`. The image is about 2.7 GB, mostly Chromium and its system libraries. On Git Bash for Windows, prefix `docker run` with `MSYS_NO_PATHCONV=1` so volume paths aren't rewritten.

Behaviour is controlled by [`config.yaml`](config.yaml): seeds, limits, rate limit, concurrency, timeouts, browser settings, and which LLM capabilities are on. Secrets are read only from the environment or `.env`. Each run writes:

| Path | Content |
|---|---|
| `data/catalog.db` | SQLite source of truth: catalog, frontier/checkpoints, runs, dead letters, LLM cache and usage |
| `output/products.json` | Nested product families with variants, categories, specs, alternatives and provenance |
| `output/products.csv` | One row per product family |
| `output/variants.csv` | One row per purchasable SKU |
| `output/products.xlsx` | Both tables as sheets |
| `output/crawl_report.json` | Run metrics and the data-quality report |
| `logs/crawl.jsonl` | Structured JSON logs (one event per line) |

---

## Architecture overview

```
                          config.yaml + .env
                                 |
                        +--------v---------+
                        |   Orchestrator   |  run_id, phases, workers, metrics, export
                        +--------+---------+
        phase 1: discover        |        phase 2: listings            phase 3: products (per URL)
  +------------------------------+-------------------------------+-------------------------------------+
  |                              |                               |                                     |
  v                              v                               v                                     |
NavigatorAgent             NavigatorAgent                LangGraph ProductPipeline                     |
 category tree from         Playwright renders the        fetch -> classify -> extract -> validate     |
 server HTML (JSON-LD        listing; reads the site's       ^        |                    |    |      |
 subcategory ItemList)       own Algolia responses;          |   not product        valid  |  recoverable
                             clicks "Next" until done        |        v                    v    v      |
                                 |                           |   dead_letter        enrich  recover    |
                                 v                           |                        |     /  |  \    |
                         crawl_jobs frontier  ----------------+--- rerender/retry ----+----'   |  give_up
                         listing_hits (membership)                                   v        v   -> degrade | dead_letter
                                                                                   store <- llm_extract
                                                                                     |
                                                     SQLite (products, variants, categories, ...)
                                                                                     |
                                                                    exporters -> JSON / CSV / XLSX / report
```

Shared infrastructure (`src/safco_agent/infra/`):

* `HttpFetcher`: httpx with robots.txt enforcement, a shared token-bucket rate limiter, and tenacity retries using exponential backoff with jitter (honours `Retry-After` on 429).
* `BrowserPool`: Playwright. It blocks images, fonts, media and trackers, and observes the JSON the storefront fetches for itself (Algolia listings, Adobe recommendations). It shares the same rate limiter and robots policy as `HttpFetcher`.
* `Storage`: SQLite with idempotent upserts, the crawl frontier, runs, dead letters, and the LLM cache and usage log.
* `LLMClient`: works with any OpenAI-compatible endpoint (MiniMax by default). It reads the key from the environment, has a timeout and bounded retries, parses JSON-only replies, caches by content hash, and logs usage. With no key it degrades gracefully.

---

## Why this approach

The design follows from what the technical spike (`scripts/spike_*.py`) found about the site:

| Finding | Consequence |
|---|---|
| Category listings are rendered **client-side by Algolia InstantSearch**. Raw HTML contains no product links. | Listings are rendered in **Playwright**. The browser *observes* the Algolia responses the page makes and extracts product URL, id, type and SKUs from them. It never calls the Algolia API directly with the embedded key. |
| Pagination is InstantSearch's "Next" control. The `?page=`/`?p=` URLs it links to are **disallowed by robots.txt**. | The navigator clicks the on-page control, which issues a search XHR, and never fetches the disallowed URLs. The robots policy is enforced for every HTTP fetch and browser navigation. |
| The category tree is in **server HTML**, as a JSON-LD `ItemList` of `CollectionPage` subcategories. | Discovery uses plain HTTP, which is cheap. The rich-text category descriptions contain malformed markup that breaks DOM parsers, so JSON-LD is the primary source and a regex anchor scan is the fallback. |
| Product pages are **server-rendered**, with JSON-LD `Product` and `BreadcrumbList` data and a `window.masterData` blob listing every child SKU with price, stock and manufacturer part number. | Extraction is **deterministic first**: fast, free and exact. |
| Alternatives come from Adobe Commerce Recommendations, which load lazily in the browser. | Product pages are rendered (configurable), and the recommendations response is captured. |
| Many description sections encode specs as free-text bullets ("Powder-free", "250 gloves per box (230 for x-large)"). | This is where an LLM adds real value. See below. |

The result is a **hybrid**: deterministic parsers for the roughly 95% that is structured, a browser only where content is dynamic, and an LLM only for genuinely irregular content. Each LLM output is checked against the source text.

### Why LangGraph
Per-URL processing is a small state machine with conditional loops (retry, recover, re-validate). LangGraph's `StateGraph` makes those transitions explicit, inspectable and bounded (see [`graph.py`](src/safco_agent/graph.py)). Agents stay plain Python classes with no LangGraph dependency, so the graph can be swapped for a simpler orchestrator or a queue-based worker without touching them.

---

## Agent responsibilities

| Agent | File | Responsibility | LLM? |
|---|---|---|---|
| **Navigator** | `agents/navigator.py` | Finds subcategories (JSON-LD ItemList, then anchor fallback). Renders listings and reads Algolia hits page by page via the Next control, stopping on the last page, no new products, or the limit. Falls back to rendered DOM links, then to the server-side JSON-LD product ItemList. Records category membership in `listing_hits` and feeds the `crawl_jobs` frontier. | no |
| **Page Classifier** | `agents/classifier.py` | Labels a page product, category, error or other using HTTP status, title, body classes, URL and JSON-LD. Only an *ambiguous* page goes to the LLM, and its answer must be one of the enum values. | only if ambiguous |
| **Extractor** | `agents/extractor.py` + `extractors/` | L1: JSON-LD (name, brand, sku, offer, availability, rating, breadcrumbs). L2: DOM and `masterData` (variants, mfr part numbers, option attributes, pack size and unit, specs, gallery images) plus recommendations (alternatives). L3: evidence-checked LLM fallback for missing critical fields that are **visibly present**, and LLM structuring of free-text specs. Every field carries provenance (`jsonld`, `master_data`, `dom`, `recs` or `llm`). | fallback + specs |
| **Validator** | `agents/validator.py` | Normalizes the record (whitespace, dedup of images and variants by SKU) and checks business rules: critical fields, price ranges, min ≤ max, on-domain URL. The outcome is `valid`, `valid_with_warnings`, `recoverable` (data is on the page but unparsed) or `fatal`. | no |
| **Recovery** | `agents/recovery.py` | Picks one action from a **closed set** (`rerender`, `retry_http`, `llm_extract`, `give_up`) using a deterministic policy, with an optional LLM chooser that is constrained to the currently allowed actions. Attempts and recovery budgets guarantee termination. | optional (off by default) |
| **Storage / Exporters** | `infra/storage.py`, `export/exporters.py` | Idempotent upserts keyed on stable ids, a change-detection hash, exports, and the quality report. | no |

---

## Where the LLM is used (and where it is not)

Provider: **MiniMax** (`MiniMax-M2.5`) through its OpenAI-compatible API. Any compatible endpoint works through `config.yaml`, and the key is read from `MINIMAX_API_KEY`.

1. **Spec structuring.** This was the most useful in practice. Description bullets such as "Textured fingertips / Lavender color / 250 gloves per box" become `{"Texture": "Textured fingertips", "Color": "Lavender", "Count Per Box": "250 gloves per box (230 for x-large)"}`. Rule-based `Key: value` parsing only catches a fraction of these.
2. **Extraction fallback.** When a critical field (name, SKU, price, variants) is missing *and* the page visibly contains it (for example after a layout or template change), the relevant visible text is sent with the target schema. The run does **not** call the LLM for a field that is legitimately absent. For example, some grouped products sell no children online, and the LLM isn't invoked for them.
3. **Ambiguous page classification** and, optionally, **recovery-action choice**. Both are bounded to an enum.

**Guardrails, applied to every LLM response:**
* Page text is wrapped as untrusted data, and the system prompt says to ignore instructions inside it.
* JSON-only output is required. `<think>` blocks are stripped and the JSON is parsed strictly.
* **Evidence check.** Every value must come with a verbatim quote that exists in the source text and contains the value's tokens. Anything else is rejected and counted (`llm_fallback_rejected_values`, `llm_specs_rejected`).
* Deterministic values are never overwritten by the LLM. LLM-added fields are marked `llm` in `field_provenance`.
* Responses are cached by content hash (`llm_cache`), so re-runs are free and deterministic. Usage, latency and token counts are logged per call in `llm_calls`.

Comparison mode: `crawl --no-llm` gives a deterministic-only baseline. The difference shows in `crawl_report.json` (`extraction_method`, spec counts, `llm_usage_by_task`). See [Sample output](#sample-output).

---

## Output schema

`schema/product.schema.json` is the machine-readable schema generated from the Pydantic models in [`models.py`](src/safco_agent/models.py). A **Product** is one product page or family. A **Variant** is one purchasable SKU. A family's advertised "From $X" price is never copied onto variants.

### Product (`products.json`, `products.csv`)
| Field | Type | Notes |
|---|---|---|
| `product_id` | str | `safco:<magento entity id>` (stable). Falls back to a URL hash. |
| `source_product_id` | str | Magento entity id (`input[name=product]`, same as the Algolia `objectID`) |
| `name`, `brand`, `manufacturer` | str | `manufacturer` comes from the variant data when unambiguous, otherwise the brand |
| `parent_sku` | str | Family SKU (JSON-LD `sku` / form `data-sku`) |
| `product_type` | str | `grouped` / `configurable` / `simple` (Magento page class) |
| `category_path` | [str] | Breadcrumb hierarchy, e.g. `["Dental Supplies","Dental Exam Gloves","Nitrile gloves"]` |
| `categories` / `root_categories` | [obj] / [str] | Every category whose listing showed the product, plus breadcrumb matches |
| `canonical_url` | str | Normalized product URL (no query or fragment) |
| `price_min`, `price_max`, `currency` | decimal-as-string | Taken from variant prices when available |
| `price_type` | enum | `range` (several variants), `exact` (one SKU), or `from` (advertised family price only) |
| `availability` | enum | `in_stock`, `out_of_stock`, `backorder`, `discontinued` or `unknown` |
| `description` | str | Description section as lines (bullets preserved) |
| `specifications` | {str:str} | `Key: value` lines and spec tables, plus evidence-checked LLM structuring |
| `image_urls` | [str] | Gallery images with resize parameters stripped |
| `alternative_products` | [{relation,name,sku,url}] | Site recommendations that share a category with the product |
| `rating_value`, `review_count` | num | JSON-LD AggregateRating |
| `variants` | [Variant] | See below |
| `extraction_method` | str | `deterministic` or `deterministic+llm` |
| `field_provenance` | {field: source} | `jsonld`, `master_data`, `dom`, `recs` or `llm` |
| `validation_status`, `validation_issues` | str, [str] | e.g. `valid_with_warnings` with `["missing_variants"]` |
| `scraped_at`, `first_seen_at`, `content_hash` | | The hash covers business fields only and is used for change detection |

### Variant (`variants.csv`, `products.json[].variants`)
| Field | Notes |
|---|---|
| `sku` | Safco item number (unique key together with `product_id`) |
| `manufacturer_code` | Manufacturer part number |
| `name`, `option_text` | e.g. `"Lavender Nitrile gloves x-small 250/box"`, `"X-small, 250/box"` |
| `attributes` | Only values literally present, e.g. `{"size":"x-small","pack":"250/box"}` |
| `price`, `currency` | Per-SKU price (decimal string). `null` when hidden, never guessed. |
| `availability`, `availability_label` | Normalized enum plus the site's label |
| `pack_size`, `unit_of_measure` | Parsed from the option text, e.g. `250`, `box` |

Example (trimmed, from `output/products.json`):
```json
{
  "product_id": "safco:95833", "name": "Lavender Nitrile", "brand": "Halyard", "parent_sku": "DRCAS",
  "category_path": ["Dental Supplies", "Dental Exam Gloves", "Nitrile gloves"],
  "price_min": "38.99", "price_max": "38.99", "price_type": "range", "availability": "in_stock",
  "specifications": {"Thickness": "at palm 2.0 mils; at fingertip 2.8 mils", "Color": "Lavender",
                     "Count Per Box": "250 gloves per box (230 for x-large)", "...": "..."},
  "alternative_products": [{"relation": "Safco Brand Recommendation", "name": "Safco Nitrilex Prevail Gloves",
                            "sku": "DRCCJ", "url": "https://www.safcodental.com/product/safco-nitrilex-prevail-trade-nitrile-gloves"}],
  "variants": [{"sku": "3020800", "manufacturer_code": "52816", "option_text": "X-small, 250/box",
                "attributes": {"size": "x-small", "pack": "250/box"}, "price": "38.99",
                "availability": "in_stock", "pack_size": 250, "unit_of_measure": "box"}],
  "field_provenance": {"name": "jsonld", "variants": "master_data", "specifications.Color": "llm", "...": "..."}
}
```

SQLite tables: `products`, `variants`, `categories`, `product_categories`, `listing_hits`, `crawl_jobs`, `crawl_runs`, `dead_letters`, `llm_cache`, `llm_calls` (DDL in [`storage.py`](src/safco_agent/infra/storage.py)).

---

## Sample output

`output/` holds a **full crawl of both categories** (final run `20261019T204038Z-013c83`). Files: `products.json`, `products.csv`, `variants.csv`, `products.xlsx`, `crawl_report.json`, `verification.md`, and `catalog.sqlite` (a database snapshot).

| | Sutures & surgical products | Dental Exam Gloves | Total |
|---|---|---|---|
| Product families | 55 | 93 | **148** |
| Purchasable SKU variants | 350 | 392 | **742** (741 unique; one SKU is listed under two families) |
| Subcategories discovered | 8 | 6 | 14 (+2 roots) |
| Listing pages traversed (Algolia, via Next) | 15 | 20 | 35 |

**Coverage:** for every one of the 16 listings, the number of products extracted equals the site's own `nbHits`. Examples: gloves 93/93 across 8 pages, nitrile 62/62 across 6 pages, sutures root 55/55 across 5 pages. Each listing stopped at `last_page`.

**Completeness:** 100% for product name, brand, family SKU, category path, URL, price, availability, description and images. 97.3% of products have variants; the other 4 genuinely sell no children online and are flagged `missing_variants`. Variant SKU, manufacturer code, price and availability are 100%. Pack size is 89.5% and unit 95.3%; the rest are options like "15 x 20mm", where no count is stated and none is inferred.

**Status:** 144 `valid`, 4 `valid_with_warnings`, 0 dead letters. Price type: 123 `range`, 21 `exact`, 4 `from`. Availability (variants): 586 in stock, 74 backorder, 65 special order, 16 drop-ship, 1 not purchasable.

**Accuracy:** checked against the live site in [`output/verification.md`](output/verification.md). For a seeded sample of 16 products (51 variants), every extracted value was re-fetched from the live page and found in its source: name 16/16, family SKU 16/16, variant SKUs 51/51, manufacturer codes 51/51, variant prices 51/51, breadcrumbs 48/48, brands 16/16 and **LLM spec values 55/55**. I also manually compared the rendered Products table of *Lavender Nitrile* in a browser with the extracted rows: all 5 rows matched on SKU, Mfr #, option, stock and price.

**LLM contribution** (deterministic-only vs. deterministic + LLM, from the same run via provenance):

| Metric | Deterministic only | + LLM |
|---|---|---|
| Products with ≥1 structured spec | 71 / 148 | 116 / 148 |
| Structured spec values | 93 | 707 |
| Avg specs per product | 0.63 | 4.78 |
| Spec values rejected by the evidence check | n/a | 56 |

LLM cost is small and bounded: 119 spec calls for the whole crawl, about 6 s average latency, and about 200 prompt + 450 completion tokens per call. Re-runs are free because of the cache. The extraction fallback was not needed on any page in the final run. Its value is in drift scenarios: in the live test `test_live_llm_recovers_variants_after_simulated_rename`, renaming the site's variant blob leaves the deterministic parser with nothing, and the LLM recovers all 5 SKUs with prices identical to ground truth.

**Operational evidence from the runs recorded in `crawl_runs`:**

| Run | What happened |
|---|---|
| `…201854Z` full | Killed mid-crawl after 17 products (simulated crash), leaving 3 jobs `in_progress`. Marked `crashed`. |
| `…202052Z` resume | Re-queued the 3 interrupted jobs and the 128 pending ones, skipped the 17 completed, and stored 127. |
| `…202730Z` resume `--retry-failed` | Re-processed the 4 failed URLs after the validator fix. All 4 were stored. |
| `…202806Z` full | Fresh re-crawl: **148/148 `unchanged`**, so the run is idempotent at full scale. |
| `…203412Z`, `…204038Z` full | After parser improvements, only the affected records came back `updated` (47, then 17). Change detection via `content_hash`. |

The final run took 261 s: 164 browser navigations, 0 errors, about 3.7 s median per product, concurrency 3 at 1 req/s.

---

## Failure handling

| Failure | Handling |
|---|---|
| Timeout, connection error, HTTP 5xx | tenacity retries with exponential backoff and jitter (`http.max_retries`). The graph's recovery then switches transport (browser re-render ↔ raw HTTP), bounded by `crawl.max_attempts`. |
| HTTP 429 | Honours `Retry-After` by pausing the **whole domain's** token bucket, not just the one request. |
| HTTP 403 / 404 / other 4xx | Permanent. No retry and no bypass attempt. The URL goes straight to the dead letter table. |
| robots.txt disallow | `RobotsDisallowed` is raised before any request is made. |
| Listing gives no Algolia response | Falls back to rendered DOM product links, then to the server-side JSON-LD ItemList (first page). The source is recorded per listing. |
| Pagination stalls | `pagination_timeout` stop reason is logged. Products found so far are kept. |
| Not a product page (removed product, error page) | The classifier sends it to the dead letter table with `not_a_product`. |
| Critical field missing but visible | Validator returns `recoverable`. Recovery re-renders, then tries an evidence-checked LLM extraction, then gives up. |
| Recovery exhausted, identity intact | **Degrade**: stored as `valid_with_warnings` with `unresolved_after_recovery`, so the data that was extracted isn't lost. |
| Recovery exhausted, identity broken | Dead letter: URL, stage, error type and message, attempts, timestamp. |
| Field legitimately absent (no price shown, no sellable children) | Stored as `valid_with_warnings`. Missing values are `null` and never inferred. |
| Unexpected exception in one URL | A last-resort guard in the worker dead-letters that URL. The run continues. |
| Process crash or Ctrl-C | The frontier is in SQLite. `resume` re-queues `in_progress` jobs, skips `done` ones, and marks the orphaned run as `crashed`. |
| LLM down, slow or malformed | Bounded retries and timeout. Failure is non-fatal: the deterministic result stands and the error is logged in `llm_calls`. |

---

## Production-minded design

| Concern | Implementation |
|---|---|
| **Rate limiting** | One async token bucket per domain (`rate_limit.requests_per_second`, `burst`) shared by HTTP fetches *and* browser navigations and clicks. Concurrency is bounded separately (`crawl.concurrency`). |
| **Retries** | tenacity (transport) plus graph-level recovery (strategy), both bounded. |
| **Error handling** | Typed `FetchError(transient=...)`, dead letters, per-URL isolation. |
| **Resumability / checkpointing** | `crawl_jobs` frontier (`pending → in_progress → done/failed`, attempts, last_error). `resume` continues it. Verified by killing a real full crawl mid-run (see [Sample output](#sample-output)). |
| **Deduplication** | Canonical URLs. Frontier `INSERT OR IGNORE`. Products keyed on the Magento entity id. Variants keyed on (`product_id`, `sku`). Category membership keyed on (`product_id`, `category_id`). |
| **Idempotency** | Every write is an upsert. `content_hash` over business fields reports `inserted`, `updated` or `unchanged`. Variants that disappear from a page are deleted, so the DB mirrors the source. Re-running an identical crawl gives all `unchanged`. |
| **Logging** | structlog: readable console output plus `logs/crawl.jsonl` (run_id, stage, url, event, duration, retries, extraction method, validation result). |
| **Config-driven** | `config.yaml` (Pydantic-validated). CLI flags override limits, the LLM switch and fetch mode. |
| **Secrets** | Only from env/`.env` (gitignored). `.env.example` is committed. Config snapshots in `crawl_runs` contain no secrets. In production, a secret manager injects env vars. |
| **Politeness** | robots.txt (including the site's wildcard query rules), an identifying User-Agent, about 1 req/s, and trackers and heavy assets blocked in the browser. |
| **Deployment path** | `Dockerfile` (Playwright base image plus uv lockfile) runs `safco-agent crawl` as a batch job (image build and a containerised sample crawl were verified), e.g. as a cron or Kubernetes CronJob, or an ECS scheduled task, with data/output/logs on volumes. |

---

## Scaling to full-site crawling in production

The site has about 3,900 products (Algolia `nbHits` with no category filter, and about 3,940 URLs in `products.xml`). At about 1 req/s a full crawl takes roughly 1–2 hours from one worker. That is fine for a nightly batch, but here is how it would grow:

1. **Seeds**: replace the two seeds with every top-level category from the site's `catalog.xml` sitemap or menu. Nothing else in the code is category-specific.
2. **Queue instead of a local frontier**: move `crawl_jobs` to Postgres (`SELECT … FOR UPDATE SKIP LOCKED`) or SQS/Redis Streams. Run N stateless workers (listing workers and product workers scale separately), each with a per-domain rate limit enforced centrally (for example a Redis token bucket).
3. **Postgres as the catalog store**: same schema, with `product_versions` rows on `content_hash` change to keep history.
4. **Incremental crawls**: use sitemap `lastmod` and Algolia's `algoliaLastUpdateAtCET` to re-fetch only changed products. Run a full reconciliation weekly to detect delisted products (`last_seen_at` / tombstones).
5. **Cheaper fetching**: product pages are server-rendered, so only the recommendations call needs a browser. In production, fetch products with `product_fetch_mode: http` (about 10× faster) and render a sampled subset, or refresh recommendations on a slower cadence.
6. **Scheduling and orchestration**: a cron, Airflow or Temporal job per run, with alerts on run failure.
7. **Versioned extraction adapters**: keep site-specific parsers behind an interface, and use the LLM selector-repair loop (fallback provenance spikes, then propose a fix, then human review) to keep them current.
8. **Observability**: ship the JSONL logs and run metrics to a metrics stack (Prometheus/Grafana, Datadog). Track pages/s, error rate by type, dead-letter growth, LLM cost per run, and the data-quality KPIs below.

---

## Monitoring data quality

Every run writes `output/crawl_report.json`, and `safco-agent stats` prints the same information. It contains:
* **Totals**: product families, SKU variants, unique SKUs, SKUs shared across products, dead letters.
* **Coverage**: products per root category and per subcategory, compared with what the listing reported (`listing_traversal[].nb_hits`), which catches silent truncation.
* **Field completeness** for products (name, brand, sku, category, price, availability, description, images, variants) and variants (sku, mfr code, price, availability, pack size, unit).
* **Validation status and issue histogram** (e.g. `missing_variants`, `variant_price_missing`).
* **Extraction method mix and LLM usage** (calls, cache hits, failures, latency, tokens, rejected values). A sudden rise in LLM fallbacks means the site layout changed.
* **Job and dead-letter breakdown** by stage and error type.

In production, these become alerting thresholds, for example "price completeness dropped by more than 5%", "nb_hits differs from extracted count", or "dead letters exceed 2%". They would sit alongside a weekly **golden set** of hand-verified products that is diffed field by field. The manual spot-check done for this submission is described in [Sample output](#sample-output).

---

## Limitations

* **Site-specific adapters.** The extractors target Safco's Magento/Hyvä templates: `masterData`, `section#description` and the JSON-LD shapes. JSON-LD keeps the core fields portable, and the LLM fallback covers drift, but another site needs its own L2 adapter.
* **Variant data comes from one embedded blob.** The grouped "Products" table is rendered lazily, client-side, from `window.masterData`. The scraper reads that blob directly, so the visible table adds nothing new. A manual browser check of *Lavender Nitrile* matched all 5 rendered rows exactly. If the blob changes shape, the browser render scrolls the table into view so the LLM fallback can extract variants from the visible rows. If the table is empty, the product is stored with `missing_variants` and nothing is invented. Four real products (e.g. *R.T.R. Membrane*) genuinely sell no children online and are flagged this way.
* **Configurable (swatch) products are unverified.** Every product in the two categories is a Magento *grouped* product, and a simple product is handled as a single SKU. Magento `configurable` products (`spConfig` / swatches) exist elsewhere on the site, but there was nothing to verify a parser against here, so none was built. They would currently surface as `missing_variants`, and the LLM path applies if the options are visible.
* **Prices are anonymous public prices.** Customer-group, tier and promo pricing ("Buy 5 get 5 free") are not modelled. Promo text is ignored.
* **Alternative products are site recommendations filtered by shared category.** They are not curated equivalence data. The `relation` field records the source unit, so treat these as "suggested substitutes".
* **Browser rendering per product is the bottleneck** (about 3–4 s per page). It is used only to capture recommendations. With `product_fetch_mode: http`, the same data minus alternatives comes about 10× faster.
* **LLM specs are only as precise as the source text.** Values are guaranteed to be quoted from the page, but spec *names* are model-chosen and not normalized to a controlled vocabulary. In production you'd map them to a taxonomy.
* **Sitemap discovery is designed but not implemented.** It would be the last-resort source when no listing source works. It wasn't needed because the Algolia path worked for every listing.
* **Single process.** The SQLite frontier and an in-process rate limiter mean one machine, by design for a POC. See the scaling section for the distributed version.
* **Selector-repair suggestions (optional P2) are not implemented.** The hooks exist (fallback provenance and rejected-value counters signal drift), but no automated suggestion loop was built.

---

## Testing

```bash
uv run pytest                 # offline unit + integration tests (saved real pages, fake LLM)
RUN_LIVE=1 uv run pytest -m live   # opt-in: real site pagination + real LLM call
```

The tests cover:
* JSON-LD, DOM, `masterData` and recommendation extraction on saved real pages: grouped multi-SKU, single-SKU, and grouped with no sellable children.
* Category discovery, Algolia response parsing and the static fallback.
* Price, pack, availability and SKU normalization.
* Validator outcomes, the evidence check, the LLM fallback on a **simulated layout change** (structured data removed, visible content intact), rejection of fabricated LLM values, and spec structuring that drops unquoted values.
* The recovery policy's bounds.
* Idempotent upserts, variant removal and the content hash.
* Frontier deduplication, crash recovery and rescheduling.
* Exports.
* End-to-end LangGraph routing: transient-failure retry, permanent failure, the LLM recovery path, degradation, and non-product pages.

---

## Project layout

```
src/safco_agent/
  cli.py               Typer CLI (crawl | resume | export | stats)
  orchestrator.py      phases, workers, run metrics
  graph.py, state.py   LangGraph per-product workflow
  config.py, models.py
  agents/              navigator, classifier, extractor, validator, recovery
  extractors/          jsonld, magento_dom, variants, recommendations
  infra/               http, browser, rate_limit, storage, llm, logging
  export/exporters.py  JSON/CSV/XLSX + quality report
scripts/               feasibility spike scripts (listing + product page)
schema/                product.schema.json
tests/                 pytest suite + saved real page fixtures
output/                committed sample dataset (full crawl of both categories)
```
