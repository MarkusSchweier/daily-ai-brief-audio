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

AMENDED (judge model -> Claude Opus 5.5, 2026-10-11 -- owner-directed, docs/adr/0016
amendment): all four judges move from `claude-opus-4-8` to `claude-opus-5-5`, which
is a stronger model at a LOWER price ($4/$20 vs $5/$25, and cache reads at 0.05x
base input instead of 0.1x). Opus 5.5's request surface forces four changes here --
nothing else about the judges (prompts, tool versions, `max_uses` caps, top-level
`cache_control`) changes:

1. **Thinking is ALWAYS on (adaptive) and cannot be disabled.** `thinking:
   {"type": "disabled"}`, `budget_tokens`, sampling params
   (`temperature`/`top_p`/`top_k`) and forced `tool_choice` (`any`/`tool`) all
   return 400 on this model. `run_judge()` has never sent ANY of them and must
   not start -- the only new field is `output_config`.
2. **Effort is explicit.** Opus 5.5's API default is `medium` (Opus 4.8's was
   `high`), so a straight model swap would quietly run the judges one effort level
   lower than before -- a change to the measuring instrument, mid-comparison.
   Every judge call therefore sends `output_config={"effort": JUDGE_EFFORT}` with
   `JUDGE_EFFORT = "high"`.
3. **`max_tokens` must cover the thinking too.** Thinking tokens count against
   `max_tokens` even though their text isn't returned under the default
   `display: "omitted"`. The v2 caps (1024-4096) were set for a judge running
   WITHOUT thinking on Opus 4.8 and would now truncate the verdict, so the three
   reasoning judges use 16000 and `length_format` 8000 (non-streaming
   `messages.create` is fine at these values).
4. **`stop_reason` is handled BEFORE the content is read** (there was no
   handling at all before):
   - `"refusal"` (a safety-classifier decline; HTTP 200, `content` empty or
     partial) raises `JudgeRefusedError` carrying `stop_details.category`.
     DELIBERATELY NO fallback to another model: a silent model swap mid-batch
     would change the measuring instrument and make the run's scores
     incomparable, which is strictly worse than a loud failure.
   - `"max_tokens"` raises `JudgeTruncatedError` -- a truncated response's text
     is not a verdict, and letting it fall through to the JSON parser would
     record it as a bland `insufficient_data` degrade instead of the real,
     fixable cause.
   - `"pause_turn"` (the server-side tool loop hitting its 10-iteration limit --
     reachable for the two web-tool judges) is CONTINUED by sending the paused
     assistant turn back unchanged, bounded at `MAX_PAUSE_TURN_CONTINUATIONS`
     (5), SUMMING token usage and web-search counts across every iteration so
     judge cost is never under-reported; exhausting the bound raises
     `JudgePauseLimitError`.
   All three errors are plain exceptions that propagate to `run.py`'s existing
   per-repetition containment (the broad `except Exception` around the whole
   post-trigger bookkeeping phase), which records `final_status="judging_failed"`
   plus the error and finalizes the run as failed -- no weakening of that
   containment, and no new silent-degrade path. AMENDED (review-fix, reviewer L3,
   2026-10-11): all three now CARRY the token usage and search count accumulated
   up to the failure (`JudgeCallError.usage` / `.search_count`). Those calls were
   really billed -- a `pause_turn` loop that exhausts its bound has made six of
   them -- and the numbers used to be discarded the instant the error was raised,
   so a failed judge call looked free. Attaching them is an accounting fix only:
   containment semantics are unchanged.

Thinking blocks appear in `response.content` as `type: "thinking"` (empty text
under the default display). `_extract_final_text_block()` already selects only
`type == "text"` blocks, so they are ignored by construction -- covered by a test
with thinking blocks interleaved.

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

# Per-judge model config (judge methodology v2, 2026-07-07, owner-directed; model
# moved to Opus 5.5 2026-10-11, same owner principle): a judge must be run on a
# model STRONGER than what it judges, so every v1 criterion -- including the
# length/format judge, whose PROMPT is otherwise unchanged -- runs on the current
# strongest Opus. Kept as ONE small, explicit, easily-flippable mapping (not
# hardcoded inline at each judge's call site) so a future owner call to move any
# single judge to a different model is a one-line change here, not a hunt through
# four files. Each judge module resolves its own entry at import time
# (`_MODEL = JUDGE_MODELS[CRITERION]`) and passes it explicitly to `run_judge()`.
#
# Runs judged BEFORE 2026-10-11 were scored by `claude-opus-4-8`; their
# `judge-cost.json` (`models`, `per_criterion[*].model`) and -- since 2026-10-11 --
# their `scores.json` (`per-criterion `model`) record which model actually judged
# them, so old and new scores stay distinguishable. `pricing.json` deliberately
# retains the Opus 4.8 entry for exactly that reason.
JUDGE_MODELS: dict[str, str] = {
    "factual_accuracy": "claude-opus-5-5",
    "content_selection": "claude-opus-5-5",
    "length_format": "claude-opus-5-5",
    "dedup": "claude-opus-5-5",
}

# `run_judge()`'s own default when a caller doesn't pass `model=` explicitly (e.g.
# a direct test of `run_judge()` itself, decoupled from any specific judge's
# config) -- kept in sync with JUDGE_MODELS' shared value, but NOT itself "the"
# judge model for any of the four criteria; each judge module passes its own
# `JUDGE_MODELS[CRITERION]` rather than relying on this default.
JUDGE_MODEL = "claude-opus-5-5"

# Sent on EVERY judge call as `output_config={"effort": JUDGE_EFFORT}`. Opus 5.5's
# API default is `medium` where Opus 4.8's was `high` (and thinking can no longer
# be disabled at all), so leaving this implicit would silently run the judges one
# effort level lower than every previously-committed run was judged at -- a change
# to the measuring instrument in the middle of a comparison. Explicit `high` keeps
# the judges at the same deliberation level as before.
JUDGE_EFFORT = "high"

# `run_judge()`'s own default `max_tokens`. Opus 5.5 counts THINKING tokens against
# max_tokens, so the pre-2026-10-11 defaults (1024-4096, set when the judges ran
# without thinking on Opus 4.8) truncate. Each judge still passes its own value
# explicitly (16000 for the three reasoning judges, 8000 for length_format); this
# default exists for direct callers/tests and is sized for thinking too.
DEFAULT_MAX_TOKENS = 16000

# How many times `run_judge()` will resume a `pause_turn` (the server-side tool
# loop reaching its 10-iteration limit) before giving up loudly. The docs'
# own recommendation ("Set a max_continuations limit (e.g., 5)") -- a bound, not a
# retry policy: each continuation is a real, billed API call whose usage is summed
# into the returned JudgeResult.
MAX_PAUSE_TURN_CONTINUATIONS = 5

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


class JudgeCallError(RuntimeError):
    """Base class for a judge call that cannot honestly produce a score (2026-10-11,
    Opus 5.5 `stop_reason` handling).

    Deliberately an EXCEPTION, not another `insufficient_data` degrade: a refused or
    truncated call is a real, fixable condition about the measuring instrument, and
    recording it as "the judge couldn't score this" would bury it in a field the
    comparison table treats as a blank. These propagate into `run.py`'s existing
    per-repetition containment, which records `final_status="judging_failed"` with
    the error and finalizes the run as failed -- loud, contained, and with the paid
    artifacts/events still on disk.

    `usage`/`search_count` (review-fix, reviewer L3 -- 2026-10-11) carry the token
    usage and server-side search count ALREADY ACCUMULATED when the call failed.
    Every iteration of a `pause_turn` loop -- including the one that raised -- was
    a real, BILLED call, and those numbers used to be dropped on the floor the
    moment the error was raised, so a refused/truncated/pause-limited judge call
    looked free. They are attributes rather than anything this module acts on:
    pricing lives in `harness/cost.py`, and whether a caller charges a failed
    repetition for them is the caller's policy decision (`run.py`'s containment
    deliberately records the failure and moves on; the numbers are now at least
    AVAILABLE to it, where before they were unrecoverable).

    `usage` is the same flat shape `JudgeResult.usage` uses, so it can be priced
    with `harness.cost.price_usage()` and `price_web_searches()` without
    translation; it defaults to an all-zero dict when a caller raises without
    supplying one."""

    def __init__(
        self,
        message: str,
        *,
        criterion: str,
        model: str,
        usage: dict[str, int] | None = None,
        search_count: int = 0,
    ):
        super().__init__(message)
        self.criterion = criterion
        self.model = model
        self.usage = dict(usage) if usage else dict(_EMPTY_USAGE)
        self.search_count = search_count


class JudgeRefusedError(JudgeCallError):
    """A safety-classifier decline: HTTP 200 with `stop_reason: "refusal"` and an
    (informational, possibly null) `stop_details.category` -- carried on
    `.category`/`.explanation` here so the operator can see WHICH classifier fired.

    There is DELIBERATELY no fallback to another model (no `fallbacks` parameter, no
    client-side retry on a different model): the whole point of a judge is that
    every candidate in a comparison is scored by the SAME instrument, so silently
    re-scoring one repetition on a different model would corrupt the comparison far
    more than a failed repetition does."""

    def __init__(
        self,
        *,
        criterion: str,
        model: str,
        category: str | None,
        explanation: str | None,
        usage: dict[str, int] | None = None,
        search_count: int = 0,
    ):
        detail = f" category={category!r}" if category is not None else " category=None (not reported)"
        if explanation:
            detail += f" explanation={explanation!r}"
        super().__init__(
            f"judge call for criterion {criterion!r} on model {model!r} was REFUSED by a safety "
            f"classifier (stop_reason='refusal',{detail}). NOT retried on another model -- that "
            "would change the measuring instrument mid-batch. Rephrase the judge prompt or "
            "exclude this input.",
            criterion=criterion,
            model=model,
            usage=usage,
            search_count=search_count,
        )
        self.category = category
        self.explanation = explanation


class JudgeTruncatedError(JudgeCallError):
    """`stop_reason: "max_tokens"` -- the response was cut off mid-flight, so its
    text is not a verdict. Raised rather than parsed, because a truncated response
    usually still contains SOME JSON-looking text and would otherwise be recorded
    as a formatting degrade, hiding the real (one-line-fixable) cause. On Opus 5.5
    thinking tokens count against `max_tokens`, which is exactly how a cap sized
    for a no-thinking model produces this."""

    def __init__(
        self,
        *,
        criterion: str,
        model: str,
        max_tokens: int,
        usage: dict[str, int] | None = None,
        search_count: int = 0,
    ):
        super().__init__(
            f"judge call for criterion {criterion!r} on model {model!r} was TRUNCATED "
            f"(stop_reason='max_tokens', max_tokens={max_tokens}). On this model thinking tokens "
            "count against max_tokens -- raise the judge's max_tokens rather than parsing a "
            "half-written verdict.",
            criterion=criterion,
            model=model,
            usage=usage,
            search_count=search_count,
        )
        self.max_tokens = max_tokens


class JudgePauseLimitError(JudgeCallError):
    """The server-side tool loop kept returning `pause_turn` after
    `MAX_PAUSE_TURN_CONTINUATIONS` resumes. Every iteration was a real billed call,
    so this is a bound on spend as much as on time; it should be unreachable with
    the judges' `max_uses` caps, and if it ever fires the cap/prompt is the thing to
    look at."""

    def __init__(
        self,
        *,
        criterion: str,
        model: str,
        continuations: int,
        usage: dict[str, int] | None = None,
        search_count: int = 0,
    ):
        super().__init__(
            f"judge call for criterion {criterion!r} on model {model!r} still returned "
            f"stop_reason='pause_turn' after {continuations} continuation(s) -- giving up rather "
            "than looping (and billing) unboundedly. Check the judge's web-tool max_uses caps.",
            criterion=criterion,
            model=model,
            usage=usage,
            search_count=search_count,
        )
        self.continuations = continuations


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
    For a call that was paused and resumed (`stop_reason: "pause_turn"`), this is
    the SUM across every iteration, not just the last one.

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
    `web_fetch_tool_result` blocks, `thinking` blocks (ALWAYS possible on Opus
    5.5, whose thinking cannot be disabled -- empty text under the default
    `display: "omitted"`, and sometimes carrying progress updates), and possibly
    more than one `text` block interleaved (a judge may narrate -- "Let me verify
    this claim..." -- between searches before committing to its final JSON
    verdict). Filtering on `type == "text"` is what makes thinking blocks (and
    every other block type) irrelevant here. The judge's structured
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


def _sum_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """Field-wise sum of two usage dicts -- used to accumulate usage across a
    `pause_turn` continuation chain, so a paused-and-resumed judge call reports
    (and is charged for) EVERY iteration it actually made, not just the last."""
    return {key: int(left.get(key, 0) or 0) + int(right.get(key, 0) or 0) for key in _EMPTY_USAGE}


def _extract_refusal_details(response: Any) -> tuple[str | None, str | None]:
    """Read `(stop_details.category, stop_details.explanation)` defensively.

    Both are INFORMATIONAL: `stop_details` can be null even on a real refusal, and
    `explanation` is not guaranteed present -- which is why `run_judge()` branches
    on `stop_reason` and only uses these to enrich the error message."""
    stop_details = getattr(response, "stop_details", None)
    if stop_details is None:
        return None, None
    return getattr(stop_details, "category", None), getattr(stop_details, "explanation", None)


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
    max_tokens: int = DEFAULT_MAX_TOKENS,
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

    `stop_reason` is inspected BEFORE the content is read (2026-10-11, Opus 5.5):
    a `"refusal"` or `"max_tokens"` response raises (`JudgeRefusedError` /
    `JudgeTruncatedError` -- see their docstrings for why these are errors rather
    than degrades), and a `"pause_turn"` response is resumed up to
    `MAX_PAUSE_TURN_CONTINUATIONS` times with usage and search counts summed
    across every iteration. Every raised error carries the usage and search count
    accumulated up to that point on `.usage`/`.search_count` -- those iterations
    were billed whether or not a verdict came back (review-fix, reviewer L3).
    """
    base_kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        # Explicit effort (2026-10-11): Opus 5.5 defaults to `medium` where Opus
        # 4.8 defaulted to `high`, and its thinking cannot be disabled -- so this
        # is what keeps the judges deliberating at the same level every
        # previously-committed run was judged at. NOTE what is NOT sent, all of
        # which 400 on this model: `thinking: {"type": "disabled"}`,
        # `budget_tokens`, temperature/top_p/top_k, forced `tool_choice`.
        output_config={"effort": JUDGE_EFFORT},
        # AUTOMATIC prompt caching (top-level cache_control; confirmed against
        # the prompt-caching docs 2026-07-07): the API auto-manages cache
        # breakpoints as the request's context grows. This matters ENORMOUSLY
        # for the web-tool judges: a server-side tool loop re-sends the
        # accumulated context (system + brief + every prior search/fetch
        # result) on EVERY iteration -- the first live v2 accuracy-judge smoke
        # burned 281,543 UNCACHED input tokens ($1.52) across its 8-search
        # loop, cache_read=0. With this field, iterations 2..N read the shared
        # prefix at the model's cache-read rate instead (0.05x base input on
        # Opus 5.5, half what it was on Opus 4.8). 5m TTL is ample (a judge
        # call's loop completes within minutes); writes cost 1.25x base, paid
        # back after a single loop iteration. Harmless on tool-less judges (one
        # iteration, nothing re-read).
        cache_control={"type": "ephemeral"},
    )
    if tools:
        base_kwargs["tools"] = tools

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    paused_assistant_blocks: list[Any] = []
    usage: dict[str, int] = dict(_EMPTY_USAGE)
    search_count = 0

    for _ in range(MAX_PAUSE_TURN_CONTINUATIONS + 1):
        response = client.messages.create(**base_kwargs, messages=messages)
        # Accumulate FIRST, unconditionally: every iteration below -- including the
        # ones that go on to raise -- was a real, billed call. The raising paths
        # below CARRY that accumulated usage/search_count on the error itself
        # (review-fix, reviewer L3): the comment used to promise this accounting
        # while the raise discarded it, so a refused/truncated/pause-limited call
        # looked free to anything downstream.
        usage = _sum_usage(usage, _extract_usage(response))
        search_count += _extract_search_count(response)

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            category, explanation = _extract_refusal_details(response)
            raise JudgeRefusedError(
                criterion=criterion,
                model=model,
                category=category,
                explanation=explanation,
                usage=usage,
                search_count=search_count,
            )
        if stop_reason == "max_tokens":
            raise JudgeTruncatedError(
                criterion=criterion,
                model=model,
                max_tokens=max_tokens,
                usage=usage,
                search_count=search_count,
            )
        if stop_reason != "pause_turn":
            break

        # Resume the paused server-side tool loop: send the paused assistant turn
        # back UNCHANGED (the API detects its trailing server_tool_use block and
        # resumes -- adding a "Continue." user message is explicitly wrong). On a
        # second or later pause the new blocks are appended to that SAME assistant
        # turn rather than added as another assistant message: the server is
        # resuming one single assistant turn, every block is still passed back
        # verbatim, and this needs no assumption about whether consecutive
        # same-role messages are accepted.
        paused_assistant_blocks.extend(response.content or [])
        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": list(paused_assistant_blocks)},
        ]
    else:
        raise JudgePauseLimitError(
            criterion=criterion,
            model=model,
            continuations=MAX_PAUSE_TURN_CONTINUATIONS,
            usage=usage,
            search_count=search_count,
        )

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
    "JudgeCallError",
    "JudgeRefusedError",
    "JudgeTruncatedError",
    "JudgePauseLimitError",
    "run_judge",
    "JUDGE_MODEL",
    "JUDGE_MODELS",
    "JUDGE_EFFORT",
    "DEFAULT_MAX_TOKENS",
    "MAX_PAUSE_TURN_CONTINUATIONS",
    "JSON_RESPONSE_INSTRUCTION",
    "JSON_RESPONSE_INSTRUCTION_WITH_ACCURACY_FINDINGS",
    "JSON_RESPONSE_INSTRUCTION_WITH_SELECTION_DISAGREEMENTS",
    "JSON_RESPONSE_INSTRUCTION_WITH_DEDUP_FINDINGS",
]
