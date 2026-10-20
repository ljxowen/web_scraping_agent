"""Config loading: YAML file for behaviour, environment (.env) for secrets."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class SiteConfig(BaseModel):
    base_url: str = "https://www.safcodental.com"
    user_agent: str = "SafcoCatalogPOC/0.1"
    respect_robots: bool = True


class Seed(BaseModel):
    name: str
    url: str


class CrawlConfig(BaseModel):
    max_products_per_category: int | None = None
    traverse_subcategories: bool = True
    product_fetch_mode: str = "browser"  # browser | http
    concurrency: int = 3
    max_attempts: int = 3


class RateLimitConfig(BaseModel):
    requests_per_second: float = 1.0
    burst: int = 2


class HttpConfig(BaseModel):
    timeout_seconds: float = 30
    max_retries: int = 4
    backoff_base_seconds: float = 1.5
    backoff_max_seconds: float = 30


class BrowserConfig(BaseModel):
    headless: bool = True
    navigation_timeout_ms: int = 45000
    listing_settle_ms: int = 2500
    recommendations_timeout_ms: int = 12000
    block_resource_types: list[str] = Field(default_factory=lambda: ["image", "font", "media"])
    block_hosts: list[str] = Field(default_factory=list)


class LLMConfig(BaseModel):
    enabled: bool = True
    provider: str = "minimax"
    base_url: str = "https://api.minimax.io/v1"
    model: str = "MiniMax-M2.5"
    api_key_env: str = "MINIMAX_API_KEY"
    timeout_seconds: float = 90
    max_retries: int = 2
    max_output_tokens: int = 4000
    extraction_fallback: bool = True
    spec_extraction: bool = True
    ambiguous_classification: bool = True
    recovery_decisions: bool = False

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.api_key)


class StorageConfig(BaseModel):
    db_path: str = "data/catalog.db"
    output_dir: str = "output"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    json_file: str | None = "logs/crawl.jsonl"


class AppConfig(BaseModel):
    site: SiteConfig = SiteConfig()
    seeds: list[Seed] = Field(default_factory=list)
    crawl: CrawlConfig = CrawlConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    http: HttpConfig = HttpConfig()
    browser: BrowserConfig = BrowserConfig()
    llm: LLMConfig = LLMConfig()
    storage: StorageConfig = StorageConfig()
    logging: LoggingConfig = LoggingConfig()

    def redacted(self) -> dict:
        """Config snapshot safe to persist in crawl_runs (no secrets live in config anyway)."""
        return self.model_dump()


def load_config(path: str | Path = "config.yaml", env_file: str | Path = ".env") -> AppConfig:
    if Path(env_file).exists():
        load_dotenv(env_file, override=False)
    data = {}
    if Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
