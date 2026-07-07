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
    Sonnet 5's introductory tier expires 2026-08-31; querying a date in September
    without a successor tier defined would raise this)."""


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


# --- Pricing table -----------------------------------------------------------------


@dataclass(frozen=True)
class PriceTier:
    """One resolved price tier for one model family, ready to cost a `ThreadUsage`."""

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

    def cost_usd(self, usage: ThreadUsage) -> float:
        """Full-precision (unrounded) USD cost for `usage` under this tier. Callers
        that need a displayable number round explicitly -- rounding INDIVIDUAL
        thread costs before summing them into a total produces a total that can be
        off by a cent's fraction from summing the raw, unrounded costs first (see
        `mine_session_cost()`'s own docstring / the golden test for the concrete
        case this matters for)."""
        input_rate = self.input_per_million_usd / 1_000_000
        output_rate = self.output_per_million_usd / 1_000_000
        return (
            usage.input_tokens * input_rate
            + usage.output_tokens * output_rate
            + usage.cache_creation_5m_input_tokens * input_rate * self.cache_write_5m_multiplier
            + usage.cache_creation_1h_input_tokens * input_rate * self.cache_write_1h_multiplier
            + usage.cache_read_input_tokens * input_rate * self.cache_read_multiplier
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


def resolve_price_tier(pricing_table: dict[str, Any], model_id: str, *, on_date: date) -> PriceTier:
    """Resolve the price tier that applies to `model_id` on `on_date`. Raises
    `UnknownModelPriceError` if `model_id` has no entry at all, or
    `PricingDriftError` if it has entries but none covers `on_date` (a gap the
    table's maintainer needs to fill in with a new tier -- ADR-0016 D2's concrete
    "Sonnet 5 in September" example)."""
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
    cache_multipliers = pricing_table.get("cache_multipliers", {})
    return PriceTier(
        model_family=family,
        label=tier.get("label", ""),
        input_per_million_usd=float(tier["input_per_million_usd"]),
        output_per_million_usd=float(tier["output_per_million_usd"]),
        cache_write_5m_multiplier=float(cache_multipliers.get("write_5m", 0.0)),
        cache_write_1h_multiplier=float(cache_multipliers.get("write_1h", 0.0)),
        cache_read_multiplier=float(cache_multipliers.get("read", 0.0)),
        effective_from=tier.get("effective_from"),
        effective_until=tier.get("effective_until"),
        source_url=pricing_table.get("source_url", ""),
        captured_on=tier.get("captured_on") or pricing_table.get("captured_on", ""),
    )


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
    pricing an unrecognized model as $0, per the task's explicit requirement."""
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


# --- Thread / session cost breakdown -------------------------------------------------


@dataclass(frozen=True)
class ThreadCost:
    """One thread's role, resolved model, usage, and cost -- the per-repetition
    `threads-usage.json`/`cost.json` unit (ADR-0016 D4)."""

    thread_id: str
    role: str
    agent_id: str | None
    model: str
    usage: ThreadUsage
    cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "role": self.role,
            "agent_id": self.agent_id,
            "model": self.model,
            "usage": self.usage.to_dict(),
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThreadCost":
        return cls(
            thread_id=data["thread_id"],
            role=data["role"],
            agent_id=data.get("agent_id"),
            model=data["model"],
            usage=ThreadUsage.from_dict(data["usage"]),
            cost_usd=float(data["cost_usd"]),
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
) -> SessionCostBreakdown:
    """Mine the full per-thread cost breakdown for one session.

    `threads` is the raw `data` list from `GET /v1/sessions/{id}/threads` (each a
    dict with `id`, `parent_thread_id`, `agent`, `usage`). `on_date` is the
    calendar date to price against (the caller decides -- the live CLI uses
    today's real date; tests pin a fixed date so results don't drift with the
    calendar, see `resolve_price_tier`'s docstring).

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
        price_tier = resolve_price_tier(pricing_table, model_id, on_date=on_date)
        raw_cost = price_tier.cost_usd(usage)

        raw_thread_costs.append(raw_cost)
        total_usage = total_usage + usage
        thread_costs.append(
            ThreadCost(
                thread_id=thread.get("id", ""),
                role=role,
                agent_id=agent_id,
                model=model_id,
                usage=usage,
                cost_usd=round(raw_cost, 4),
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
    "ThreadUsage",
    "PriceTier",
    "ThreadCost",
    "SessionCostBreakdown",
    "load_pricing_table",
    "resolve_price_tier",
    "price_usage",
    "price_web_searches",
    "check_pricing_drift",
    "mine_session_cost",
    "fetch_threads",
]
