"""Cost/token miner (PRD docs/prd/eval-harness.md FR-14, AC-14).

Turns the owner's one-off, by-hand transcript mine (find `span.model_request_end`
events, sum their `model_usage` token counts, split research-vs-writing at the last
`web_search` tool call) into repeatable tooling: given a Managed Agents `session_id`,
produce a structured cost breakdown in USD, by phase (research vs. writing/delivery)
and by thread (forward-compatible with a future multi-agent coordinator session that
has more than one thread).

## Why "per-thread" is the primary data source, not a hard-coded single thread

Managed Agents' `multiagent: {type: "coordinator", agents: [...]}` feature (confirmed
live against `platform.claude.com/docs/en/managed-agents/multi-agent`, 2026-07-04) runs
sub-agents in separately-queryable **session threads**, each with its own usage. The
*current* production pipeline (`deploy/managed-agent/`) is a single agent, one thread —
so today there is only ever one thread to mine. This module is nonetheless structured
around `GET /v1/sessions/{id}/threads` (enumerate threads) plus each thread's own model
request/usage events as the *primary* shape, so that if a future epic splits the
pipeline into a coordinator + sub-agents, this miner already attributes cost correctly
per thread with no rewrite — it would simply have more than one `ThreadCost` in the
breakdown, one per thread, with no further phase-boundary heuristic needed (each
sub-agent's thread *is* its own phase).

For the **single-thread case** (today, and for the foreseeable near-term), phase-level
(research vs. writing/delivery) attribution still requires the heuristic the owner used
by hand: within that one thread's events, find tool-use events, take the **last**
`web_search` tool call as the research/writing boundary, and sum the token fields from
`model_usage` (`cache_creation_input_tokens`, `cache_read_input_tokens`,
`input_tokens`, `output_tokens`) before vs. after that boundary. This heuristic is
implemented as the fallback specifically for a session with exactly one thread; a
multi-thread session attributes cost **per thread directly** instead (see
`mine_session_cost()`'s branching below) and never falls back to the single-thread
heuristic, so the code does not assume "there is exactly one thread and one heuristic
boundary" any more deeply than this one, isolated fallback path.

## Pricing

Introductory Claude Sonnet 5 pricing, current as of this build (2026-07-04) — cite and
update these constants if Anthropic's pricing changes; they are the only thing in this
module likely to go stale:
  - Input tokens:              $2.00  / 1M tokens
  - Output tokens:              $10.00 / 1M tokens
  - Cache write (creation):    $2.50  / 1M tokens
  - Cache read:                 $0.20  / 1M tokens

## Data source shape

This module does not itself make network calls — it is handed already-fetched JSON
(the Sessions/Threads API response bodies), so it is unit-testable against a fixture
without a real Anthropic API key or network access. The thin `fetch_session_cost()`
wrapper at the bottom does the actual `GET` calls using the `anthropic` SDK / `httpx`,
for the Lambda handler to call in production; the pure functions above it are what the
test suite exercises directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Pricing constants (USD per token) --------------------------------------------
#
# Introductory Claude Sonnet 5 pricing (per this task's instructions, 2026-07-04):
# $2/M input, $10/M output, cache write $2.50/M, cache read $0.20/M. Expressed here
# as per-token (not per-million) floats so the arithmetic below needs no /1e6 at each
# call site. UPDATE THESE if Anthropic's pricing changes -- they are constants, not a
# live-fetched price list, by design (PRD FR-14 asks for repeatable tooling over the
# token *accounting*, not a pricing-API integration).
PRICE_PER_INPUT_TOKEN = 2.00 / 1_000_000
PRICE_PER_OUTPUT_TOKEN = 10.00 / 1_000_000
PRICE_PER_CACHE_WRITE_TOKEN = 2.50 / 1_000_000
PRICE_PER_CACHE_READ_TOKEN = 0.20 / 1_000_000

PHASE_RESEARCH = "research"
# "writing" also covers delivery (HTML conversion, Polly narration invocation, SES
# send) in the current single-agent pipeline, since none of that is a separate LLM
# call the miner can distinguish from the writing phase's own model requests -- see
# the module docstring's phase-boundary heuristic. A future multi-agent split could
# introduce a genuinely distinct "delivery" thread/phase; this constant intentionally
# does not foreclose that.
PHASE_WRITING = "writing"
PHASE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class TokenUsage:
    """One `model_usage` event's token counts, in the Sessions API's own field names."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def cost_usd(self) -> float:
        return (
            self.input_tokens * PRICE_PER_INPUT_TOKEN
            + self.output_tokens * PRICE_PER_OUTPUT_TOKEN
            + self.cache_creation_input_tokens * PRICE_PER_CACHE_WRITE_TOKEN
            + self.cache_read_input_tokens * PRICE_PER_CACHE_READ_TOKEN
        )

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


@dataclass(frozen=True)
class PhaseCost:
    """One phase's (research / writing / unknown) token usage + derived cost."""

    phase: str
    usage: TokenUsage
    cost_usd: float


@dataclass(frozen=True)
class ThreadCost:
    """One session thread's total usage + cost, with a phase breakdown.

    `phase_breakdown` has exactly one entry per phase name that contributed any usage.
    For today's single-thread pipeline, this is populated via the
    last-`web_search`-boundary heuristic (`_split_by_last_web_search_boundary`). For a
    future multi-thread (coordinator + sub-agents) session, each THREAD's total usage
    is its own phase-equivalent, so a caller attributing cost across threads does not
    need this heuristic at all -- see `mine_session_cost()`.
    """

    thread_id: str
    usage: TokenUsage
    cost_usd: float
    phase_breakdown: tuple[PhaseCost, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SessionCostBreakdown:
    """The full structured cost breakdown for one evaluation run's session (PRD
    FR-14/FR-16: "cost broken down by phase... and by thread")."""

    session_id: str
    total_cost_usd: float
    total_usage: TokenUsage
    threads: tuple[ThreadCost, ...]
    # Session-level phase totals, aggregated across all threads' phase breakdowns
    # (convenience view -- for the common single-thread case this is identical to
    # threads[0].phase_breakdown; for a multi-thread session, each thread's overall
    # usage is folded in under PHASE_UNKNOWN unless that thread itself reports a
    # phase breakdown).
    phase_totals: tuple[PhaseCost, ...] = field(default_factory=tuple)


def _usage_from_event(event: dict) -> TokenUsage:
    """Extract a TokenUsage from one `model_usage`-bearing event dict. Missing fields
    default to 0 -- a partial/older event shape must not raise, only under-count
    (never over-count) that one event's contribution."""
    usage = event.get("model_usage") or event.get("usage") or {}
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
    )


def _is_model_request_end(event: dict) -> bool:
    return event.get("type") == "span.model_request_end"


def _is_web_search_tool_use(event: dict) -> bool:
    """True for a tool-use event invoking `web_search` (the research/writing boundary
    marker per the owner's manual mining procedure). Tolerant of a couple of plausible
    event shapes (`tool_name` at the top level, or nested under `tool_use`) since the
    exact beta event schema may drift -- see the module docstring's "hard to verify
    without a real API call" caveat, also flagged in this task's final report."""
    event_type = event.get("type", "")
    if "tool_use" not in event_type and "tool_call" not in event_type:
        return False
    tool_name = event.get("tool_name") or (event.get("tool_use") or {}).get("name") or ""
    return tool_name == "web_search"


def _split_by_last_web_search_boundary(events: list[dict]) -> tuple[PhaseCost, PhaseCost]:
    """The single-thread phase-attribution fallback (module docstring): find the LAST
    `web_search` tool-use event in `events` (assumed chronologically ordered, as the
    Sessions API returns them) and sum `model_request_end` events' `model_usage`
    before vs. after it. Everything up to and including the boundary is "research";
    everything after is "writing" (which, in today's single-agent pipeline, also
    covers delivery -- see PHASE_WRITING's docstring).

    If no `web_search` event is found at all (e.g. a run that never searched, or an
    event shape this heuristic doesn't recognize -- see `_is_web_search_tool_use`'s
    tolerance note), everything is attributed to PHASE_UNKNOWN rather than guessed
    into research or writing -- an honest "we couldn't find the boundary" signal
    rather than a silently wrong split.
    """
    last_boundary_index: int | None = None
    for i, event in enumerate(events):
        if _is_web_search_tool_use(event):
            last_boundary_index = i

    if last_boundary_index is None:
        unknown_usage = TokenUsage()
        for event in events:
            if _is_model_request_end(event):
                unknown_usage = unknown_usage + _usage_from_event(event)
        return (
            PhaseCost(phase=PHASE_UNKNOWN, usage=unknown_usage, cost_usd=unknown_usage.cost_usd()),
            PhaseCost(phase=PHASE_UNKNOWN, usage=TokenUsage(), cost_usd=0.0),
        )

    research_usage = TokenUsage()
    writing_usage = TokenUsage()
    for i, event in enumerate(events):
        if not _is_model_request_end(event):
            continue
        usage = _usage_from_event(event)
        if i <= last_boundary_index:
            research_usage = research_usage + usage
        else:
            writing_usage = writing_usage + usage

    return (
        PhaseCost(phase=PHASE_RESEARCH, usage=research_usage, cost_usd=research_usage.cost_usd()),
        PhaseCost(phase=PHASE_WRITING, usage=writing_usage, cost_usd=writing_usage.cost_usd()),
    )


def mine_thread_cost(thread_id: str, events: list[dict]) -> ThreadCost:
    """Mine one thread's total usage + phase breakdown from its event list.

    `events` is the thread's chronologically-ordered event stream (whatever shape
    `GET /v1/sessions/{id}/threads/{thread_id}` or the session's own embedded thread
    event log returns -- see `fetch_session_cost()` below for the live-call side).
    """
    total_usage = TokenUsage()
    for event in events:
        if _is_model_request_end(event):
            total_usage = total_usage + _usage_from_event(event)

    research_phase, writing_phase = _split_by_last_web_search_boundary(events)
    if research_phase.phase == PHASE_UNKNOWN:
        # No web_search boundary was found at all -- report a single PHASE_UNKNOWN
        # entry (the "everything" bucket), not two (see
        # _split_by_last_web_search_boundary's no-boundary branch, which returns an
        # empty second placeholder purely to keep the function's return arity fixed).
        phase_breakdown = (research_phase,)
    else:
        phase_breakdown = tuple(p for p in (research_phase, writing_phase) if p.usage.cost_usd() > 0)
        if not phase_breakdown:
            # Both phases had zero usage (e.g. a web_search boundary exists but no
            # model_request_end events at all) -- still report both so a caller can
            # see the split exists, just with zero cost on each side.
            phase_breakdown = (research_phase, writing_phase)

    return ThreadCost(
        thread_id=thread_id,
        usage=total_usage,
        cost_usd=total_usage.cost_usd(),
        phase_breakdown=phase_breakdown,
    )


def mine_session_cost(session_id: str, threads: dict[str, list[dict]]) -> SessionCostBreakdown:
    """Mine the full cost breakdown for a session given its threads' event lists.

    `threads` maps thread_id -> that thread's event list. For today's single-agent
    pipeline this dict has exactly one entry, and the single-thread
    last-`web_search`-boundary heuristic (`mine_thread_cost`) supplies the phase
    breakdown. For a hypothetical multi-thread (coordinator + sub-agents) session,
    each thread is mined independently and attributed to its OWN phase breakdown
    (each thread's own heuristic result) -- cost is never pooled across threads before
    phase-splitting, so a future multi-agent epic gets correct per-thread attribution
    with no change to this function's structure, only more entries in `threads`.
    """
    thread_costs = tuple(mine_thread_cost(thread_id, events) for thread_id, events in threads.items())

    total_usage = TokenUsage()
    for tc in thread_costs:
        total_usage = total_usage + tc.usage

    phase_totals_by_name: dict[str, TokenUsage] = {}
    for tc in thread_costs:
        for phase_cost in tc.phase_breakdown:
            phase_totals_by_name[phase_cost.phase] = phase_totals_by_name.get(phase_cost.phase, TokenUsage()) + phase_cost.usage

    phase_totals = tuple(
        PhaseCost(phase=name, usage=usage, cost_usd=usage.cost_usd())
        for name, usage in phase_totals_by_name.items()
    )

    return SessionCostBreakdown(
        session_id=session_id,
        total_cost_usd=total_usage.cost_usd(),
        total_usage=total_usage,
        threads=thread_costs,
        phase_totals=phase_totals,
    )


def fetch_session_cost(anthropic_api_key: str, session_id: str, *, base_url: str | None = None) -> SessionCostBreakdown:
    """Live entry point: fetch a session's threads + events from the Anthropic Sessions
    API and mine the cost breakdown.

    Deliberately thin -- all the actual logic is in the pure functions above, which
    the test suite exercises directly against a fixture with no network access. This
    function is the one piece that is NOT unit-tested against a real API call (per
    this task's instructions); it is a straightforward HTTP client wrapper, isolated
    here so it's easy to swap/mock at the Lambda-handler level.

    Endpoint shapes used (beta, `managed-agents-2026-04-01` header, matching the rest
    of this repo's Managed Agents API usage -- see deploy/managed-agent/README.md):
      - GET /v1/sessions/{session_id}/threads       -> enumerate thread ids
      - GET /v1/sessions/{session_id}/threads/{tid} -> that thread's event stream

    NOTE for a future maintainer: this beta surface was not independently re-verified
    against a live API call while building this miner (no live session existed to
    mine against at build time) -- see this task's final report for the explicit flag
    on this. If the actual response shape differs (e.g. events nested under a
    different key, or threads enumerated differently), only this function and
    `_usage_from_event`/`_is_web_search_tool_use`'s tolerance need to change; the
    phase-splitting and cost-arithmetic logic above is independent of the exact HTTP
    shape.
    """
    import httpx

    headers = {
        "x-api-key": anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "managed-agents-2026-04-01",
    }
    root = (base_url or "https://api.anthropic.com").rstrip("/")

    with httpx.Client(base_url=root, headers=headers, timeout=30.0) as client:
        threads_response = client.get(f"/v1/sessions/{session_id}/threads")
        threads_response.raise_for_status()
        thread_ids = [t["id"] for t in threads_response.json().get("data", [])]

        threads: dict[str, list[dict]] = {}
        for thread_id in thread_ids:
            events_response = client.get(f"/v1/sessions/{session_id}/threads/{thread_id}")
            events_response.raise_for_status()
            threads[thread_id] = events_response.json().get("events", [])

    return mine_session_cost(session_id, threads)


__all__ = [
    "PRICE_PER_INPUT_TOKEN",
    "PRICE_PER_OUTPUT_TOKEN",
    "PRICE_PER_CACHE_WRITE_TOKEN",
    "PRICE_PER_CACHE_READ_TOKEN",
    "PHASE_RESEARCH",
    "PHASE_WRITING",
    "PHASE_UNKNOWN",
    "TokenUsage",
    "PhaseCost",
    "ThreadCost",
    "SessionCostBreakdown",
    "mine_thread_cost",
    "mine_session_cost",
    "fetch_session_cost",
]
