import json
import os
import re
from pathlib import Path

import pytest

from safco_agent.models import FetchedPage

FIX = Path(__file__).parent / "fixtures"
LAVENDER_URL = "https://www.safcodental.com/product/lavender-nitrile"
SURGICEL_URL = "https://www.safcodental.com/product/surgicel-reg"


def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits the real website / LLM API (set RUN_LIVE=1)")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test; set RUN_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture
def lavender_html() -> str:
    return read("product_grouped_lavender.html")


@pytest.fixture
def lavender_page(lavender_html) -> FetchedPage:
    recs = json.loads(read("adobe_recs_lavender.json"))
    return FetchedPage(url=LAVENDER_URL, html=lavender_html, status=200, captured={"recommendations": recs})


@pytest.fixture
def surgicel_page() -> FetchedPage:
    return FetchedPage(url=SURGICEL_URL, html=read("product_grouped_surgicel.html"), status=200)


@pytest.fixture
def drifted_lavender_page(lavender_html) -> FetchedPage:
    """Simulated layout change: structured data (JSON-LD + masterData) gone, visible content intact."""
    html = re.sub(r'<script type="application/ld\+json">.*?</script>', "", lavender_html, flags=re.S)
    html = html.replace("window.masterData", "window.__renamedData")
    return FetchedPage(url=LAVENDER_URL, html=html, status=200, via="http")


class FakeLLM:
    """Deterministic stand-in for LLMClient: returns canned JSON per task and records prompts."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.available = True
        self.stats = {}

    async def complete_json(self, task, system, user, url=None):
        self.calls.append((task, user))
        r = self.responses.get(task)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def fake_llm_factory():
    return FakeLLM


@pytest.fixture
def storage(tmp_path):
    from safco_agent.infra.storage import Storage
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()
