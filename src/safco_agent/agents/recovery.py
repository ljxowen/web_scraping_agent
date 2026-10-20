"""Recovery agent: chooses a bounded recovery action after a fetch/extraction/validation failure.

The action space is closed: the agent can only pick one of RecoveryAction. A deterministic policy is
the default; optionally an LLM picks among the *currently allowed* actions (its answer is ignored if
it names anything else). Budgets guarantee termination.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..infra.llm import LLMClient, LLMError
from ..infra.logging import get_logger

log = get_logger("recovery")


class RecoveryAction(str, Enum):
    RERENDER = "rerender"              # fetch again through the browser (dynamic content / transient)
    RETRY_DETERMINISTIC = "retry_http" # fetch raw HTML again and rerun deterministic parsers
    LLM_EXTRACT = "llm_extract"        # evidence-checked LLM extraction of visibly-present fields
    GIVE_UP = "give_up"                # mark unresolved -> dead letter


@dataclass
class RecoveryContext:
    url: str
    stage: str                         # fetch | classify | validate
    error: str | None = None
    transient: bool = True
    missing: list[str] = field(default_factory=list)
    visible_missing: list[str] = field(default_factory=list)
    attempts: int = 0
    max_attempts: int = 3
    tried: list[str] = field(default_factory=list)
    llm_available: bool = False


class RecoveryAgent:
    def __init__(self, llm: LLMClient | None = None, use_llm_decisions: bool = False):
        self.llm, self.use_llm = llm, use_llm_decisions
        self.stats: dict[str, int] = {a.value: 0 for a in RecoveryAction}

    def allowed_actions(self, ctx: RecoveryContext) -> list[RecoveryAction]:
        if ctx.attempts >= ctx.max_attempts:
            return [RecoveryAction.GIVE_UP]
        allowed = []
        if ctx.stage == "fetch":
            if ctx.transient:
                allowed += [RecoveryAction.RERENDER, RecoveryAction.RETRY_DETERMINISTIC]
        else:
            if ctx.visible_missing and ctx.llm_available and RecoveryAction.LLM_EXTRACT.value not in ctx.tried:
                allowed.append(RecoveryAction.LLM_EXTRACT)
            if RecoveryAction.RERENDER.value not in ctx.tried:
                allowed.append(RecoveryAction.RERENDER)
        allowed.append(RecoveryAction.GIVE_UP)
        return allowed

    def policy(self, ctx: RecoveryContext, allowed: list[RecoveryAction]) -> RecoveryAction:
        if ctx.stage == "fetch":
            # alternate transports: a browser render often succeeds when raw HTTP flaked, and vice versa
            last = ctx.tried[-1] if ctx.tried else None
            pref = RecoveryAction.RETRY_DETERMINISTIC if last == RecoveryAction.RERENDER.value else RecoveryAction.RERENDER
            return pref if pref in allowed else allowed[0]
        # Content problem: re-render first if the page may not have finished loading, then LLM.
        if RecoveryAction.RERENDER in allowed and not ctx.tried:
            return RecoveryAction.RERENDER
        if RecoveryAction.LLM_EXTRACT in allowed:
            return RecoveryAction.LLM_EXTRACT
        return allowed[0]

    async def decide(self, ctx: RecoveryContext) -> RecoveryAction:
        allowed = self.allowed_actions(ctx)
        action = self.policy(ctx, allowed)
        if self.use_llm and self.llm and self.llm.available and len(allowed) > 2:
            system = ("You choose one recovery action for a failed web-scraping step. "
                      'Respond with JSON only: {"action": "<one of the allowed actions>", "reason": "<short>"}')
            user = (f"Stage: {ctx.stage}\nError: {ctx.error}\nMissing fields: {ctx.missing}\n"
                    f"Missing but visibly present: {ctx.visible_missing}\nAlready tried: {ctx.tried}\n"
                    f"Allowed actions: {[a.value for a in allowed]}")
            try:
                data = await self.llm.complete_json("recovery_decision", system, user, url=ctx.url)
                choice = RecoveryAction(str(data.get("action")))
                if choice in allowed:
                    action = choice
            except (LLMError, ValueError, AttributeError) as e:
                log.info("llm_recovery_decision_ignored", url=ctx.url, error=str(e)[:200])
        self.stats[action.value] += 1
        log.info("recovery_decision", url=ctx.url, stage=ctx.stage, action=action.value,
                 allowed=[a.value for a in allowed], attempts=ctx.attempts)
        return action
