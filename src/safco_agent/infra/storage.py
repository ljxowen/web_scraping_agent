"""SQLite persistence: catalog tables (source of truth), crawl frontier, runs, dead letters, LLM cache.

All writes are upserts keyed on stable identities, so re-running a crawl is idempotent.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from ..models import Category, Product, utcnow

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS categories (
    category_id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    parent_category_id TEXT REFERENCES categories(category_id),
    root_category_id TEXT,
    depth INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    source_product_id TEXT,
    canonical_url TEXT NOT NULL UNIQUE,
    name TEXT,
    brand TEXT,
    manufacturer TEXT,
    parent_sku TEXT,
    product_type TEXT,
    category_path_json TEXT,
    description TEXT,
    price_min TEXT,
    price_max TEXT,
    price_type TEXT,
    currency TEXT,
    availability TEXT,
    rating_value REAL,
    review_count INTEGER,
    specifications_json TEXT,
    extra_specifications_json TEXT,
    image_urls_json TEXT,
    alternative_products_json TEXT,
    extraction_method TEXT,
    field_provenance_json TEXT,
    validation_status TEXT,
    validation_issues_json TEXT,
    scraped_at TEXT,
    first_seen_at TEXT,
    content_hash TEXT,
    last_run_id TEXT
);

CREATE TABLE IF NOT EXISTS variants (
    variant_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    sku TEXT NOT NULL,
    manufacturer_code TEXT,
    name TEXT,
    option_text TEXT,
    attributes_json TEXT,
    price TEXT,
    currency TEXT,
    availability TEXT,
    availability_label TEXT,
    pack_size INTEGER,
    unit_of_measure TEXT,
    source_variant_id TEXT,
    UNIQUE(product_id, sku)
);
CREATE INDEX IF NOT EXISTS ix_variants_sku ON variants(sku);

CREATE TABLE IF NOT EXISTS product_categories (
    product_id TEXT NOT NULL,
    category_id TEXT NOT NULL REFERENCES categories(category_id),
    source TEXT,
    PRIMARY KEY (product_id, category_id)
);

-- Raw listing observations: which category listing showed which product URL (membership evidence).
CREATE TABLE IF NOT EXISTS listing_hits (
    product_url TEXT NOT NULL,
    category_id TEXT NOT NULL REFERENCES categories(category_id),
    source TEXT,
    source_product_id TEXT,
    seen_at TEXT,
    PRIMARY KEY (product_url, category_id)
);

CREATE TABLE IF NOT EXISTS crawl_jobs (
    canonical_url TEXT PRIMARY KEY,
    page_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | in_progress | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    category_id TEXT,
    meta_json TEXT,
    discovered_at TEXT,
    last_attempt_at TEXT,
    completed_at TEXT,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON crawl_jobs(page_type, status);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id TEXT PRIMARY KEY,
    config_json TEXT,
    mode TEXT,
    started_at TEXT,
    ended_at TEXT,
    status TEXT,
    metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS dead_letters (
    url TEXT PRIMARY KEY,
    stage TEXT,
    error_type TEXT,
    error_message TEXT,
    attempts INTEGER,
    last_attempt_at TEXT,
    run_id TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    task TEXT,
    model TEXT,
    response_json TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    task TEXT,
    url TEXT,
    model TEXT,
    cached INTEGER,
    ok INTEGER,
    latency_s REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    error TEXT,
    created_at TEXT
);
"""


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


class Storage:
    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit; explicit txns below
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Additive column migrations for databases created by older versions."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(products)")}
        if "extra_specifications_json" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN extra_specifications_json TEXT")

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- runs
    def start_run(self, run_id: str, config: dict, mode: str) -> None:
        self.conn.execute(
            "INSERT INTO crawl_runs(run_id, config_json, mode, started_at, status) VALUES (?,?,?,?, 'running')",
            (run_id, _j(config), mode, utcnow()),
        )

    def finish_run(self, run_id: str, status: str, metrics: dict) -> None:
        self.conn.execute(
            "UPDATE crawl_runs SET ended_at=?, status=?, metrics_json=? WHERE run_id=?",
            (utcnow(), status, _j(metrics), run_id),
        )

    def latest_run(self, status: str | None = None) -> sqlite3.Row | None:
        q = "SELECT * FROM crawl_runs" + (" WHERE status=?" if status else "") + " ORDER BY started_at DESC LIMIT 1"
        return self.conn.execute(q, (status,) if status else ()).fetchone()

    # ---------------------------------------------------------- categories
    def upsert_category(self, c: Category) -> None:
        self.conn.execute(
            """INSERT INTO categories(category_id, canonical_url, name, parent_category_id, root_category_id, depth, discovered_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(category_id) DO UPDATE SET name=excluded.name,
                 parent_category_id=COALESCE(excluded.parent_category_id, categories.parent_category_id),
                 root_category_id=excluded.root_category_id, depth=excluded.depth""",
            (c.category_id, c.canonical_url, c.name, c.parent_category_id, c.root_category_id, c.depth, c.discovered_at),
        )

    def categories(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM categories ORDER BY root_category_id, depth, name").fetchall()

    def link_product_category(self, product_id: str, category_id: str, source: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO product_categories(product_id, category_id, source) VALUES (?,?,?)",
            (product_id, category_id, source),
        )
        return cur.rowcount > 0

    def record_listing_hit(self, url: str, category_id: str, source: str, source_product_id: str | None) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO listing_hits(product_url, category_id, source, source_product_id, seen_at) "
            "VALUES (?,?,?,?,?)", (url, category_id, source, source_product_id, utcnow()))
        return cur.rowcount > 0

    def listing_categories_for(self, url: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM listing_hits WHERE product_url=?", (url,)).fetchall()

    def category_by_name(self, name: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM categories WHERE lower(name)=lower(?)", (name,)).fetchall()

    # ------------------------------------------------------------ frontier
    def enqueue(self, url: str, page_type: str, run_id: str, category_id: str | None = None,
                meta: dict | None = None) -> bool:
        """Insert a job if unseen. Returns True when newly added (dedup of discovered URLs)."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO crawl_jobs(canonical_url, page_type, status, category_id, meta_json, discovered_at, run_id)
               VALUES (?,?,'pending',?,?,?,?)""",
            (url, page_type, category_id, _j(meta or {}), utcnow(), run_id),
        )
        return cur.rowcount > 0

    def schedule(self, url: str, page_type: str, run_id: str, category_id: str | None = None,
                 meta: dict | None = None) -> str:
        """Put a URL in scope for this run. Returns 'new' | 'rescheduled' | 'duplicate' (already in this run)."""
        row = self.job(url)
        if row is None:
            self.enqueue(url, page_type, run_id, category_id, meta)
            return "new"
        if row["run_id"] == run_id and row["status"] in ("pending", "in_progress", "done", "failed"):
            return "duplicate"
        self.conn.execute(
            "UPDATE crawl_jobs SET status='pending', attempts=0, last_error=NULL, run_id=?, "
            "category_id=COALESCE(category_id, ?) WHERE canonical_url=?", (run_id, category_id, url))
        return "rescheduled"

    def park_jobs(self, page_type: str) -> None:
        """Fresh run: take previous jobs out of the queue; only URLs re-discovered in scope get scheduled."""
        self.conn.execute("UPDATE crawl_jobs SET status='idle' WHERE page_type=?", (page_type,))

    def reset_for_new_run(self, page_type: str, run_id: str) -> None:
        """A fresh (non-resume) crawl re-processes every known URL; upserts keep it idempotent."""
        self.conn.execute(
            "UPDATE crawl_jobs SET status='pending', attempts=0, last_error=NULL, run_id=? WHERE page_type=?",
            (run_id, page_type),
        )

    def recover_in_progress(self) -> int:
        """Jobs interrupted mid-processing (crash / Ctrl-C) go back to pending on resume."""
        return self.conn.execute(
            "UPDATE crawl_jobs SET status='pending' WHERE status='in_progress'").rowcount

    def pending_jobs(self, page_type: str, max_attempts: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM crawl_jobs WHERE page_type=? AND status='pending' AND attempts < ? ORDER BY discovered_at",
            (page_type, max_attempts),
        ).fetchall()

    def mark_job(self, url: str, status: str, error: str | None = None, bump_attempt: bool = False) -> None:
        now = utcnow()
        self.conn.execute(
            f"""UPDATE crawl_jobs SET status=?, last_error=?, last_attempt_at=?,
                completed_at=CASE WHEN ?='done' THEN ? ELSE completed_at END
                {', attempts=attempts+1' if bump_attempt else ''} WHERE canonical_url=?""",
            (status, error, now, status, now, url),
        )

    def job(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM crawl_jobs WHERE canonical_url=?", (url,)).fetchone()

    def job_counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.conn.execute("SELECT page_type, status, COUNT(*) n FROM crawl_jobs GROUP BY 1,2"):
            out.setdefault(r["page_type"], {})[r["status"]] = r["n"]
        return out

    # --------------------------------------------------------- dead letters
    def dead_letter(self, url: str, stage: str, error_type: str, message: str, attempts: int, run_id: str) -> None:
        self.conn.execute(
            """INSERT INTO dead_letters(url, stage, error_type, error_message, attempts, last_attempt_at, run_id)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET stage=excluded.stage, error_type=excluded.error_type,
                 error_message=excluded.error_message, attempts=excluded.attempts,
                 last_attempt_at=excluded.last_attempt_at, run_id=excluded.run_id""",
            (url, stage, error_type, message[:2000], attempts, utcnow(), run_id),
        )

    def clear_dead_letter(self, url: str) -> None:
        self.conn.execute("DELETE FROM dead_letters WHERE url=?", (url,))

    def dead_letters(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM dead_letters ORDER BY last_attempt_at").fetchall()

    # ------------------------------------------------------------ products
    def upsert_product(self, p: Product, run_id: str) -> str:
        """Idempotent upsert. Returns 'inserted' | 'updated' | 'unchanged'."""
        h = p.content_hash()
        existing = self.conn.execute(
            "SELECT content_hash FROM products WHERE product_id=?", (p.product_id,)).fetchone()
        outcome = "inserted" if existing is None else ("unchanged" if existing["content_hash"] == h else "updated")
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                """INSERT INTO products(product_id, source_product_id, canonical_url, name, brand, manufacturer,
                     parent_sku, product_type, category_path_json, description, price_min, price_max, price_type,
                     currency, availability, rating_value, review_count, specifications_json, extra_specifications_json,
                     image_urls_json, alternative_products_json, extraction_method, field_provenance_json, validation_status,
                     validation_issues_json, scraped_at, first_seen_at, content_hash, last_run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(product_id) DO UPDATE SET
                     source_product_id=excluded.source_product_id, canonical_url=excluded.canonical_url,
                     name=excluded.name, brand=excluded.brand, manufacturer=excluded.manufacturer,
                     parent_sku=excluded.parent_sku, product_type=excluded.product_type,
                     category_path_json=excluded.category_path_json, description=excluded.description,
                     price_min=excluded.price_min, price_max=excluded.price_max, price_type=excluded.price_type,
                     currency=excluded.currency, availability=excluded.availability,
                     rating_value=excluded.rating_value, review_count=excluded.review_count,
                     specifications_json=excluded.specifications_json,
                     extra_specifications_json=excluded.extra_specifications_json,
                     image_urls_json=excluded.image_urls_json,
                     alternative_products_json=excluded.alternative_products_json,
                     extraction_method=excluded.extraction_method, field_provenance_json=excluded.field_provenance_json,
                     validation_status=excluded.validation_status, validation_issues_json=excluded.validation_issues_json,
                     scraped_at=excluded.scraped_at, content_hash=excluded.content_hash, last_run_id=excluded.last_run_id""",
                (p.product_id, p.source_product_id, p.canonical_url, p.name, p.brand, p.manufacturer, p.parent_sku,
                 p.product_type, _j(p.category_path), p.description,
                 None if p.price_min is None else str(p.price_min), None if p.price_max is None else str(p.price_max),
                 p.price_type.value if p.price_type else None, p.currency, p.availability.value, p.rating_value,
                 p.review_count, _j(p.specifications), _j(p.extra_specifications), _j(p.image_urls),
                 _j([a.model_dump() for a in p.alternative_products]), p.extraction_method, _j(p.field_provenance),
                 p.validation_status, _j(p.validation_issues), p.scraped_at, p.scraped_at, h, run_id),
            )
            keep = []
            for v in p.variants:
                vid = f"{p.product_id}:{v.sku}"
                keep.append(vid)
                self.conn.execute(
                    """INSERT INTO variants(variant_id, product_id, sku, manufacturer_code, name, option_text,
                         attributes_json, price, currency, availability, availability_label, pack_size,
                         unit_of_measure, source_variant_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(variant_id) DO UPDATE SET manufacturer_code=excluded.manufacturer_code,
                         name=excluded.name, option_text=excluded.option_text, attributes_json=excluded.attributes_json,
                         price=excluded.price, currency=excluded.currency, availability=excluded.availability,
                         availability_label=excluded.availability_label, pack_size=excluded.pack_size,
                         unit_of_measure=excluded.unit_of_measure, source_variant_id=excluded.source_variant_id""",
                    (vid, p.product_id, v.sku, v.manufacturer_code, v.name, v.option_text, _j(v.attributes),
                     None if v.price is None else str(v.price), v.currency, v.availability.value,
                     v.availability_label, v.pack_size, v.unit_of_measure, v.source_variant_id),
                )
            # Variants no longer offered on the page are removed so the table mirrors the source.
            placeholders = ",".join("?" * len(keep)) or "''"
            self.conn.execute(
                f"DELETE FROM variants WHERE product_id=? AND variant_id NOT IN ({placeholders})",
                (p.product_id, *keep),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return outcome

    def product_id_for_url(self, url: str) -> str | None:
        r = self.conn.execute("SELECT product_id FROM products WHERE canonical_url=?", (url,)).fetchone()
        return r["product_id"] if r else None

    def iter_products(self) -> Iterable[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM products ORDER BY canonical_url").fetchall()

    def variants_for(self, product_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM variants WHERE product_id=? ORDER BY sku", (product_id,)).fetchall()

    def all_variants(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT v.*, p.name AS product_name, p.canonical_url AS product_url FROM variants v "
            "JOIN products p USING(product_id) ORDER BY p.canonical_url, v.sku").fetchall()

    def product_category_rows(self, product_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.* FROM product_categories pc JOIN categories c USING(category_id) WHERE pc.product_id=? "
            "ORDER BY c.depth", (product_id,)).fetchall()

    # ---------------------------------------------------------------- llm
    def llm_cache_get(self, key: str) -> dict | None:
        r = self.conn.execute("SELECT response_json FROM llm_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(r["response_json"]) if r else None

    def llm_cache_put(self, key: str, task: str, model: str, response: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache(cache_key, task, model, response_json, created_at) VALUES (?,?,?,?,?)",
            (key, task, model, _j(response), utcnow()))

    def log_llm_call(self, **kw) -> None:
        kw.setdefault("created_at", utcnow())
        cols = ",".join(kw)
        self.conn.execute(f"INSERT INTO llm_calls({cols}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()
