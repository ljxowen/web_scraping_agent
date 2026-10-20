"""Exports from SQLite (source of truth) to JSON / CSV / XLSX, plus the data-quality crawl report."""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from ..infra.storage import Storage
from ..models import utcnow

PRODUCT_CSV_COLUMNS = [
    "product_id", "name", "brand", "manufacturer", "parent_sku", "product_type", "root_category",
    "category_path", "categories", "canonical_url", "price_min", "price_max", "price_type", "currency",
    "availability", "variant_count", "variant_skus", "rating_value", "review_count", "description",
    "specifications", "extra_specifications", "image_urls", "alternative_products", "extraction_method", "validation_status",
    "validation_issues", "scraped_at", "content_hash",
]
VARIANT_CSV_COLUMNS = [
    "variant_id", "product_id", "product_name", "product_url", "sku", "manufacturer_code", "name", "option_text",
    "attributes", "price", "currency", "availability", "availability_label", "pack_size", "unit_of_measure",
]

CRITICAL_FIELDS = ["name", "brand", "parent_sku", "category_path", "canonical_url", "price_min", "availability",
                   "description", "image_urls", "variants"]
VARIANT_FIELDS = ["sku", "manufacturer_code", "price", "availability", "pack_size", "unit_of_measure"]


def _loads(s, default):
    try:
        return json.loads(s) if s else default
    except json.JSONDecodeError:
        return default


def product_records(storage: Storage) -> list[dict]:
    cats = {c["category_id"]: dict(c) for c in storage.categories()}
    out = []
    for row in storage.iter_products():
        pid = row["product_id"]
        cat_rows = storage.product_category_rows(pid)
        roots = sorted({cats[c["root_category_id"]]["name"] for c in cat_rows
                        if c["root_category_id"] in cats})
        out.append({
            "product_id": pid,
            "source_product_id": row["source_product_id"],
            "name": row["name"],
            "brand": row["brand"],
            "manufacturer": row["manufacturer"],
            "parent_sku": row["parent_sku"],
            "product_type": row["product_type"],
            "root_categories": roots,
            "category_path": _loads(row["category_path_json"], []),
            "categories": [{"category_id": c["category_id"], "name": c["name"], "url": c["canonical_url"]}
                           for c in cat_rows],
            "canonical_url": row["canonical_url"],
            "price_min": row["price_min"],
            "price_max": row["price_max"],
            "price_type": row["price_type"],
            "currency": row["currency"],
            "availability": row["availability"],
            "rating_value": row["rating_value"],
            "review_count": row["review_count"],
            "description": row["description"],
            "specifications": _loads(row["specifications_json"], {}),
            "extra_specifications": _loads(row["extra_specifications_json"], {}),
            "image_urls": _loads(row["image_urls_json"], []),
            "alternative_products": _loads(row["alternative_products_json"], []),
            "variants": [
                {"sku": v["sku"], "manufacturer_code": v["manufacturer_code"], "name": v["name"],
                 "option_text": v["option_text"], "attributes": _loads(v["attributes_json"], {}),
                 "price": v["price"], "currency": v["currency"], "availability": v["availability"],
                 "availability_label": v["availability_label"], "pack_size": v["pack_size"],
                 "unit_of_measure": v["unit_of_measure"]}
                for v in storage.variants_for(pid)
            ],
            "extraction_method": row["extraction_method"],
            "field_provenance": _loads(row["field_provenance_json"], {}),
            "validation_status": row["validation_status"],
            "validation_issues": _loads(row["validation_issues_json"], []),
            "scraped_at": row["scraped_at"],
            "first_seen_at": row["first_seen_at"],
            "content_hash": row["content_hash"],
        })
    return out


def _flat(rec: dict) -> dict:
    return {
        **{k: rec.get(k) for k in PRODUCT_CSV_COLUMNS},
        "root_category": "; ".join(rec["root_categories"]),
        "category_path": " > ".join(rec["category_path"]),
        "categories": "; ".join(c["name"] for c in rec["categories"]),
        "variant_count": len(rec["variants"]),
        "variant_skus": "; ".join(v["sku"] for v in rec["variants"]),
        "specifications": json.dumps(rec["specifications"], ensure_ascii=False),
        "extra_specifications": json.dumps(rec["extra_specifications"], ensure_ascii=False),
        "image_urls": "; ".join(rec["image_urls"]),
        "alternative_products": "; ".join(f"{a.get('name')} ({a.get('sku')})" for a in rec["alternative_products"]),
        "validation_issues": "; ".join(rec["validation_issues"]),
    }


def quality_report(storage: Storage, records: list[dict], run_metrics: dict | None) -> dict:
    n = len(records)
    filled = Counter()
    for r in records:
        for f in CRITICAL_FIELDS:
            if r.get(f) not in (None, "", [], {}):
                filled[f] += 1
    variants = [v for r in records for v in r["variants"]]
    v_filled = Counter()
    for v in variants:
        for f in VARIANT_FIELDS:
            if v.get(f) not in (None, ""):
                v_filled[f] += 1
    per_root: dict[str, dict] = defaultdict(lambda: {"products": 0, "variants": 0})
    for r in records:
        for root in r["root_categories"] or ["(unassigned)"]:
            per_root[root]["products"] += 1
            per_root[root]["variants"] += len(r["variants"])
    per_cat = {row["name"]: row["n"] for row in storage.query(
        "SELECT c.name, COUNT(DISTINCT pc.product_id) n FROM product_categories pc JOIN categories c "
        "USING(category_id) GROUP BY c.category_id ORDER BY c.depth, c.name")}
    listings = [
        {"category": row["canonical_url"], **_loads(row["meta_json"], {})}
        for row in storage.query("SELECT canonical_url, meta_json FROM crawl_jobs WHERE page_type='category'")
    ]
    llm_rows = storage.query(
        "SELECT task, COUNT(*) calls, SUM(cached) cached, SUM(ok) ok, ROUND(AVG(CASE WHEN cached=0 THEN latency_s END),2) "
        "avg_latency_s, SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens "
        "FROM llm_calls GROUP BY task")
    sku_counts = Counter(v["sku"] for v in variants)
    canon_keys = Counter(k for r in records for k in r["specifications"])
    extra_keys = Counter(k for r in records for k in r.get("extra_specifications", {}))
    spec_coverage: dict[str, dict] = {}
    for root in per_root:
        rs = [r for r in records if root in r["root_categories"]]
        keys = Counter(k for r in rs for k in r["specifications"])
        spec_coverage[root] = {k: f"{c}/{len(rs)}" for k, c in keys.most_common(10)}
    return {
        "generated_at": utcnow(),
        "run": run_metrics or {},
        "totals": {
            "product_families": n,
            "sku_variants": len(variants),
            "unique_skus": len(sku_counts),
            "skus_shared_across_products": sorted([s for s, c in sku_counts.items() if c > 1]),
            "dead_letters": len(storage.dead_letters()),
        },
        "jobs": storage.job_counts(),
        "products_per_root_category": dict(per_root),
        "products_per_category": per_cat,
        "listing_traversal": listings,
        "field_completeness_products": {f: round(filled[f] / n, 3) if n else None for f in CRITICAL_FIELDS},
        "field_completeness_variants": {f: round(v_filled[f] / len(variants), 3) if variants else None
                                        for f in VARIANT_FIELDS},
        "sku_coverage": round(sum(1 for r in records if r["variants"] or r["parent_sku"]) / n, 3) if n else None,
        "validation_status": dict(Counter(r["validation_status"] for r in records)),
        "validation_issues": dict(Counter(i.split(":")[0] for r in records for i in r["validation_issues"])),
        "extraction_method": dict(Counter(r["extraction_method"] for r in records)),
        "price_type": dict(Counter(r["price_type"] for r in records)),
        "specification_quality": {
            "canonical_keys_used": len(canon_keys),
            "canonical_values": sum(canon_keys.values()),
            "extra_keys_used": len(extra_keys),
            "extra_values": sum(extra_keys.values()),
            "products_with_canonical_specs": sum(1 for r in records if r["specifications"]),
            "top_canonical_keys": dict(canon_keys.most_common(15)),
            "coverage_by_root_category": spec_coverage,
        },
        "llm_usage_by_task": [dict(r) for r in llm_rows],
        "dead_letters": [dict(r) for r in storage.dead_letters()],
    }


def export_all(storage: Storage, output_dir: str, run_metrics: dict | None = None,
               formats: tuple[str, ...] = ("json", "csv", "xlsx")) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    records = product_records(storage)
    paths: dict[str, str] = {}
    if "json" in formats:
        p = out / "products.json"
        p.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        paths["products.json"] = str(p)
    variant_rows = [
        {**{k: v[k] for k in v.keys() if k in VARIANT_CSV_COLUMNS}, "attributes": v["attributes_json"]}
        for v in storage.all_variants()
    ]
    if "csv" in formats:
        p = out / "products.csv"
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=PRODUCT_CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in records:
                w.writerow(_flat(r))
        paths["products.csv"] = str(p)
        p = out / "variants.csv"
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=VARIANT_CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(variant_rows)
        paths["variants.csv"] = str(p)
    if "xlsx" in formats:
        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "products"
            ws.append(PRODUCT_CSV_COLUMNS)
            for r in records:
                fr = _flat(r)
                ws.append([fr.get(c) for c in PRODUCT_CSV_COLUMNS])
            ws2 = wb.create_sheet("variants")
            ws2.append(VARIANT_CSV_COLUMNS)
            for v in variant_rows:
                ws2.append([v.get(c) for c in VARIANT_CSV_COLUMNS])
            p = out / "products.xlsx"
            wb.save(p)
            paths["products.xlsx"] = str(p)
        except ImportError:
            pass
    report = quality_report(storage, records, run_metrics)
    p = out / "crawl_report.json"
    p.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    paths["crawl_report.json"] = str(p)
    return paths
