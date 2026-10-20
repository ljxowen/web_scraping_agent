"""Independent spot-check of scraped data against the live source pages + LLM contribution analysis.

For a seeded random sample of products per root category, re-fetch the product page (plain HTTP,
~1 req/s) and check that every extracted identity/price value literally occurs in the page source.
Writes output/verification.md. Usage: uv run python scripts/verify_sample.py [per_category=8]
"""
from __future__ import annotations

import html as htmllib
import json
import random
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

OUT = Path("output")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip().lower()


def main(per_category: int = 8) -> None:
    products = json.loads((OUT / "products.json").read_text(encoding="utf-8"))
    by_root: dict[str, list[dict]] = defaultdict(list)
    for p in products:
        for r in p["root_categories"]:
            by_root[r].append(p)
    rng = random.Random(42)
    sample = []
    for root, items in sorted(by_root.items()):
        multi = [p for p in items if len(p["variants"]) > 1]
        rest = [p for p in items if len(p["variants"]) <= 1]
        k_multi = min(len(multi), max(1, per_category * 3 // 4))  # bias towards multi-variant families
        picked = rng.sample(multi, k_multi) + rng.sample(rest, min(len(rest), per_category - k_multi))
        sample += [(root, p) for p in picked]

    rows, totals = [], defaultdict(lambda: [0, 0])
    client = httpx.Client(headers={"User-Agent": "SafcoCatalogPOC/0.1 verification"}, timeout=30,
                          follow_redirects=True)
    for root, p in sample:
        time.sleep(1.0)
        page = client.get(p["canonical_url"]).text
        src = norm(page)
        checks: dict[str, tuple[int, int]] = {}

        def check(name: str, values: list[str]) -> None:
            values = [v for v in values if v]
            ok = sum(1 for v in values if norm(v) in src)
            checks[name] = (ok, len(values))
            totals[name][0] += ok
            totals[name][1] += len(values)

        check("name", [p["name"]])
        check("parent_sku", [p["parent_sku"]])
        check("variant_skus", [v["sku"] for v in p["variants"]])
        check("mfr_codes", [v["manufacturer_code"] for v in p["variants"]])
        # prices appear in masterData as e.g. "38.990000"; compare on the 2-decimal prefix
        check("variant_prices", [v["price"] for v in p["variants"] if v["price"]])
        check("breadcrumbs", p["category_path"])
        check("brand", [p["brand"]])
        check("llm_spec_values", [p["specifications"][k.split(".", 1)[1]] for k, v in p["field_provenance"].items()
                                  if k.startswith("specifications.") and v == "llm"
                                  and k.split(".", 1)[1] in p["specifications"]])
        rows.append((root, p, checks))
    client.close()

    # LLM contribution: provenance makes the deterministic-only baseline exact (LLM never overwrites).
    det_specs = llm_specs = det_only_products_with_specs = with_specs = 0
    for p in products:
        prov = p["field_provenance"]
        n_llm = sum(1 for k, v in prov.items() if k.startswith("specifications.") and v == "llm")
        n_det = len(p["specifications"]) - n_llm
        det_specs += n_det
        llm_specs += n_llm
        det_only_products_with_specs += n_det > 0
        with_specs += len(p["specifications"]) > 0
    report = json.loads((OUT / "crawl_report.json").read_text(encoding="utf-8"))
    ex = report.get("run", {}).get("agents", {}).get("extractor", {})
    usage = report.get("llm_usage_by_task", [])

    lines = ["# Verification against live source pages", "",
             f"Seeded random sample (seed 42) of up to {per_category} products per root category, biased towards "
             "multi-variant families. Each extracted value is checked to occur in the raw HTML of the live page "
             f"(re-fetched {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}).", "",
             "| Category | Product | Variants | name | sku | variant SKUs | mfr codes | variant prices | breadcrumbs | brand | LLM spec values |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for root, p, c in rows:
        cell = lambda k: f"{c[k][0]}/{c[k][1]}" if c[k][1] else "n/a"
        lines.append(f"| {root} | [{p['name']}]({p['canonical_url']}) | {len(p['variants'])} | " +
                     " | ".join(cell(k) for k in ("name", "parent_sku", "variant_skus", "mfr_codes",
                                                  "variant_prices", "breadcrumbs", "brand", "llm_spec_values")) + " |")
    lines += ["", "**Totals (values found in source / values checked):** " +
              ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in totals.items()), "",
              "## Deterministic-only vs. deterministic + LLM", "",
              "LLM output never overwrites deterministic values and is provenance-tagged, so the deterministic-only "
              "baseline can be read from the same run.", "",
              "| Metric | Deterministic only | + LLM |", "|---|---|---|",
              f"| Products with ≥1 structured spec | {det_only_products_with_specs}/{len(products)} | "
              f"{with_specs}/{len(products)} |",
              f"| Structured spec values (total) | {det_specs} | {det_specs + llm_specs} |",
              f"| Avg specs per product | {det_specs / len(products):.2f} | {(det_specs + llm_specs) / len(products):.2f} |",
              "", f"LLM spec values rejected by the evidence check this run: {ex.get('llm_specs_rejected', 'n/a')} "
              f"(fallback values rejected: {ex.get('llm_fallback_rejected_values', 'n/a')}).", "",
              "LLM usage by task (all runs in the DB):", "", "```json", json.dumps(usage, indent=1), "```"]
    (OUT / "verification.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
