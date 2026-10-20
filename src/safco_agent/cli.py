"""Command-line interface: crawl | resume | export | stats."""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import typer

from .config import load_config
from .infra.logging import configure_logging

app = typer.Typer(add_completion=False, help="Agent-based Safco Dental product scraper (POC).")


def _setup(config: str):
    cfg = load_config(config)
    configure_logging(cfg.logging.level, cfg.logging.json_file)
    if sys.platform == "win32":
        # Windows consoles default to a legacy code page; product names contain (R)/(TM) symbols.
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    return cfg


def _summary(metrics: dict) -> None:
    c = metrics.get("counters", {})
    typer.echo(json.dumps({"run_id": metrics.get("run_id"), "elapsed_s": metrics.get("elapsed_s"), **c,
                           "llm": {k: metrics["llm"].get(k) for k in ("available", "calls", "cache_hits", "failures")}},
                          indent=2))


@app.command()
def crawl(
    config: str = typer.Option("config.yaml", help="Path to YAML config"),
    max_products_per_category: Optional[int] = typer.Option(
        None, "--max-products-per-category", "-n", help="Sample mode: cap products per seed category"),
    full: bool = typer.Option(False, "--full", help="Full mode: no product limit (overrides config)"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Deterministic-only run (LLM steps disabled)"),
    fetch_mode: Optional[str] = typer.Option(None, help="Product fetch mode: browser | http"),
):
    """Start a new crawl of the configured seed categories."""
    cfg = _setup(config)
    if full:
        cfg.crawl.max_products_per_category = None
    if fetch_mode:
        cfg.crawl.product_fetch_mode = fetch_mode
    from .orchestrator import Orchestrator
    limit = None if full else max_products_per_category
    orch = Orchestrator(cfg, max_products_per_category=limit, llm_enabled=False if no_llm else None)
    _summary(asyncio.run(orch.run()))


@app.command()
def resume(
    config: str = typer.Option("config.yaml", help="Path to YAML config"),
    retry_failed: bool = typer.Option(False, help="Also retry URLs that previously failed"),
    no_llm: bool = typer.Option(False, "--no-llm"),
):
    """Continue an interrupted crawl from the SQLite frontier (completed URLs are skipped)."""
    cfg = _setup(config)
    from .orchestrator import Orchestrator
    orch = Orchestrator(cfg, resume=True, retry_failed=retry_failed, llm_enabled=False if no_llm else None)
    _summary(asyncio.run(orch.run()))


@app.command()
def export(
    config: str = typer.Option("config.yaml"),
    format: str = typer.Option("json,csv,xlsx", "--format", help="Comma-separated: json,csv,xlsx"),
    output_dir: Optional[str] = typer.Option(None, help="Override output directory"),
):
    """Export stored products and variants (and refresh crawl_report.json)."""
    cfg = _setup(config)
    from .export.exporters import export_all
    from .infra.storage import Storage
    storage = Storage(cfg.storage.db_path)
    last = storage.latest_run()
    metrics = json.loads(last["metrics_json"]) if last and last["metrics_json"] else None
    paths = export_all(storage, output_dir or cfg.storage.output_dir, metrics,
                       tuple(f.strip() for f in format.split(",")))
    storage.close()
    typer.echo(json.dumps(paths, indent=2))


@app.command()
def stats(config: str = typer.Option("config.yaml")):
    """Print crawl and data-quality metrics from the database."""
    cfg = _setup(config)
    from .export.exporters import product_records, quality_report
    from .infra.storage import Storage
    storage = Storage(cfg.storage.db_path)
    last = storage.latest_run()
    report = quality_report(storage, product_records(storage), None)
    storage.close()
    view = {k: report[k] for k in ("totals", "jobs", "products_per_root_category", "products_per_category",
                                   "field_completeness_products", "field_completeness_variants", "sku_coverage",
                                   "validation_status", "validation_issues", "extraction_method",
                                   "llm_usage_by_task")}
    view["last_run"] = {k: last[k] for k in ("run_id", "mode", "status", "started_at", "ended_at")} if last else None
    typer.echo(json.dumps(view, indent=2, default=str))


if __name__ == "__main__":
    app()
