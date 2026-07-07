"""Shared judge plumbing: the `JudgeResult` shape and the one Anthropic Messages API
call wrapper every judge (content selection, accuracy, length/format, dedup) uses.

G-Eval style: the caller supplies a rubric + scoring scale in the prompt and asks the
judge to reason ("rationale") before committing to a score, then to also cite
"evidence" (a short quote/example) supporting that score -- matching PRD FR-6/FR-15's
"score plus judge rationale plus supporting evidence" shape for every kept criterion.

Uses the standard Anthropic **Messages API** (a normal one-shot API call, not a
Managed Agents session) -- these are narrow, well-scoped scoring tasks, not
open-ended multi-turn work.

PORTED, UNCHANGED (ADR-0016 "Eval-harness re-integration" Phase 1, 2026-07-07) from
`deploy/eval/eval_core/judges/base.py` -- a judge call is a plain Messages API call
regardless of where the run's artifacts it scores came from (S3, in the old
backbone, vs. `candidate_sync.trigger.fetch_catted_file_contents()`'s session-events
retrieval, in this one), so this plumbing needed no change at all.

AMENDED (review-fix pass, 2026-07-07 -- reviewer Medium, confirmed gap vs ADR-0016):
`run_judge()` used to call `client.messages.create(...)` and discard
`response.usage` entirely, so a judge's own token cost was never captured anywhere
-- meaning it could never be priced or reported "separately from pipeline cost" per
ADR-0016's own cross-cutting section and PRD §7. `JudgeResult` now carries a
`usage` field (the SAME flat shape `harness.cost.ThreadUsage.to_dict()`/
`from_dict()` use, so a caller can price it against `pricing.json` with zero
translation) captured on EVERY call, including the malformed-JSON-response degrade
path (the call still cost real tokens even when the response couldn't be parsed).
This module still does NOT price the usage itself -- that stays in `harness/cost.py`
(pricing is business logic against a specific price table; this module's job is
only to make the one Messages API call and parse its result), keeping `eval_core`
free of any dependency on `harness`.

AMENDED (judge methodology v2, 2026-07-07 -- owner-directed rework, docs/adr/0016
amendment): three changes, all additive to the shape above:

1. **Per-judge model config, ALL FOUR defaulting to Opus 4.8.** `JUDGE_MODELS`
   below is the single, explicit, easily-flippable mapping of criterion -> model
   id. The owner's direction (2026-07-07): a judge must be run on a model STRONGER
   than what it judges, or the evaluation doesn't mean much -- so every criterion
   (including the unchanged length/format judge) defaults to `claude-opus-4-8`,
   NOT the previous single shared `claude-haiku-4-5`. `run_judge()` now takes an
   explicit `model=` (each judge module passes its own `JUDGE_MODELS[CRITERION]`)
   and `JudgeResult` records which model actually ran the call (`.model`) so a
   caller can price each criterion against ITS OWN model rather than one global
   constant -- `JUDGE_MODEL` below is kept only as `run_judge()`'s own parameter
   default (for direct callers/tests that don't pass `model=` explicitly), not as
   "the" judge model.
2. **Server-side web_search/web_fetch tool support.** `run_judge()` now accepts an
   optional `tools=` list, passed straight through to `messages.create(...)`
   unmodified (this module makes no assumption about which tool types/versions a
   caller uses -- see each judge module for the specific tool blocks and the
   verified schema/version notes). Server-side tools mean a single Messages API
   response can carry MIXED content: `server_tool_use` blocks, `web_search_tool_
   result`/`web_fetch_tool_result` blocks, and one or more `text` blocks
   interleaved (a judge may narrate before/between tool calls before committing to
   its final JSON verdict). `_extract_final_text_block()` below takes ONLY the
   LAST text block for JSON parsing -- concatenating every text block (the old
   behavior) risks an earlier narration block's own stray `{`/`}` characters
   corrupting `_extract_json_object()`'s outermost-brace scan, and is simply the
   wrong text to parse when Claude narrates ("Let me search for...") before its
   verdict.
3. **Search-count capture for judge-cost accounting.** `JudgeResult.search_count`
   captures `response.usage.server_tool_use.web_search_requests` (confirmed live
   2026-07-07 against the web-search-tool docs page's own example response JSON)
   -- web search is billed per-call ($10/1,000 searches), a SEPARATE cost axis
   from token usage, priced by `harness.cost.price_web_searches()`, never folded
   into the token-based `usage`/`JudgeResult.usage` numbers. Defaults to 0 and is
   captured defensively (never raises) exactly like `_extract_usage()` -- a
   response with no tool calls, or a test double with no `server_tool_use`
   attribute, still returns a valid `JudgeResult`.

Also new: `JudgeResult.findings` / `.selection_disagreements` -- optional
structured arrays (`list[dict] | None`) a v2 judge's JSON response may include
alongside the original `score`/`rationale`/`evidence`/`insufficient_data` shape
(additive, per-judge; see `accuracy.py`/`content_selection.py`/`dedup.py` for which
key each uses and its exact per-entry shape). `run_judge()` parses BOTH keys
generically whenever present in the response JSON -- it does not care which judge
is calling it or validate the array's internal shape (that is each judge's own
prompt's job to specify and, at review time, a human's job to sanity-check); a
judge whose prompt never asks for one simply never gets it back (stays `None`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Per-judge model config (judge methodology v2, 2026-07-07, owner-directed): a
# judge must be run on a model STRONGER than what it judges, so every v1 criterion
# -- including the length/format judge, whose PROMPT is otherwise unchanged --
# defaults to Opus 4.8. Kept as ONE small, explicit, easily-flippable mapping (not
# hardcoded inline at each judge's call site) so a future owner call to move any
# single judge to a different model (e.g. back to Sonnet/Haiku once Opus judge
# cost is validated in practice) is a one-line change here, not a hunt through
# four files. Each judge module resolves its own entry at import time
# (`_MODEL = JUDGE_MODELS[CRITERION]`) and passes it explicitly to `run_judge()`.
JUDGE_MODELS: dict[str, str] = {
    "factual_accuracy": "claude-opus-4-8",
    "content_selection": "claude-opus-4-8",
    "length_format": "claude-opus-4-8",
    "dedup": "claude-opus-4-8",
}

# `run_judge()`'s own default when a caller doesn't pass `model=` explicitly (e.g.
# a direct test of `run_judge()` itself, decoupled from any specific judge's
# config) -- kept in sync with JUDGE_MODELS' shared value, but NOT itself "the"
# judge model for any of the four criteria; each judge module passes its own
# `JUDGE_MODELS[CRITERION]` rather than relying on this default.
JUDGE_MODEL = "claude-opus-4-8"

# The zero-usage shape returned when a response carries no `.usage` at all (should
# not happen against the real Anthropic SDK, but a test double or a future API
# revision might omit it) -- explicit rather than an ad hoc dict literal at each
# call site.
_EMPTY_USAGE: dict[str, int] = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation_5m_input_tokens": 0,
    "cache_creation_1h_input_tokens": 0,
}


@dataclass(frozen=True)
class JudgeResult:
    """One judge's output for one criterion: PRD FR-6/FR-15's "score plus judge
    rationale plus supporting evidence" shape.

    `score` is on the judge's own stated 1-5 scale (matching the reader-feedback
    table's 1-5 grading, so FR-15's calibration join can compare like-for-like without
    a rescale). `insufficient_data=True` marks a judge's graceful degrade (e.g.
    content-selection with no candidates.json, FR-6's documented degrade path) --
    when True, `score` is None and `rationale` explains why, rather than a
    fabricated/guessed score.

    `usage` is the judge CALL's own token usage (input/output/cache, the same flat
    shape `harness.cost.ThreadUsage` uses) -- captured so a caller can price it
    SEPARATELY from pipeline cost (ADR-0016 review-fix; see module docstring).

    `model` (judge methodology v2) records which model actually made this call --
    per-judge model config means this is no longer implied by a single global
    constant, so a caller (`run.py`'s `_price_judge_results()`) must price each
    criterion against ITS OWN recorded model.

    `search_count` (judge methodology v2) is the number of server-side web-search
    tool invocations this call made (0 for a judge with no `tools=`, or a call that
    happened not to search even with tools available) -- priced separately from
    `usage` via `harness.cost.price_web_searches()`.

    `findings` / `selection_disagreements` (judge methodology v2) are OPTIONAL
    structured arrays a v2 judge's prompt may ask for, parsed straight from the
    response JSON when present (see module docstring)."""

    criterion: str
    score: int | None
    rationale: str
    evidence: str
    insufficient_data: bool = False
    usage: dict[str, int] = field(default_factory=lambda: dict(_EMPTY_USAGE))
    model: str = JUDGE_MODEL
    search_count: int = 0
    findings: list[dict[str, Any]] | None = None
    selection_disagreements: list[dict[str, Any]] | None = None


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first top-level JSON object from `text`. Judge prompts ask for a
    JSON-only response, but models occasionally wrap it in prose or a code fence --
    tolerate both by finding the outermost {...} span rather than requiring the whole
    response to be valid JSON on its own."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in judge response: {text!r}")
    return json.loads(text[start : end + 1])


def _extract_final_text_block(response: Any) -> str:
    """Return ONLY the LAST `text`-type content block in `response.content`.

    A response that used server-side tools (web_search/web_fetch) carries MIXED
    content: `server_tool_use` blocks, `web_search_tool_result`/
    `web_fetch_tool_result` blocks, and possibly more than one `text` block
    interleaved (a judge may narrate -- "Let me verify this claim..." -- between
    searches before committing to its final JSON verdict). The judge's structured
    JSON response is always the LAST text block emitted; take that one
    specifically, never join every text block (an earlier narration block could
    itself contain stray `{`/`}` characters that would corrupt
    `_extract_json_object()`'s outermost-brace scan, or simply not BE the JSON at
    all). For a plain response with exactly one text block (every v1 judge, and
    any v2 judge call that made no tool calls), this is identical to the old
    "join every text block" behavior."""
    text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return text_blocks[-1] if text_blocks else ""


def _extract_usage(response: Any) -> dict[str, int]:
    """Capture a Messages API response's own token usage into the SAME flat shape
    `harness.cost.ThreadUsage.to_dict()`/`from_dict()` use, so a caller can price
    it against `pricing.json` with zero translation (review-fix: see module
    docstring). Reads every field defensively via `getattr(..., 0)` -- a fake test
    double, or a future SDK response shape, may not carry every attribute; this
    must never raise over a missing usage field.

    The real Anthropic Messages API's usage shape is FLAT
    (`{input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens}`) -- unlike the Sessions/Threads API's nested
    `cache_creation: {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}` shape
    `harness.cost.ThreadUsage.from_api_usage()` parses. This function tolerates
    BOTH: it prefers a nested `usage.cache_creation` object if present (a future/
    beta response shape), and otherwise attributes a flat
    `cache_creation_input_tokens` value to the 5m bucket -- prompt caching's
    default TTL, and the closest available rate in `pricing.json` (there is no
    separate "flat cache write, unknown TTL" price tier)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return dict(_EMPTY_USAGE)

    cache_creation = getattr(usage, "cache_creation", None)
    if cache_creation is not None:
        cache_5m = getattr(cache_creation, "ephemeral_5m_input_tokens", 0) or 0
        cache_1h = getattr(cache_creation, "ephemeral_1h_input_tokens", 0) or 0
    else:
        cache_5m = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_1h = 0

    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_5m_input_tokens": cache_5m,
        "cache_creation_1h_input_tokens": cache_1h,
    }


def _extract_search_count(response: Any) -> int:
    """Capture `response.usage.server_tool_use.web_search_requests` (confirmed
    live 2026-07-07 against the web-search-tool docs page's own example response:
    `"usage": {..., "server_tool_use": {"web_search_requests": 1}}`) -- the number
    of server-side web-search tool invocations this ONE Messages API call made.
    Reads defensively via `getattr(..., 0)` at every level, exactly like
    `_extract_usage()` -- a response with no tools, a call that made no searches,
    or a test double missing the attribute entirely, all return 0, never raise."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    server_tool_use = getattr(usage, "server_tool_use", None)
    if server_tool_use is None:
        return 0
    return getattr(server_tool_use, "web_search_requests", 0) or 0


def run_judge(
    client: Any,
    *,
    criterion: str,
    system_prompt: str,
    user_prompt: str,
    model: str = JUDGE_MODEL,
    max_tokens: int = 1024,
    tools: list[dict[str, Any]] | None = None,
) -> JudgeResult:
    """Call the Messages API once and parse a `JudgeResult` from the response.

    `client` is an Anthropic SDK client (or a test double exposing the same
    `messages.create(...)` shape) -- injected so tests never need a real API key or
    network access. `model` (judge methodology v2) is the SPECIFIC model this call
    runs on -- each judge module passes its own `JUDGE_MODELS[CRITERION]`, never
    relying on this function's own `JUDGE_MODEL` default (kept only for direct
    callers/tests). `tools`, when given, is passed straight through to
    `messages.create(...)` unmodified -- this module makes no assumption about
    which tool types/versions a caller uses.

    The judge is instructed to respond with a single JSON object:
    `{"score": 1-5 | null, "rationale": "...", "evidence": "...",
    "insufficient_data": bool}`, optionally plus a `findings` and/or
    `selection_disagreements` array (parsed through as-is when present -- see
    module docstring). A malformed/unparseable response degrades to an
    `insufficient_data=True` result rather than raising -- a judge call must never
    crash the harness's evaluation run over an LLM formatting slip. The call's own
    token usage AND search count are ALWAYS captured into the returned result,
    even on the malformed-response degrade path below -- the call still cost real
    tokens (and may have made real, billable searches) whether or not the response
    could be parsed (review-fix, see module docstring).
    """
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        # AUTOMATIC prompt caching (top-level cache_control; confirmed against
        # the prompt-caching docs 2026-07-07): the API auto-manages cache
        # breakpoints as the request's context grows. This matters ENORMOUSLY
        # for the web-tool judges: a server-side tool loop re-sends the
        # accumulated context (system + brief + every prior search/fetch
        # result) on EVERY iteration -- the first live v2 accuracy-judge smoke
        # burned 281,543 UNCACHED input tokens ($1.52) across its 8-search
        # loop, cache_read=0. With this field, iterations 2..N read the shared
        # prefix at 0.1x base instead. 5m TTL is ample (a judge call's loop
        # completes within minutes); writes cost 1.25x base, paid back after a
        # single loop iteration. Harmless on tool-less judges (one iteration,
        # nothing re-read).
        cache_control={"type": "ephemeral"},
    )
    if tools:
        kwargs["tools"] = tools

    response = client.messages.create(**kwargs)
    usage = _extract_usage(response)
    search_count = _extract_search_count(response)
    text = _extract_final_text_block(response)

    try:
        parsed = _extract_json_object(text)
    except (ValueError, json.JSONDecodeError):
        return JudgeResult(
            criterion=criterion,
            score=None,
            rationale=f"Judge response could not be parsed as JSON: {text[:500]!r}",
            evidence="",
            insufficient_data=True,
            usage=usage,
            model=model,
            search_count=search_count,
        )

    insufficient = bool(parsed.get("insufficient_data", False))
    raw_score = parsed.get("score")
    score = None if insufficient or raw_score is None else int(raw_score)

    findings = parsed.get("findings")
    selection_disagreements = parsed.get("selection_disagreements")

    return JudgeResult(
        criterion=criterion,
        score=score,
        rationale=str(parsed.get("rationale", "")),
        evidence=str(parsed.get("evidence", "")),
        insufficient_data=insufficient,
        usage=usage,
        model=model,
        search_count=search_count,
        findings=findings if isinstance(findings, list) else None,
        selection_disagreements=selection_disagreements if isinstance(selection_disagreements, list) else None,
    )


JSON_RESPONSE_INSTRUCTION = (
    "Respond with ONLY a single JSON object, no other text, no markdown code fence, "
    'in exactly this shape: {"score": <integer 1-5, or null if insufficient_data>, '
    '"rationale": "<your reasoning, 2-4 sentences, written BEFORE you decide the '
    'score>", "evidence": "<a short supporting quote or concrete example from the '
    'input>", "insufficient_data": <true only if you genuinely cannot judge this '
    "from the given input, false otherwise>}."
)

# --- v2 JSON response instructions (judge methodology v2, 2026-07-07) --------------
# Each extends JSON_RESPONSE_INSTRUCTION's base shape with ONE additional structured
# array field, specific to what that judge documents. `run_judge()` parses both
# `findings` and `selection_disagreements` generically (see module docstring); these
# constants exist so each judge's own prompt can spell out its array's exact
# per-entry shape without duplicating the base score/rationale/evidence wording.

JSON_RESPONSE_INSTRUCTION_WITH_ACCURACY_FINDINGS = (
    "Respond with ONLY a single JSON object, no other text, no markdown code fence, "
    'in exactly this shape: {"score": <integer 1-5, or null if insufficient_data>, '
    '"rationale": "<your reasoning, 2-4 sentences, written BEFORE you decide the '
    'score>", "evidence": "<a short supporting quote or concrete example from the '
    'input>", "insufficient_data": <true only if you genuinely cannot judge this '
    'from the given input, false otherwise>, "findings": [{"claim": "<the claim '
    'you checked, quoted or closely paraphrased from the brief>", "verdict": '
    '"confirmed" | "contradicted" | "unverifiable", "source_checked": "<URL or '
    'publication you consulted via web_search/web_fetch>", "note": "<one sentence '
    "-- if contradicted, SPECIFICALLY state how the brief's version differs from "
    'what your research found>"}, ...] (one entry per claim you checked; empty '
    "array only if you genuinely checked nothing)}."
)

JSON_RESPONSE_INSTRUCTION_WITH_SELECTION_DISAGREEMENTS = (
    "Respond with ONLY a single JSON object, no other text, no markdown code fence, "
    'in exactly this shape: {"score": <integer 1-5, or null if insufficient_data>, '
    '"rationale": "<your reasoning, 2-4 sentences, written BEFORE you decide the '
    'score>", "evidence": "<a short supporting quote or concrete example from the '
    'input>", "insufficient_data": <true only if you genuinely cannot judge this '
    'from the given input, false otherwise>, "selection_disagreements": '
    '[{"story": "<the story\'s title/short description>", "judge_view": "should '
    'have been included" | "should have been excluded", "rationale": "<why -- '
    "cite what your own web_search/web_fetch research found that changed your "
    'view>"}, ...] (empty array if you agree with every inclusion/exclusion '
    "decision)}."
)

JSON_RESPONSE_INSTRUCTION_WITH_DEDUP_FINDINGS = (
    "Respond with ONLY a single JSON object, no other text, no markdown code fence, "
    'in exactly this shape: {"score": <integer 1-5, or null if insufficient_data>, '
    '"rationale": "<your reasoning, 2-4 sentences, written BEFORE you decide the '
    'score>", "evidence": "<a short supporting quote or concrete example from the '
    'input>", "insufficient_data": <true only if you genuinely cannot judge this '
    'from the given input, false otherwise>, "findings": [{"story": "<the '
    'repeated story\'s title/short description>", "duplicate_of_date": '
    '"YYYY-MM-DD", "labelled_as_followup": <bool -- does today\'s brief itself '
    'flag this as a follow-up/update?>, "justified": <bool -- does it add '
    'substantial new data/findings, not just a rehash?>, "note": "<one '
    'sentence>"}, ...] (empty array if no duplications were found)}.'
)

__all__ = [
    "JudgeResult",
    "run_judge",
    "JUDGE_MODEL",
    "JUDGE_MODELS",
    "JSON_RESPONSE_INSTRUCTION",
    "JSON_RESPONSE_INSTRUCTION_WITH_ACCURACY_FINDINGS",
    "JSON_RESPONSE_INSTRUCTION_WITH_SELECTION_DISAGREEMENTS",
    "JSON_RESPONSE_INSTRUCTION_WITH_DEDUP_FINDINGS",
]
