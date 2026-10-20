"""Small provider-agnostic LLM client (OpenAI-compatible APIs; MiniMax by default).

Features: env-based key, timeout + bounded retries, JSON-only output parsing (reasoning models'
<think> blocks are stripped), content-hash response cache in SQLite, per-call usage logging.
When no key is configured the client reports `available == False` and callers skip LLM steps.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from ..config import LLMConfig
from .logging import get_logger
from .storage import Storage

log = get_logger("llm")

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(Exception):
    pass


def parse_json_response(text: str) -> Any:
    """Extract the JSON payload from a model reply (handles <think> blocks and ``` fences)."""
    text = _THINK.sub("", text or "").strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise LLMError(f"no JSON object in response: {text[:200]!r}")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


class LLMClient:
    def __init__(self, cfg: LLMConfig, storage: Storage, run_id: str):
        self.cfg, self.storage, self.run_id = cfg, storage, run_id
        self._client = None
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "prompt_tokens": 0, "completion_tokens": 0,
                      "latency_s": 0.0}
        if cfg.active:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url,
                                       timeout=cfg.timeout_seconds, max_retries=cfg.max_retries)

    @property
    def available(self) -> bool:
        return self._client is not None

    def _key(self, task: str, system: str, user: str) -> str:
        return hashlib.sha256(f"{self.cfg.model}|{task}|{system}|{user}".encode()).hexdigest()

    async def complete_json(self, task: str, system: str, user: str, url: str | None = None) -> Any:
        if not self.available:
            raise LLMError("LLM not configured")
        key = self._key(task, system, user)
        cached = self.storage.llm_cache_get(key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            self.storage.log_llm_call(run_id=self.run_id, task=task, url=url, model=self.cfg.model, cached=1,
                                      ok=1, latency_s=0.0, prompt_tokens=0, completion_tokens=0, error=None)
            return cached["data"]
        t0 = time.monotonic()
        self.stats["calls"] += 1
        try:
            resp = await self._client.chat.completions.create(
                model=self.cfg.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=self.cfg.max_output_tokens,
                temperature=0.1,
            )
            text = resp.choices[0].message.content or ""
            data = parse_json_response(text)
        except Exception as e:
            self.stats["failures"] += 1
            latency = round(time.monotonic() - t0, 2)
            self.storage.log_llm_call(run_id=self.run_id, task=task, url=url, model=self.cfg.model, cached=0,
                                      ok=0, latency_s=latency, prompt_tokens=None, completion_tokens=None,
                                      error=f"{type(e).__name__}: {e}"[:500])
            log.warning("llm_call_failed", task=task, url=url, error=str(e)[:300])
            raise LLMError(str(e)) from e
        latency = round(time.monotonic() - t0, 2)
        usage = getattr(resp, "usage", None)
        pt = getattr(usage, "prompt_tokens", None)
        ct = getattr(usage, "completion_tokens", None)
        self.stats["prompt_tokens"] += pt or 0
        self.stats["completion_tokens"] += ct or 0
        self.stats["latency_s"] += latency
        self.storage.log_llm_call(run_id=self.run_id, task=task, url=url, model=self.cfg.model, cached=0, ok=1,
                                  latency_s=latency, prompt_tokens=pt, completion_tokens=ct, error=None)
        self.storage.llm_cache_put(key, task, self.cfg.model, {"data": data})
        log.info("llm_call", task=task, url=url, latency_s=latency, prompt_tokens=pt, completion_tokens=ct)
        return data
