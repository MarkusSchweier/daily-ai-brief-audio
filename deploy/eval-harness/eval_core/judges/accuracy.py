"""FR-7: factual accuracy / hallucination-risk judge (LLM-judge only, no human
agree/override step per PRD §4.B -- reported for visibility).

Judges whether the brief's claims are traceable to something a reasonable web search
would surface; flags claims that read as unsupported or fabricated (numbers, dates,
benchmark scores, or quotes with no plausible sourcing).

Disclosed gap vs. PRD FR-7's literal wording: this v1 judge checks PLAUSIBILITY and
INTERNAL CONSISTENCY (does this claim read like something a web search would
actually confirm, is it hedged appropriately, is it self-contradictory) -- it does
NOT itself re-fetch each cited source and verify literal source-traceability. The
PRD explicitly designates this criterion "LLM-judge only" (§4.B/FR-7), which allows
this treatment; flagged here so a future reader doesn't assume a stronger guarantee
than what is actually implemented.

PORTED, UNCHANGED (ADR-0016 Phase 1, 2026-07-07) from `deploy/eval/eval_core/judges/accuracy.py`.
"""

from __future__ import annotations

from typing import Any

from .base import JSON_RESPONSE_INSTRUCTION, JudgeResult, run_judge

CRITERION = "factual_accuracy"

_SYSTEM_PROMPT = (
    "You are a fact-checking judge for a daily AI news brief. You will be given the "
    "brief's content. Judge whether its factual claims (numbers, dates, dollar "
    "amounts, benchmark scores, direct quotes, named products/models) read as the "
    "kind of thing a reasonable web search would actually surface and confirm -- i.e. "
    "specific, plausible, internally consistent, and appropriately hedged when the "
    "brief itself says something is unconfirmed/rumored. Flag any claim that reads as "
    "suspiciously invented, oddly specific with no plausible source, or internally "
    "contradictory. You are NOT fetching the web yourself -- judge plausibility and "
    "internal consistency, not ground truth. Score 1 (multiple claims read as "
    "fabricated/unsupported) to 5 (all claims read as well-sourced and appropriately "
    "hedged where relevant). " + JSON_RESPONSE_INSTRUCTION
)


def judge_factual_accuracy(client: Any, *, brief_markdown: str) -> JudgeResult:
    user_prompt = f"BRIEF:\n{brief_markdown}\n\nJudge factual accuracy / hallucination risk per your system instructions."
    return run_judge(client, criterion=CRITERION, system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt)


__all__ = ["judge_factual_accuracy", "CRITERION"]
