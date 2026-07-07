"""Factual-accuracy judge -- FULL REWORK, judge methodology v2 (2026-07-07,
owner-directed, docs/adr/0016 amendment).

## Why this was reworked (the two live-run findings that motivated it)

The v1 version of this judge (PORTED, ADR-0016 Phase 1) judged PLAUSIBILITY
against its own training data, not ACCURACY against the brief's own sources -- and
a real committed run exposed exactly the failure mode that framing predicts.
`deploy/eval-harness/runs/production-baseline/2026-07-07-174129-cdafb6-harness-
validation-baseline/repetitions/01/scores.json`'s `factual_accuracy` entry scored
a real, correctly-dated production brief a 2/5, with the rationale citing "The
brief is dated July 7, 2026 -- a future date nearly two years from now" and
"[fictional] product names" as evidence of fabrication. That is KNOWLEDGE-CUTOFF
BIAS, not a real defect: the brief was accurately reporting real events from a
date after the judge's own training cutoff, and the judge treated "I don't
recognize this" as if it were evidence of fabrication, rather than doing the one
thing that would actually answer the question -- checking.

## What changed

This judge now ACTUALLY VALIDATES the brief's content via its OWN live research
(the server-side `web_search`/`web_fetch` tools), rather than judging whether
claims merely READ as plausible against training-data familiarity:

1. **Explicit anti-cutoff-bias instruction.** The system prompt states directly
   that the brief may legitimately be dated after the judge's own knowledge
   cutoff, and that "I don't recognize this event/product/model" is NOT itself
   evidence of fabrication -- only the judge's own live research can establish
   that.
2. **Given the curated source list (`sources.md`).** The judge is told which
   outlets the brief's OWN research draws from (read from this repo's lockstep
   copy, `deploy/managed-agent/skills/daily-ai-brief/sources.md`, and passed in by
   the caller -- this module does no file I/O of its own) -- context for where the
   brief's sourcing SHOULD trace back to, though the judge is not restricted to
   those domains for its own verification searches.
3. **Extract-then-verify process, with a stated focus set.** The judge extracts
   headlines and key factual statements PER SECTION -- focused on headlines,
   numbers, dates, dollar amounts, benchmark scores, direct quotes, and named
   products/models -- then verifies or falsifies each via its own web research,
   documenting every checked claim in a structured `findings` array
   (`{claim, verdict, source_checked, note}`), with any deviation between the
   brief's version and what its research found SPECIFICALLY documented.
4. **Server-side web_search + web_fetch tools, capped.** `max_uses: 8` on both
   tools (owner spec: "accuracy: 8" for search; the same cap is applied to fetch
   -- not itemized by the owner, but a deliberate, documented safety bound so an
   uncapped fetch tool can't unboundedly inflate token cost/time on a single
   judge call).
5. **Model: Opus 4.8** (judge methodology v2's owner-directed "judges must be
   stronger than what they judge" principle -- see `base.JUDGE_MODELS`).

## Tool schema/version, verified live 2026-07-07 (record of what was confirmed)

Verified by fetching the current docs pages directly (no API key needed):
`https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/web-search-tool`
and `.../web-fetch-tool`.

- **web_search**: tool block `{"type": "web_search_20250305", "name":
  "web_search", "max_uses": <n>}`. `web_search_20250305` is explicitly still
  current/documented ("The examples on this page use `web_search_20250305` for
  basic search") -- newer versions (`web_search_20260209` dynamic filtering,
  `web_search_20260318` dynamic filtering + response-inclusion control) exist but
  add capabilities this judge doesn't need. No `anthropic-beta` header required.
  Billing: "$10 per 1,000 searches" (flat per-call, model-independent) -- priced
  by `harness.cost.price_web_searches()`.
- **web_fetch**: tool block `{"type": "web_fetch_20250910", "name": "web_fetch",
  "max_uses": <n>}`. `web_fetch_20250910` ("for basic fetch") is explicitly still
  current/available per the docs' own version list, alongside newer dynamic-
  filtering versions this judge doesn't need. **No `anthropic-beta` header is
  required** -- CONFIRMED live 2026-07-07: the docs' own cURL example sends no
  `anthropic-beta` header at all, and no "in beta" callout appears anywhere on
  the page. This corrects the historical assumption (the task's own starting
  note) that web_fetch needed a beta header (`web-fetch-2025-09-10`) -- that
  requirement is no longer current. Billing: "available... at no additional
  cost... you only pay standard token costs" -- i.e. NOT priced by
  `price_web_searches()`; fetched content flows through ordinary input-token
  accounting instead.
- **Usage/search-count shape**: a response's `usage.server_tool_use.
  web_search_requests` (confirmed via the docs page's own example response JSON)
  is how many searches this call made -- `base._extract_search_count()` reads
  this.
"""

from __future__ import annotations

from typing import Any

from .base import (
    JSON_RESPONSE_INSTRUCTION_WITH_ACCURACY_FINDINGS,
    JUDGE_MODELS,
    JudgeResult,
    run_judge,
)

CRITERION = "factual_accuracy"

# Owner spec: "accuracy: 8" for web_search's max_uses. The SAME cap is applied to
# web_fetch (not itemized by the owner -- a deliberate, documented safety bound;
# see module docstring).
_MAX_TOOL_USES = 8

_MODEL = JUDGE_MODELS[CRITERION]

_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": _MAX_TOOL_USES}
_WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": _MAX_TOOL_USES}

_FOCUS_SET_DESCRIPTION = (
    "headlines, numbers, dates, dollar amounts, benchmark scores, direct quotes, "
    "and named products/models"
)

# Split into two parts so the caller-supplied `sources_md` (only known at call
# time, never at import time) is inserted with a plain f-string at call time --
# deliberately NOT a `.format()`/`{sources_md}` placeholder baked into a
# module-level constant, which would require escaping every OTHER literal brace
# in this prompt (fragile, easy to get wrong). `_SYSTEM_PROMPT_PREFIX` ends right
# before the source list; `_SYSTEM_PROMPT_SUFFIX` picks up right after it.
_SYSTEM_PROMPT_PREFIX = (
    "You are a rigorous fact-CHECKING judge for a daily AI news brief. Your job is to "
    "ACTUALLY VALIDATE the brief's content against live, current sources -- not to judge "
    "whether it merely READS as plausible against what you already know.\n\n"
    "CRITICAL -- do not let your own training-data knowledge cutoff bias your judgment: "
    "this brief may legitimately be dated AFTER your knowledge cutoff, and it will very "
    "likely describe real, current events, products, model names, and figures you have "
    "never seen before. 'I don't recognize this event/product/model' is NOT evidence of "
    "fabrication -- it is exactly the situation your OWN live research (the web_search "
    "and web_fetch tools you have been given) exists to resolve. Every verdict you reach "
    "must come from what your live research actually finds, never from whether a claim "
    "merely matches what you already knew before searching.\n\n"
    "CURATED SOURCE LIST this brief's own research draws from (context on where its "
    "sourcing should trace back to -- you are NOT restricted to these domains for your "
    "own verification searches):\n\n"
)

_SYSTEM_PROMPT_SUFFIX = (
    "\n\nYour process: (1) extract the brief's key headlines and factual statements PER "
    f"SECTION, focused on: {_FOCUS_SET_DESCRIPTION}; (2) for EACH claim, use web_search "
    "and/or web_fetch to find live, current sourcing that confirms, contradicts, or fails "
    "to substantiate it; (3) SPECIFICALLY document any deviation between the brief's "
    "version of a claim and what your own research actually found -- a different number, "
    "a different date, a hedge the brief dropped, an outlet that doesn't corroborate it. "
    "Score 1 (multiple claims your research contradicts, or that you could not "
    "substantiate at all after genuinely trying) to 5 (every checked claim confirmed by "
    "your own research, with the brief appropriately hedging anything it itself flags as "
    "unconfirmed/rumored). " + JSON_RESPONSE_INSTRUCTION_WITH_ACCURACY_FINDINGS
)


def judge_factual_accuracy(client: Any, *, brief_markdown: str, sources_md: str) -> JudgeResult:
    """Score factual accuracy by having the judge conduct its OWN live web
    research (via server-side web_search/web_fetch) against the brief's key
    claims, rather than judging plausibility from training-data familiarity alone.

    `sources_md` is the daily-ai-brief skill's curated source list content (the
    caller reads it from `deploy/managed-agent/skills/daily-ai-brief/sources.md`
    -- this module does no file I/O of its own, matching every other judge's
    "caller resolves inputs, judge only scores" discipline)."""
    system_prompt = _SYSTEM_PROMPT_PREFIX + sources_md + _SYSTEM_PROMPT_SUFFIX
    user_prompt = (
        f"BRIEF:\n{brief_markdown}\n\n"
        "Extract the key claims per your system instructions' focus set, verify each via "
        "your own web_search/web_fetch research, and judge factual accuracy per your "
        "system instructions."
    )
    return run_judge(
        client,
        criterion=CRITERION,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=_MODEL,
        max_tokens=4096,
        tools=[_WEB_SEARCH_TOOL, _WEB_FETCH_TOOL],
    )


__all__ = ["judge_factual_accuracy", "CRITERION"]
