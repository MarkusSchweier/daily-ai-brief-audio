"""Per-thread, model-aware, cache-bucket-aware cost attribution (ADR-0016 D2).

Turns a candidate run's per-thread token *usage* -- `GET /v1/sessions/{id}/threads`,
confirmed live shape: `{input_tokens, output_tokens, cache_read_input_tokens,
cache_creation: {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}}`, NOTE the
nested `cache_creation` bucket -- this is a DIFFERENT shape from the old
`deploy/eval/eval_core/cost_miner.py`'s flat `model_usage` event fields -- into a
structured cost breakdown, by multiplying each thread's usage against a small,
git-pinned, model-aware price table (`pricing.json`).

## Why per-thread, not per-phase-heuristic

The old miner's "last web_search boundary" heuristic existed only because a
single-agent pipeline has exactly one thread and needed SOME way to split
research-vs-writing cost. A multi-agent candidate already runs each phase as its
own separately-queryable session THREAD -- so per-thread IS the natural cost unit,
with no heuristic needed at all (each sub-agent's thread already IS its own
phase, exactly as that module's own docstring predicted). This module is built
around the threads endpoint as the PRIMARY data source for exactly that reason.

## Model resolution: candidate declaration first, thread's own embedded agent second

Per ADR-0016 D2 / the task instructions: "Model per thread resolves from the
candidate declaration (multiagent.json roster / model.txt), falling back to
GET /v1/agents/{id} shape if needed." Concretely:
  - PRIMARY: match each thread's `agent.id` against the loaded
    `candidate_sync.loader.CandidateDeclaration`'s own agent_ids (the top-level
    agent = the coordinator/sole agent; each `sub_agents[i].agent_id` from
    `multiagent.json`'s roster) -- this is the git-tracked SOURCE OF TRUTH for what
    model a candidate's declaration SAYS each agent runs.
  - FALLBACK: if no `candidate_declaration` is given, or a thread's `agent.id`
    doesn't match anything in it (an unexpected/orphaned thread), read the model
    straight off the thread's OWN embedded `agent` object -- `GET
    /v1/sessions/{id}/threads` embeds the exact same `{id, name, description,
    model: {id: ...}, ...}` shape `GET /v1/agents/{id}` itself returns, so this is
    literally "the GET /v1/agents/{id} shape," just already present on the thread
    record with no second API call needed.

This module makes NO network calls of its own except `fetch_threads()` at the
bottom (mirroring `eval_core/cost_miner.py`'s original "pure functions above, thin
network wrapper below" split, so the cost math is unit-testable with no API key or
network access).

## Per-request rate cards (2026-10-11, Claude Haiku 5.5)

Most model families price every token the same way regardless of request size, so a
thread's AGGREGATE usage is enough to cost it. Claude Haiku 5.5 is not one of them:
it has TWO rate cards and the pricing page bills each request on the card chosen by
THAT request's own prompt length (`input + cache_read + cache_creation` tokens,
threshold 100,000 -- "a request over the threshold pays the higher prices even when
part of its prompt is a cache hit, and earlier requests keep the prices they were
billed at"). A thread that mixes small and large requests therefore CANNOT be priced
from its aggregate usage at all -- any single-card guess is simply wrong, by up to 5x
in either direction.

So for a family whose `pricing.json` tier carries a `long_prompt` card, this module
prices the thread PER REQUEST, from the session event LIST `run.py` already fetches
for artifact retrieval. (Note the on-disk shape differs: `run_store.write_events()`
WRAPS that list as `{"events": [...]}` in `events.json`, so a caller reading the
file back passes either the wrapper or `["events"]` -- `extract_request_usages()`
accepts both. Review-fix, reviewer L2: this docstring used to claim the parameter
and the file held the same thing.) Every `span.model_request_end` event carries
`model_usage: {input_tokens, output_tokens, cache_read_input_tokens,
cache_creation_input_tokens}` (FLAT cache_creation, no 5m/1h split -- unlike the
threads endpoint's nested buckets). Guard rails, all fail-loud
(`PerRequestPricingError`), never a silent guess:

  - **Integrity check.** The per-request usages must sum to the thread's own usage
    totals EXACTLY (per-request `cache_creation` sum == thread 5m + 1h buckets).
    Verified against the stored runs on 2026-10-11: for every SINGLE-thread run the
    sums match to the token (29 requests for `haiku-swap-hardened`, 27 for
    `production-baseline`, 18 for `haiku-swap`).
  - **1h cache writes.** If the thread's 1h bucket is non-zero, raise -- the flat
    per-request `cache_creation_input_tokens` field cannot be split back into TTL
    buckets, so the 2x-vs-1.25x write rate could not be attributed honestly.
  - **No events, no price.** If a rate-carded model's events weren't supplied, raise.
  - **Multi-thread sessions are refused.** `span.model_request_end` carries NO thread
    identifier (confirmed 2026-10-11 across all six stored runs: its only keys are
    `id`, `is_error`, `model_request_start_id`, `model_usage`, `processed_at`, `type`;
    only the `session.thread_*` / `agent.thread_message_*` event families carry a
    `session_thread_id`). Worse, on the stored MULTI-thread runs the retrieved event
    list covers only part of the session -- its per-request sums fall far short of the
    threads' totals -- so there is no honest way to attribute a request to a thread.
    Rate-carded pricing is therefore supported only for single-thread sessions; a
    multi-thread session containing a rate-carded agent raises (the integrity check
    would catch it anyway, but the dedicated error says WHY).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

# CandidateDeclaration is only used for type hints / attribute access below -- the
# caller (harness/run.py, or a test) is responsible for having deploy/candidates/ on
# sys.path already (this package's entry-point scripts set that up; see their own
# headers), matching this repo's established "each script wires its own sys.path"
# convention rather than this library module doing it implicitly.
from candidate_sync.loader import CandidateDeclaration  # noqa: E402

# Phase/role vocabulary drawn directly from the daily-ai-brief skill's own numbered
# Daily workflow steps (see deploy/candidates/multiagent-aggressive-haiku/multiagent.json's
# roster descriptions) -- checked longest-alias-first so "listening-script" is never
# mis-matched by a shorter, unrelated substring.
_PHASE_LABELS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("listening-script", "listening_script"), "listening-script"),
    (("research",), "research"),
    (("selection",), "selection"),
    (("writing",), "writing"),
    (("coordinator", "orchestrat"), "coordinator"),
)


class PricingError(RuntimeError):
    """Base class for pricing-table problems -- a clear, fail-loud error rather
    than a raw KeyError/ValueError deep in cost math."""


class UnknownModelPriceError(PricingError):
    """Raised when a model id (or its declared aliases) has no entry anywhere in
    `pricing.json` -- a candidate references a model this table has never heard
    of, which must fail loud, not silently price as $0."""


class PricingDriftError(PricingError):
    """Raised when a model family HAS entries in `pricing.json`, but none of its
    tiers' [effective_from, effective_until] window covers the requested date --
    e.g. today is past every known tier's expiry (ADR-0016 D2's concrete example:
    a tier defined with an `effective_until` and no successor tier; querying a date
    after that expiry would raise this)."""


class MalformedPricingTableError(PricingError):
    """Raised when `pricing.json` itself is structurally wrong in a way that would
    otherwise price something at (or near) ZERO in silence -- review-fix, reviewer
    M1 (2026-10-11). Two concrete holes it closes, both previously silent:

      - a missing `cache_multipliers` key (globally or left unmerged by a per-model
        override) used to `.get(key, 0.0)` into a 0.0 multiplier, pricing that
        whole cache bucket as FREE. On a real run that is most of the bill;
      - a `long_prompt` card with a missing/zero `threshold_prompt_tokens` used to
        read as "no second rate card at all", so a rate-carded model was
        aggregate-priced on its CHEAP card -- measured 4.7x under on a real Haiku
        5.5 run -- and missing `long_prompt` rates priced every long-prompt request
        at $0.

    A pricing table is a small, hand-reviewed file; the only honest response to a
    malformed one is to refuse to produce a number."""


class PerRequestPricingError(PricingError):
    """Raised when a model priced by PER-REQUEST rate cards (a `pricing.json` tier
    with a `long_prompt` card -- Claude Haiku 5.5) cannot be priced from the data
    actually available: no session events supplied, a multi-thread session (the
    events carry no thread identifier), an empty request list, a non-zero 1h
    cache-write bucket (not attributable per request), or per-request usages that
    don't sum to the thread's own totals. Always a fail-loud refusal to guess --
    see the module docstring's "Per-request rate cards" section."""


# --- Token usage -----------------------------------------------------------------


@dataclass(frozen=True)
class ThreadUsage:
    """One thread's token usage, in the Sessions/Threads API's own confirmed field
    names -- the nested `cache_creation` shape, NOT the old cost_miner's flat
    `cache_creation_input_tokens` single bucket."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0

    @classmethod
    def from_api_usage(cls, usage: dict[str, Any] | None) -> "ThreadUsage":
        """Build from a raw `usage` dict as returned by `GET
        /v1/sessions/{id}/threads` (per-thread) or `GET /v1/sessions/{id}` (session
        total) -- both share the same shape. Missing fields default to 0 -- a
        partial/older shape must not raise, only under-count (never over-count)."""
        usage = usage or {}
        cache_creation = usage.get("cache_creation") or {}
        return cls(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_creation_5m_input_tokens=int(cache_creation.get("ephemeral_5m_input_tokens", 0) or 0),
            cache_creation_1h_input_tokens=int(cache_creation.get("ephemeral_1h_input_tokens", 0) or 0),
        )

    def __add__(self, other: "ThreadUsage") -> "ThreadUsage":
        return ThreadUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_5m_input_tokens=self.cache_creation_5m_input_tokens + other.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens=self.cache_creation_1h_input_tokens + other.cache_creation_1h_input_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_5m_input_tokens": self.cache_creation_5m_input_tokens,
            "cache_creation_1h_input_tokens": self.cache_creation_1h_input_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThreadUsage":
        return cls(
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(data.get("cache_read_input_tokens", 0) or 0),
            cache_creation_5m_input_tokens=int(data.get("cache_creation_5m_input_tokens", 0) or 0),
            cache_creation_1h_input_tokens=int(data.get("cache_creation_1h_input_tokens", 0) or 0),
        )


# --- Per-request usage (rate-carded models only) -----------------------------------

# The two rate cards a `long_prompt`-carrying tier can bill a request on -- the keys
# of `ThreadCost.request_card_counts` and the labels in its error messages.
CARD_STANDARD = "standard"
CARD_LONG_PROMPT = "long_prompt"

# The one session-event type that carries a request's own token usage.
_MODEL_REQUEST_END_EVENT = "span.model_request_end"


@dataclass(frozen=True)
class RequestUsage:
    """ONE model request's token usage, in the session events' own confirmed field
    names: `span.model_request_end.model_usage` = `{input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens}`.

    Note the FLAT `cache_creation_input_tokens` -- the event shape carries no 5m/1h
    split (unlike the threads endpoint's nested `cache_creation` buckets), which is
    exactly why `verify_request_usages_match_thread()` refuses to price a thread
    whose 1h bucket is non-zero."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """The request's PROMPT length, as the pricing page defines it for rate-card
        selection: "A request's prompt length counts all of its input tokens,
        including cache reads and cache writes" -- so all three input-side fields,
        never output."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    @classmethod
    def from_model_usage(cls, model_usage: dict[str, Any] | None) -> "RequestUsage":
        """Build from one event's raw `model_usage` dict. Missing fields default to
        0 -- a partial/older shape must not raise here; the integrity check against
        the thread's own totals is what catches a shape this under-reads."""
        model_usage = model_usage or {}
        return cls(
            input_tokens=int(model_usage.get("input_tokens", 0) or 0),
            output_tokens=int(model_usage.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(model_usage.get("cache_read_input_tokens", 0) or 0),
            cache_creation_input_tokens=int(model_usage.get("cache_creation_input_tokens", 0) or 0),
        )


def normalize_events(events: "list[dict[str, Any]] | dict[str, Any]") -> list[dict[str, Any]]:
    """Accept EITHER shape the session events travel in (review-fix, reviewer L2):

      - the bare list `candidate_sync.trigger`'s `CandidateRunResult.events` carries
        (what `run.py` passes straight into `mine_session_cost()`), or
      - the `{"events": [...]}` WRAPPER `harness.run_store.write_events()` writes to
        `repetitions/<NN>/events.json`, i.e. what anything re-reading that file gets.

    The two were conflated in this module's docstrings, which invited a caller to
    hand the wrapper dict straight in -- where iterating it would have yielded its
    KEYS ("events") and silently found zero model requests. Anything else
    (including None) normalizes to an empty list; callers that must fail loud on
    "no events" already do so by request count."""
    if isinstance(events, dict):
        wrapped = events.get("events")
        return list(wrapped) if isinstance(wrapped, list) else []
    return list(events) if events else []


def extract_request_usages(events: "list[dict[str, Any]] | dict[str, Any]") -> list[RequestUsage]:
    """Every `span.model_request_end` event's `model_usage`, in event order -- one
    `RequestUsage` per model request the session made. Events of every other type
    are ignored; an event of this type with no `model_usage` at all is ignored too
    (it contributes no tokens, and counting it as a zero-token request would
    mis-report the per-card request counts).

    `events` may be the bare list OR `events.json`'s `{"events": [...]}` wrapper
    (see `normalize_events()`)."""
    usages: list[RequestUsage] = []
    for event in normalize_events(events):
        if event.get("type") != _MODEL_REQUEST_END_EVENT:
            continue
        model_usage = event.get("model_usage")
        if not model_usage:
            continue
        usages.append(RequestUsage.from_model_usage(model_usage))
    return usages


def verify_request_usages_match_thread(
    requests: list[RequestUsage], usage: ThreadUsage, *, model_id: str, thread_id: str = ""
) -> None:
    """Fail loud (`PerRequestPricingError`) unless `requests` accounts for EXACTLY
    the thread's own usage totals -- the integrity check that makes per-request
    pricing trustworthy instead of plausible (see the module docstring).

    Two distinct refusals: a non-zero 1h cache-write bucket (the flat per-request
    `cache_creation_input_tokens` cannot be split back into TTL buckets, so the
    2x-vs-1.25x write rate could not be attributed honestly), and any mismatch
    between the per-request sums and the thread totals (events missing, duplicated,
    or belonging to a different thread)."""
    where = f" (thread {thread_id})" if thread_id else ""
    if usage.cache_creation_1h_input_tokens:
        raise PerRequestPricingError(
            f"cannot price {model_id!r}{where} per request: the thread reports "
            f"{usage.cache_creation_1h_input_tokens} 1-HOUR cache-write tokens, but a "
            "span.model_request_end event's model_usage carries only a FLAT "
            "cache_creation_input_tokens with no TTL split -- the 2x 1h write rate cannot be "
            "attributed to individual requests. Price this thread by hand, or re-run without "
            "1h cache writes."
        )

    summed_input = sum(r.input_tokens for r in requests)
    summed_output = sum(r.output_tokens for r in requests)
    summed_cache_read = sum(r.cache_read_input_tokens for r in requests)
    summed_cache_creation = sum(r.cache_creation_input_tokens for r in requests)
    thread_cache_creation = usage.cache_creation_5m_input_tokens + usage.cache_creation_1h_input_tokens

    mismatches = []
    if summed_input != usage.input_tokens:
        mismatches.append(f"input_tokens {summed_input} != {usage.input_tokens}")
    if summed_output != usage.output_tokens:
        mismatches.append(f"output_tokens {summed_output} != {usage.output_tokens}")
    if summed_cache_read != usage.cache_read_input_tokens:
        mismatches.append(f"cache_read_input_tokens {summed_cache_read} != {usage.cache_read_input_tokens}")
    if summed_cache_creation != thread_cache_creation:
        mismatches.append(f"cache_creation {summed_cache_creation} != {thread_cache_creation} (5m+1h)")

    if mismatches:
        raise PerRequestPricingError(
            f"cannot price {model_id!r}{where} per request: the {len(requests)} "
            "span.model_request_end event(s) do NOT sum to the thread's own usage totals -- "
            + "; ".join(mismatches)
            + ". The events are incomplete or belong to a different thread; refusing to guess "
            "a per-request cost from a partial trace."
        )


# --- Pricing table -----------------------------------------------------------------


@dataclass(frozen=True)
class PriceTier:
    """One resolved price tier for one model family, ready to cost a `ThreadUsage`.

    The three `long_prompt_*` fields (2026-10-11) are the tier's OPTIONAL second
    rate card -- present only for a family Anthropic prices by prompt length
    (Claude Haiku 5.5). When present, this tier can NOT be costed from aggregate
    usage at all (`cost_usd()` raises); price it per request via
    `price_requests()`. They default to None so every other family, and every
    hand-built `PriceTier` in a test, is unaffected."""

    model_family: str
    label: str
    input_per_million_usd: float
    output_per_million_usd: float
    cache_write_5m_multiplier: float
    cache_write_1h_multiplier: float
    cache_read_multiplier: float
    effective_from: str | None
    effective_until: str | None
    source_url: str
    captured_on: str
    long_prompt_threshold_tokens: int | None = None
    long_prompt_input_per_million_usd: float | None = None
    long_prompt_output_per_million_usd: float | None = None

    @property
    def has_long_prompt_card(self) -> bool:
        """True when this tier bills PER REQUEST off two rate cards -- the single
        flag every caller branches on."""
        return self.long_prompt_threshold_tokens is not None

    def _cost_on_card(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int,
        cache_creation_5m_input_tokens: int,
        cache_creation_1h_input_tokens: int,
        input_per_million_usd: float,
        output_per_million_usd: float,
    ) -> float:
        """The one piece of cost arithmetic in this module: tokens x one rate
        card's own rates, with the cache multipliers applied to THAT card's base
        input rate (`pricing.json`'s schema note, layer 3)."""
        input_rate = input_per_million_usd / 1_000_000
        output_rate = output_per_million_usd / 1_000_000
        return (
            input_tokens * input_rate
            + output_tokens * output_rate
            + cache_creation_5m_input_tokens * input_rate * self.cache_write_5m_multiplier
            + cache_creation_1h_input_tokens * input_rate * self.cache_write_1h_multiplier
            + cache_read_input_tokens * input_rate * self.cache_read_multiplier
        )

    def cost_usd(self, usage: ThreadUsage) -> float:
        """Full-precision (unrounded) USD cost for an AGGREGATE `usage` under this
        tier. Callers that need a displayable number round explicitly -- rounding
        INDIVIDUAL thread costs before summing them into a total produces a total
        that can be off by a cent's fraction from summing the raw, unrounded costs
        first (see `mine_session_cost()`'s own docstring / the golden test for the
        concrete case this matters for).

        Raises `PerRequestPricingError` for a tier with a `long_prompt` rate card:
        aggregate usage cannot say which card each request was billed on, and
        picking one would be wrong by up to 5x. Use `price_requests()`."""
        if self.has_long_prompt_card:
            raise PerRequestPricingError(
                f"model family {self.model_family!r} is priced on TWO rate cards by per-request "
                f"prompt length (threshold {self.long_prompt_threshold_tokens} tokens) -- it cannot "
                "be priced from aggregate/flat usage, which cannot say which card each request fell "
                "on. Price it per request from the session events (harness.cost.price_requests)."
            )
        return self._cost_on_card(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cache_creation_5m_input_tokens=usage.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens=usage.cache_creation_1h_input_tokens,
            input_per_million_usd=self.input_per_million_usd,
            output_per_million_usd=self.output_per_million_usd,
        )

    def card_for(self, request: RequestUsage) -> str:
        """Which rate card bills `request`: the long-prompt card iff its prompt
        length is STRICTLY GREATER than the threshold (a request of exactly
        100,000 tokens pays the standard card -- the page's threshold is "prompts
        up to 100,000 tokens" for the cheap card)."""
        if not self.has_long_prompt_card:
            return CARD_STANDARD
        assert self.long_prompt_threshold_tokens is not None  # narrowed by has_long_prompt_card
        return CARD_LONG_PROMPT if request.prompt_tokens > self.long_prompt_threshold_tokens else CARD_STANDARD

    def cost_usd_for_request(self, request: RequestUsage) -> float:
        """Full-precision cost of ONE request, on whichever card `card_for()`
        picks. The request's flat `cache_creation_input_tokens` is billed at the
        5-minute write rate -- sound precisely because `price_requests()` only ever
        runs after `verify_request_usages_match_thread()` has confirmed the
        thread's 1h bucket is zero."""
        card = self.card_for(request)
        if card == CARD_LONG_PROMPT:
            # Unreachable for a tier built by `resolve_price_tier()` (it validates
            # the card all-or-nothing -- reviewer M1), but a hand-built PriceTier
            # could still carry a threshold with no rates, and `or 0.0` used to
            # price those requests at exactly $0. Refuse instead.
            if self.long_prompt_input_per_million_usd is None or self.long_prompt_output_per_million_usd is None:
                raise MalformedPricingTableError(
                    f"model family {self.model_family!r} declares a long-prompt threshold "
                    f"({self.long_prompt_threshold_tokens} tokens) but not both long-prompt rates "
                    f"(input={self.long_prompt_input_per_million_usd}, "
                    f"output={self.long_prompt_output_per_million_usd}) -- refusing to price a "
                    "long-prompt request at $0."
                )
            input_per_million = float(self.long_prompt_input_per_million_usd)
            output_per_million = float(self.long_prompt_output_per_million_usd)
        else:
            input_per_million = self.input_per_million_usd
            output_per_million = self.output_per_million_usd
        return self._cost_on_card(
            input_tokens=request.input_tokens,
            output_tokens=request.output_tokens,
            cache_read_input_tokens=request.cache_read_input_tokens,
            cache_creation_5m_input_tokens=request.cache_creation_input_tokens,
            cache_creation_1h_input_tokens=0,
            input_per_million_usd=input_per_million,
            output_per_million_usd=output_per_million,
        )


def load_pricing_table(path: str | None = None) -> dict[str, Any]:
    """Load `pricing.json` (default: the copy next to this package) as a plain
    dict. No caching -- this is called at most a few times per CLI invocation."""
    import json
    from pathlib import Path

    if path is None:
        path = str(Path(__file__).resolve().parent.parent / "pricing.json")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _resolve_model_family(pricing_table: dict[str, Any], model_id: str) -> str:
    models = pricing_table.get("models", {})
    for family, entry in models.items():
        if model_id in entry.get("aliases", []):
            return family
    raise UnknownModelPriceError(
        f"no pricing entry (nor alias) found for model id {model_id!r} in pricing.json -- "
        "add a tier for this model (or an alias pointing at an existing family) before "
        "costing a run that uses it"
    )


def _date_in_window(on_date: date, effective_from: str | None, effective_until: str | None) -> bool:
    if effective_from is not None and on_date < date.fromisoformat(effective_from):
        return False
    if effective_until is not None and on_date > date.fromisoformat(effective_until):
        return False
    return True


REQUIRED_CACHE_MULTIPLIER_KEYS: tuple[str, ...] = ("write_5m", "write_1h", "read")

# The three fields a `long_prompt` rate card must carry, all or nothing.
REQUIRED_LONG_PROMPT_KEYS: tuple[str, ...] = (
    "threshold_prompt_tokens",
    "input_per_million_usd",
    "output_per_million_usd",
)


def _resolve_cache_multipliers(
    pricing_table: dict[str, Any], entry: dict[str, Any], *, model_family: str
) -> dict[str, float]:
    """The table's global `cache_multipliers` with the model entry's OWN optional
    `cache_multipliers` merged over it, key by key (`pricing.json`'s schema note,
    layer 2). A partial override (`{"read": 0.05}` -- Opus 5.5 / Sonnet 5.5, whose
    cache READS are 0.05x base input while their writes keep the usual 1.25x/2x)
    therefore inherits every ratio it doesn't mention.

    FAIL LOUD on a missing ratio (review-fix, reviewer M1): all three of
    `REQUIRED_CACHE_MULTIPLIER_KEYS` must be present AFTER the merge. The previous
    code read them with `.get(key, 0.0)` at the call site, so a typo'd or
    forgotten key priced that entire cache bucket as FREE -- and cache tokens are
    the dominant cost on every run this harness measures (a brief's cache-read
    bucket alone has been >4M tokens), so the resulting number would have been
    confidently, invisibly wrong. Every value must also be a non-negative
    number."""
    merged = dict(pricing_table.get("cache_multipliers", {}))
    merged.update(entry.get("cache_multipliers", {}))

    missing = [key for key in REQUIRED_CACHE_MULTIPLIER_KEYS if key not in merged]
    if missing:
        raise MalformedPricingTableError(
            f"pricing.json is malformed for model family {model_family!r}: cache multiplier(s) "
            f"{', '.join(missing)} missing after merging the per-model override over the global "
            f"`cache_multipliers` (present: {sorted(merged) or 'none'}). Every one of "
            f"{', '.join(REQUIRED_CACHE_MULTIPLIER_KEYS)} is required -- defaulting a missing one "
            "to 0.0 would price that whole cache bucket as FREE."
        )

    resolved: dict[str, float] = {}
    for key in REQUIRED_CACHE_MULTIPLIER_KEYS:
        value = merged[key]
        try:
            numeric = float(value)
        except (TypeError, ValueError) as e:
            raise MalformedPricingTableError(
                f"pricing.json is malformed for model family {model_family!r}: cache multiplier "
                f"{key!r} is {value!r}, which is not a number"
            ) from e
        if numeric < 0:
            raise MalformedPricingTableError(
                f"pricing.json is malformed for model family {model_family!r}: cache multiplier "
                f"{key!r} is negative ({numeric})"
            )
        resolved[key] = numeric
    return resolved


def _resolve_long_prompt_card(tier: dict[str, Any], *, model_family: str) -> dict[str, Any] | None:
    """The tier's OPTIONAL `long_prompt` second rate card, validated all-or-nothing
    (review-fix, reviewer M1), or None when the tier declares no such card at all.

    A tier that carries a `long_prompt` KEY must carry all three of
    `REQUIRED_LONG_PROMPT_KEYS`, with a threshold strictly greater than zero. The
    previous code read the threshold as
    `int(lp["threshold_prompt_tokens"]) if lp.get("threshold_prompt_tokens") else None`,
    so a missing/zero/null threshold made the whole card VANISH: the model was then
    aggregate-priced on its cheap card (measured 4.7x under on a real Haiku 5.5
    run), and a missing rate priced every long-prompt request at $0 via
    `float(... or 0.0)`. Both now refuse to produce a number instead."""
    if "long_prompt" not in tier:
        return None

    card = tier["long_prompt"]
    label = tier.get("label", "")
    where = f"model family {model_family!r}, tier {label!r}"
    if not isinstance(card, dict):
        raise MalformedPricingTableError(
            f"pricing.json is malformed for {where}: `long_prompt` is {card!r}, which is not an "
            "object. Either declare the full rate card (threshold + both rates) or remove the key "
            "entirely -- a half-declared card silently prices the model on the WRONG card."
        )

    missing = [key for key in REQUIRED_LONG_PROMPT_KEYS if card.get(key) is None]
    if missing:
        raise MalformedPricingTableError(
            f"pricing.json is malformed for {where}: `long_prompt` is missing {', '.join(missing)}. "
            f"A declared long-prompt card requires all of {', '.join(REQUIRED_LONG_PROMPT_KEYS)} -- "
            "a missing threshold made the card vanish (the model was then aggregate-priced on its "
            "CHEAP card, 4.7x under on a real run) and a missing rate priced long-prompt requests "
            "at $0."
        )

    try:
        threshold = int(card["threshold_prompt_tokens"])
        input_rate = float(card["input_per_million_usd"])
        output_rate = float(card["output_per_million_usd"])
    except (TypeError, ValueError) as e:
        raise MalformedPricingTableError(
            f"pricing.json is malformed for {where}: `long_prompt` carries a non-numeric value "
            f"({card!r})"
        ) from e

    if threshold <= 0:
        raise MalformedPricingTableError(
            f"pricing.json is malformed for {where}: `long_prompt.threshold_prompt_tokens` is "
            f"{threshold}, which must be greater than zero -- a zero/negative threshold cannot "
            "select a rate card (and previously read as 'no long-prompt card at all', silently "
            "aggregate-pricing the model on its cheap card)."
        )
    if input_rate < 0 or output_rate < 0:
        raise MalformedPricingTableError(
            f"pricing.json is malformed for {where}: `long_prompt` rates must be non-negative "
            f"(got input={input_rate}, output={output_rate})"
        )

    return {
        "threshold_prompt_tokens": threshold,
        "input_per_million_usd": input_rate,
        "output_per_million_usd": output_rate,
    }


def resolve_price_tier(pricing_table: dict[str, Any], model_id: str, *, on_date: date) -> PriceTier:
    """Resolve the price tier that applies to `model_id` on `on_date`. Raises
    `UnknownModelPriceError` if `model_id` has no entry at all, or
    `PricingDriftError` if it has entries but none covers `on_date` (a gap the
    table's maintainer needs to fill in with a new tier).

    Reads both of `pricing.json`'s optional per-model/per-tier extensions
    (2026-10-11): the model entry's own `cache_multipliers` override (merged over
    the global defaults) and the tier's own `long_prompt` second rate card.

    Both extensions are VALIDATED, not merely read (review-fix, reviewer M1): a
    missing cache multiplier or a half-declared `long_prompt` card raises
    `MalformedPricingTableError` rather than silently pricing a cache bucket as
    free or a rate-carded model on the wrong card. See
    `_resolve_cache_multipliers()` / `_resolve_long_prompt_card()`."""
    family = _resolve_model_family(pricing_table, model_id)
    entry = pricing_table["models"][family]
    tiers = entry.get("tiers", [])
    matched = [t for t in tiers if _date_in_window(on_date, t.get("effective_from"), t.get("effective_until"))]
    if not matched:
        windows = ", ".join(f"{t.get('label')}: {t.get('effective_from')}..{t.get('effective_until')}" for t in tiers)
        raise PricingDriftError(
            f"pricing drift: model family {family!r} (model id {model_id!r}) has no price tier "
            f"covering {on_date.isoformat()} -- known tiers: [{windows}]. Add a new tier to "
            "pricing.json before costing a run priced on this date."
        )
    # Prefer the tier with the latest effective_from if more than one somehow
    # matches (well-formed data should never have overlapping windows, but this
    # keeps resolution deterministic rather than order-dependent if it ever does).
    tier = max(matched, key=lambda t: t.get("effective_from") or "")
    cache_multipliers = _resolve_cache_multipliers(pricing_table, entry, model_family=family)
    long_prompt = _resolve_long_prompt_card(tier, model_family=family)
    return PriceTier(
        model_family=family,
        label=tier.get("label", ""),
        input_per_million_usd=float(tier["input_per_million_usd"]),
        output_per_million_usd=float(tier["output_per_million_usd"]),
        cache_write_5m_multiplier=cache_multipliers["write_5m"],
        cache_write_1h_multiplier=cache_multipliers["write_1h"],
        cache_read_multiplier=cache_multipliers["read"],
        effective_from=tier.get("effective_from"),
        effective_until=tier.get("effective_until"),
        source_url=pricing_table.get("source_url", ""),
        captured_on=tier.get("captured_on") or pricing_table.get("captured_on", ""),
        long_prompt_threshold_tokens=(long_prompt["threshold_prompt_tokens"] if long_prompt else None),
        long_prompt_input_per_million_usd=(long_prompt["input_per_million_usd"] if long_prompt else None),
        long_prompt_output_per_million_usd=(long_prompt["output_per_million_usd"] if long_prompt else None),
    )


def price_requests(requests: list[RequestUsage], *, price_tier: PriceTier) -> tuple[float, dict[str, int]]:
    """Price a thread PER REQUEST under `price_tier`, returning
    `(full_precision_cost_usd, {card: request_count})`.

    Each request is billed on the card its OWN prompt length selects (the pricing
    page: "Each request is priced on its own"), so a thread that mixes short and
    long prompts costs exactly what Anthropic billed rather than whatever one card
    would have produced. The returned per-card counts are carried through to
    `ThreadCost.request_card_counts` so a reader of `cost.json` can see the split
    that produced the number instead of taking it on faith.

    Works for a tier WITHOUT a long-prompt card too (every request lands on
    `CARD_STANDARD`), which is what makes it safe for a test to compare the two
    pricing paths; `mine_session_cost()` still uses aggregate pricing there,
    because that needs no events at all."""
    counts = {CARD_STANDARD: 0, CARD_LONG_PROMPT: 0}
    total = 0.0
    for request in requests:
        counts[price_tier.card_for(request)] += 1
        total += price_tier.cost_usd_for_request(request)
    return total, counts


def price_usage(usage: dict[str, int], *, model: str, pricing_table: dict[str, Any], on_date: date) -> float:
    """Price a plain, flat usage dict (the SAME shape `ThreadUsage.to_dict()`/
    `from_dict()` and `eval_core.judges.base.JudgeResult.usage` use) against
    `model`'s `pricing.json` tier for `on_date`. Full precision (unrounded) --
    callers round for display, matching `mine_session_cost()`'s own
    sum-before-rounding discipline.

    Review-fix (ADR-0016 reviewer Medium, "judge cost accounting"): this is the
    ONE place judge-call cost is priced, reusing the EXACT same price table and
    arithmetic as pipeline cost -- `run.py` calls this once per judge call with
    `model=eval_core.judges.base.JUDGE_MODEL` and writes the result to a SEPARATE
    `judge-cost.json`, never folded into `SessionCostBreakdown`/`cost.json` (the
    whole point is that it isn't confused with pipeline cost, per PRD §7).

    Raises `UnknownModelPriceError`/`PricingDriftError` (via `resolve_price_tier`)
    if `model` has no pricing.json entry -- FAILS LOUD rather than silently
    pricing an unrecognized model as $0, per the task's explicit requirement.

    Any per-model cache-multiplier override applies automatically (2026-10-11:
    this is how an Opus 5.5 judge call gets its 0.05x cache-read rate -- it is
    resolved in `resolve_price_tier()`, not restated here). A model priced on
    per-request rate cards raises `PerRequestPricingError`: a flat usage dict
    carries no per-request breakdown, so no honest card choice exists (this would
    only ever fire if a JUDGE were pointed at Claude Haiku 5.5, which the
    "judges must be stronger than what they judge" principle rules out anyway)."""
    price_tier = resolve_price_tier(pricing_table, model, on_date=on_date)
    return price_tier.cost_usd(ThreadUsage.from_dict(usage))


def price_web_searches(search_count: int, *, pricing_table: dict[str, Any]) -> float:
    """Price `search_count` server-side web-search tool invocations against
    `pricing.json`'s flat `web_search.cost_per_1000_searches_usd` rate.

    Anthropic bills the web-search tool per-CALL, not per-token and not
    model-dependent ("$10 per 1,000 searches", confirmed live 2026-07-07 against
    the web-search-tool docs page) -- a completely separate cost axis from
    `price_usage()`'s token-based pricing above, which is why this is its own
    function rather than folded into `PriceTier`/`resolve_price_tier` (those are
    keyed by MODEL; this is keyed by nothing but a flat rate). The web-FETCH tool
    is deliberately NOT priced here -- it is billed at ordinary token cost only,
    no separate per-call fee (confirmed live the same day against the
    web-fetch-tool docs page: "available... at no additional cost... you only pay
    standard token costs") -- fetched-page content already flows through
    `price_usage()`'s normal input-token accounting.

    Fails loud (raises `PricingError`) if `pricing.json` has no top-level
    `web_search` entry at all, rather than silently pricing search usage as
    $0 -- mirrors `resolve_price_tier()`'s "never silently price an unknown/
    unconfigured thing as free" discipline."""
    if search_count <= 0:
        return 0.0
    web_search_pricing = pricing_table.get("web_search")
    if web_search_pricing is None:
        raise PricingError(
            "pricing.json has no top-level 'web_search' entry -- cannot price web-search "
            "tool usage (add a 'web_search': {'cost_per_1000_searches_usd': ...} block)"
        )
    rate_per_search = float(web_search_pricing["cost_per_1000_searches_usd"]) / 1000
    return search_count * rate_per_search


def check_pricing_drift(pricing_table: dict[str, Any], *, on_date: date) -> list[str]:
    """`--check-pricing-drift`'s core logic: for every model family declared in
    `pricing.json`, confirm SOME tier covers `on_date`. Returns a list of issue
    strings (empty = no drift). Never raises -- the CLI decides what to do with a
    non-empty list (print + exit(1))."""
    issues: list[str] = []
    for family in pricing_table.get("models", {}):
        # Any alias resolves to the same family; use the family name itself, which
        # is always its own first-class lookup key even if not literally listed in
        # its own aliases (defensive -- every model entry in this repo's pricing.json
        # DOES list itself as its own alias, but this does not assume that).
        try:
            resolve_price_tier(pricing_table, family, on_date=on_date)
        except UnknownModelPriceError:
            aliases = pricing_table["models"][family].get("aliases", [])
            if not aliases:
                issues.append(f"model family {family!r} has no aliases at all -- no model id could ever resolve to it")
                continue
            try:
                resolve_price_tier(pricing_table, aliases[0], on_date=on_date)
            except PricingDriftError as e:
                issues.append(str(e))
        except PricingDriftError as e:
            issues.append(str(e))
    return issues


# --- Role/model resolution from a candidate declaration -----------------------------


def _derive_role(name: str, description: str, *, fallback: str) -> str:
    """Derive a human-readable phase/role label by scanning `name`/`description`
    for the daily-ai-brief skill's own phase vocabulary (see `_PHASE_LABELS`).
    Falls back to `fallback` (typically a positional label like "sub-agent-2" or
    the raw agent name) when nothing matches -- this module never invents a role
    it can't justify from the agent's own declared name/description."""
    combined = f"{name} {description}".lower()
    for aliases, canonical in _PHASE_LABELS:
        if any(alias in combined for alias in aliases):
            return canonical
    return fallback


def _declaration_agent_id_map(declaration: CandidateDeclaration) -> dict[str, tuple[str, str]]:
    """Build `{agent_id: (model, role)}` from a loaded candidate declaration -- the
    PRIMARY model/role resolution source (see module docstring)."""
    mapping: dict[str, tuple[str, str]] = {}
    top_role = "coordinator" if declaration.is_multi_agent else "primary"
    if declaration.agent.agent_id:
        mapping[declaration.agent.agent_id] = (declaration.agent.model, top_role)
    for index, sub_agent in enumerate(declaration.sub_agents, start=1):
        if not sub_agent.agent_id:
            continue
        role = _derive_role(sub_agent.name, sub_agent.description, fallback=f"sub-agent-{index}")
        mapping[sub_agent.agent_id] = (sub_agent.model, role)
    return mapping


def _resolve_thread_model_and_role(
    thread: dict[str, Any],
    *,
    declaration_map: dict[str, tuple[str, str]] | None,
    index: int,
    thread_count: int,
) -> tuple[str, str, str | None]:
    """Return `(model_id, role, agent_id)` for one raw thread record (an entry of
    `GET /v1/sessions/{id}/threads`'s `data` list)."""
    agent = thread.get("agent") or {}
    agent_id = agent.get("id") or thread.get("agent_id")

    if declaration_map and agent_id and agent_id in declaration_map:
        model_id, role = declaration_map[agent_id]
        return model_id, role, agent_id

    # FALLBACK: the thread's own embedded agent object IS the "GET /v1/agents/{id}
    # shape" (see module docstring) -- no second API call needed.
    model_id = (agent.get("model") or {}).get("id", "")
    is_root = thread.get("parent_thread_id") is None
    if is_root:
        # The coordinator's own name/description routinely NARRATES every phase it
        # orchestrates (e.g. "...(research -> selection -> writing ->
        # listening-script), delegating each phase to its sub-agent...") -- running
        # the phase-keyword scan on ITS text would false-match on whichever phase
        # word happens to appear first, mislabeling the coordinator as one of its
        # own sub-agents' roles. The root thread's role is therefore assigned
        # directly, never keyword-derived.
        role = "coordinator" if thread_count > 1 else "primary"
    else:
        name = agent.get("name", "")
        description = agent.get("description", "")
        role = _derive_role(name, description, fallback=f"sub-agent-{index}")
    return model_id, role, agent_id


# --- Per-request pricing gate (rate-carded models) ------------------------------------


def _price_thread_per_request(
    price_tier: PriceTier,
    usage: ThreadUsage,
    *,
    events: "list[dict[str, Any]] | dict[str, Any] | None",
    model_id: str,
    thread_id: str,
    thread_count: int,
) -> tuple[float, dict[str, int]]:
    """Price ONE rate-carded thread per request, refusing (loudly) rather than
    guessing whenever the available data can't support it -- the three
    preconditions from the module docstring, in the order a caller hits them."""
    if events is None:
        raise PerRequestPricingError(
            f"model {model_id!r} is priced on per-request rate cards, but no session events were "
            "supplied -- pass `events=` to mine_session_cost() (the event list run.py fetches for "
            "artifact retrieval, or the `{'events': [...]}` wrapper read back out of "
            "events.json; both are accepted). Refusing to price it off aggregate thread usage, "
            "which cannot say which card each request was billed on."
        )

    if thread_count > 1:
        raise PerRequestPricingError(
            f"model {model_id!r} is priced on per-request rate cards, but this session has "
            f"{thread_count} threads and a span.model_request_end event carries NO thread "
            "identifier (its only fields are id/is_error/model_request_start_id/model_usage/"
            "processed_at) -- a request cannot be attributed to a thread, so per-request pricing "
            "is supported for single-thread sessions only. Price a multi-agent rate-carded run by "
            "hand, or run that agent as a single-agent candidate."
        )

    requests = extract_request_usages(events)
    if not requests:
        raise PerRequestPricingError(
            f"model {model_id!r} is priced on per-request rate cards, but the supplied events "
            "contain no span.model_request_end event with usage at all -- nothing to price. The "
            "event list is empty or truncated."
        )

    verify_request_usages_match_thread(requests, usage, model_id=model_id, thread_id=thread_id)
    return price_requests(requests, price_tier=price_tier)


# --- Thread / session cost breakdown -------------------------------------------------


@dataclass(frozen=True)
class ThreadCost:
    """One thread's role, resolved model, usage, and cost -- the per-repetition
    `threads-usage.json`/`cost.json` unit (ADR-0016 D4).

    `request_card_counts` (2026-10-11) is the transparency breakdown for a thread
    priced PER REQUEST off two rate cards: `{"standard": n, "long_prompt": m}`, so
    a reader of `cost.json` can see which cards produced the number. It stays None
    -- and is OMITTED from `to_dict()` entirely -- for every aggregate-priced
    thread, which keeps `cost.json`/`threads-usage.json` byte-identical to what
    every record committed under `runs/` already carries (those records predate
    this field; `from_dict()` tolerates its absence)."""

    thread_id: str
    role: str
    agent_id: str | None
    model: str
    usage: ThreadUsage
    cost_usd: float
    request_card_counts: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "thread_id": self.thread_id,
            "role": self.role,
            "agent_id": self.agent_id,
            "model": self.model,
            "usage": self.usage.to_dict(),
            "cost_usd": self.cost_usd,
        }
        if self.request_card_counts is not None:
            data["request_card_counts"] = dict(self.request_card_counts)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThreadCost":
        return cls(
            thread_id=data["thread_id"],
            role=data["role"],
            agent_id=data.get("agent_id"),
            model=data["model"],
            usage=ThreadUsage.from_dict(data["usage"]),
            cost_usd=float(data["cost_usd"]),
            request_card_counts=data.get("request_card_counts"),
        )


@dataclass(frozen=True)
class SessionCostBreakdown:
    """The full structured cost breakdown for one candidate run's session."""

    session_id: str
    total_cost_usd: float
    total_usage: ThreadUsage
    threads: tuple[ThreadCost, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "total_cost_usd": self.total_cost_usd,
            "total_usage": self.total_usage.to_dict(),
            "threads": [t.to_dict() for t in self.threads],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionCostBreakdown":
        return cls(
            session_id=data["session_id"],
            total_cost_usd=float(data["total_cost_usd"]),
            total_usage=ThreadUsage.from_dict(data["total_usage"]),
            threads=tuple(ThreadCost.from_dict(t) for t in data.get("threads", [])),
        )


def mine_session_cost(
    session_id: str,
    threads: list[dict[str, Any]],
    *,
    pricing_table: dict[str, Any],
    on_date: date,
    candidate_declaration: CandidateDeclaration | None = None,
    events: "list[dict[str, Any]] | dict[str, Any] | None" = None,
) -> SessionCostBreakdown:
    """Mine the full per-thread cost breakdown for one session.

    `threads` is the raw `data` list from `GET /v1/sessions/{id}/threads` (each a
    dict with `id`, `parent_thread_id`, `agent`, `usage`). `on_date` is the
    calendar date to price against (the caller decides -- the live CLI uses
    today's real date; tests pin a fixed date so results don't drift with the
    calendar, see `resolve_price_tier`'s docstring).

    `events` is the session's own event list -- either the bare list `run.py`
    already fetched for artifact retrieval, or the `{"events": [...]}` wrapper
    `run_store.write_events()` stores in `events.json` (both accepted; see
    `normalize_events()`). It is used ONLY
    for a model whose tier carries a `long_prompt` rate card -- Claude Haiku 5.5 --
    which cannot be priced from aggregate usage and is instead priced per request
    from `span.model_request_end.model_usage`, with a fail-loud integrity check
    against the thread's own totals (see the module docstring). Every other family
    ignores it entirely, so omitting it stays valid for them.

    Threads are sorted by `created_at` (defensive normalization -- the API already
    returns them chronologically, but this does not assume that) so the root
    (coordinator/sole-agent) thread and each sub-agent thread's positional index
    are resolved deterministically for the fallback role-labeling path.

    Total cost is computed by summing each thread's FULL-PRECISION (unrounded)
    cost and rounding ONLY the sum -- summing already-rounded per-thread costs can
    be off by a cent's fraction from the true total (confirmed by the golden test
    against a real captured run, see tests/test_cost_golden.py). Each `ThreadCost`
    still carries its OWN independently-rounded `cost_usd` for display.
    """
    declaration_map = _declaration_agent_id_map(candidate_declaration) if candidate_declaration else None

    ordered_threads = sorted(threads, key=lambda t: t.get("created_at") or "")
    thread_count = len(ordered_threads)

    raw_thread_costs: list[float] = []
    thread_costs: list[ThreadCost] = []
    total_usage = ThreadUsage()

    sub_agent_index = 0
    for thread in ordered_threads:
        is_root = thread.get("parent_thread_id") is None
        if not is_root:
            sub_agent_index += 1
        model_id, role, agent_id = _resolve_thread_model_and_role(
            thread, declaration_map=declaration_map, index=sub_agent_index, thread_count=thread_count
        )
        usage = ThreadUsage.from_api_usage(thread.get("usage"))
        thread_id = thread.get("id", "")
        price_tier = resolve_price_tier(pricing_table, model_id, on_date=on_date)

        request_card_counts: dict[str, int] | None = None
        if price_tier.has_long_prompt_card:
            raw_cost, request_card_counts = _price_thread_per_request(
                price_tier,
                usage,
                events=events,
                model_id=model_id,
                thread_id=thread_id,
                thread_count=thread_count,
            )
        else:
            raw_cost = price_tier.cost_usd(usage)

        raw_thread_costs.append(raw_cost)
        total_usage = total_usage + usage
        thread_costs.append(
            ThreadCost(
                thread_id=thread_id,
                role=role,
                agent_id=agent_id,
                model=model_id,
                usage=usage,
                cost_usd=round(raw_cost, 4),
                request_card_counts=request_card_counts,
            )
        )

    total_cost_usd = round(sum(raw_thread_costs), 4)

    return SessionCostBreakdown(
        session_id=session_id,
        total_cost_usd=total_cost_usd,
        total_usage=total_usage,
        threads=tuple(thread_costs),
    )


# --- Live HTTP entry point ---------------------------------------------------------


def fetch_threads(client: Any, session_id: str) -> list[dict[str, Any]]:
    """GET /v1/sessions/{id}/threads -- the live entry point. `client` is an
    `httpx.Client` already configured with the Agents/Deployments/Sessions beta
    header (e.g. `candidate_sync.trigger.build_deployments_client()`), injected so
    tests never need a real API key or network access."""
    response = client.get(f"/v1/sessions/{session_id}/threads")
    response.raise_for_status()
    return response.json().get("data", [])


__all__ = [
    "PricingError",
    "UnknownModelPriceError",
    "PricingDriftError",
    "MalformedPricingTableError",
    "PerRequestPricingError",
    "CARD_STANDARD",
    "CARD_LONG_PROMPT",
    "REQUIRED_CACHE_MULTIPLIER_KEYS",
    "REQUIRED_LONG_PROMPT_KEYS",
    "ThreadUsage",
    "RequestUsage",
    "PriceTier",
    "ThreadCost",
    "SessionCostBreakdown",
    "load_pricing_table",
    "resolve_price_tier",
    "normalize_events",
    "extract_request_usages",
    "verify_request_usages_match_thread",
    "price_requests",
    "price_usage",
    "price_web_searches",
    "check_pricing_drift",
    "mine_session_cost",
    "fetch_threads",
]
