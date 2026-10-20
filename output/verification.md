# Verification against live source pages

Seeded random sample (seed 42) of up to 8 products per root category, biased towards multi-variant families. Each extracted value is checked to occur in the raw HTML of the live page (re-fetched 2026-10-20 04:53 UTC).

| Category | Product | Variants | name | sku | variant SKUs | mfr codes | variant prices | breadcrumbs | brand | LLM spec values |
|---|---|---|---|---|---|---|---|---|---|---|
| Dental Exam Gloves | [Aurelia Sonic 100](https://www.safcodental.com/product/aurelia-reg-sonic-reg-100) | 5 | 1/1 | 1/1 | 5/5 | 5/5 | 5/5 | 3/3 | 1/1 | 7/7 |
| Dental Exam Gloves | [Alasta Shimmer Pink Nitrile](https://www.safcodental.com/product/alasta-sup-reg-sup-shimmer-pink-nitrile) | 4 | 1/1 | 1/1 | 4/4 | 4/4 | 4/4 | 3/3 | 1/1 | 11/11 |
| Dental Exam Gloves | [Cyntek](https://www.safcodental.com/product/cyntek-reg) | 5 | 1/1 | 1/1 | 5/5 | 5/5 | 5/5 | 3/3 | 1/1 | 8/8 |
| Dental Exam Gloves | [Compac Nitrile Gloves](https://www.safcodental.com/product/compac-nitrile) | 5 | 1/1 | 1/1 | 5/5 | 5/5 | 5/5 | 3/3 | 1/1 | 1/1 |
| Dental Exam Gloves | [Black Maxx Thin with Glide Assist Technology](https://www.safcodental.com/product/black-maxx-sup-reg-sup) | 5 | 1/1 | 1/1 | 5/5 | 5/5 | 5/5 | 3/3 | 1/1 | 5/5 |
| Dental Exam Gloves | [Aurelia Protege](https://www.safcodental.com/product/aurelia-sup-reg-sup-prot-eacute-g-eacute-sup-reg-sup) | 5 | 1/1 | 1/1 | 5/5 | 5/5 | 5/5 | 3/3 | 1/1 | 5/5 |
| Dental Exam Gloves | [ProDerm](https://www.safcodental.com/product/proderm-trade) | 1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 3/3 | 1/1 | 7/7 |
| Dental Exam Gloves | [Contour](https://www.safcodental.com/product/contour-reg) | 0 | 1/1 | 1/1 | n/a | n/a | n/a | 3/3 | 1/1 | 4/4 |
| Sutures & surgical products | [Surgifoam](https://www.safcodental.com/product/surgifoam-reg) | 2 | 1/1 | 1/1 | 2/2 | 2/2 | 2/2 | 3/3 | 1/1 | 5/5 |
| Sutures & surgical products | [Sodium chloride 0.9% for injection - B Braun](https://www.safcodental.com/product/sodium-chloride-0-9-for-injection-b-braun) | 3 | 1/1 | 1/1 | 3/3 | 3/3 | 3/3 | 3/3 | 1/1 | 2/2 |
| Sutures & surgical products | [Disposable scalpels](https://www.safcodental.com/product/disposable-scalpels) | 4 | 1/1 | 1/1 | 4/4 | 4/4 | 4/4 | 3/3 | 1/1 | n/a |
| Sutures & surgical products | [Sterile water for injection - B Braun](https://www.safcodental.com/product/sterile-water-for-injection-b-braun) | 4 | 1/1 | 1/1 | 4/4 | 4/4 | 4/4 | 3/3 | 1/1 | 3/3 |
| Sutures & surgical products | [Periacryl 90](https://www.safcodental.com/product/periacryl-reg-90) | 2 | 1/1 | 1/1 | 2/2 | 2/2 | 2/2 | 3/3 | 1/1 | n/a |
| Sutures & surgical products | [Coldstar Disposable Instant Non-Insulated Cold Pack](https://www.safcodental.com/product/coldstar-disposable-instant-non-insulated-cold-pack) | 4 | 1/1 | 1/1 | 4/4 | 4/4 | 4/4 | 3/3 | 1/1 | n/a |
| Sutures & surgical products | [ActCel](https://www.safcodental.com/product/actcel) | 1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 3/3 | 1/1 | n/a |
| Sutures & surgical products | [Barricaid](https://www.safcodental.com/product/barricaid-reg) | 1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 3/3 | 1/1 | n/a |

**Totals (values found in source / values checked):** name 16/16, parent_sku 16/16, variant_skus 51/51, mfr_codes 51/51, variant_prices 51/51, breadcrumbs 48/48, brand 16/16, llm_spec_values 58/58

## Deterministic-only vs. deterministic + LLM

LLM output never overwrites deterministic values and is provenance-tagged, so the deterministic-only baseline can be read from the same run.

| Metric | Deterministic only | + LLM |
|---|---|---|
| Products with ≥1 structured spec | 71/148 | 118/148 |
| Structured spec values (total) | 92 | 801 |
| Avg specs per product | 0.62 | 5.41 |

Spec names: 27 canonical names in `specifications`, 5 long-tail names in `extra_specifications`.

LLM spec values rejected by the evidence check this run: 55 (fallback values rejected: 0).

LLM usage by task (all runs in the DB):

```json
[
 {
  "task": "extract_fallback",
  "calls": 4,
  "cached": 0,
  "ok": 4,
  "avg_latency_s": 8.25,
  "prompt_tokens": 2824,
  "completion_tokens": 2136
 },
 {
  "task": "structure_specs",
  "calls": 1077,
  "cached": 839,
  "ok": 1077,
  "avg_latency_s": 7.08,
  "prompt_tokens": 84651,
  "completion_tokens": 115818
 }
]
```
