"""Unit tests for harness/cost.py (ADR-0016 D2) -- pricing resolution, the drift
check, and BOTH model/role resolution paths (declaration-primary, thread-fallback).
The golden reproduction of a real captured run lives separately in
`test_cost_golden.py`; these tests use small, synthetic, hand-built fixtures.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from candidate_sync.loader import AgentDeclaration, CandidateDeclaration

from harness import cost


def _agent(name="", description="", model="claude-sonnet-5", agent_id=None) -> AgentDeclaration:
    return AgentDeclaration(
        name=name,
        description=description,
        model=model,
        system_prompt="",
        task_prompt="",
        tools=[],
        mcp_servers=[],
        skills=[],
        parameters={},
        agent_id=agent_id,
    )


def _thread(
    thread_id: str,
    *,
    agent_id: str,
    model_id: str,
    name: str = "",
    description: str = "",
    parent_thread_id: str | None = None,
    created_at: str = "2026-07-07T00:00:00Z",
    input_tokens=0,
    output_tokens=0,
    cache_read=0,
    cache_5m=0,
    cache_1h=0,
) -> dict:
    return {
        "id": thread_id,
        "parent_thread_id": parent_thread_id,
        "created_at": created_at,
        "agent": {"id": agent_id, "name": name, "description": description, "model": {"id": model_id}},
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation": {"ephemeral_5m_input_tokens": cache_5m, "ephemeral_1h_input_tokens": cache_1h},
        },
    }


# --- ThreadUsage ---------------------------------------------------------------------


def test_thread_usage_from_api_usage_parses_the_nested_cache_creation_shape():
    usage = cost.ThreadUsage.from_api_usage(
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 50},
        }
    )
    assert usage == cost.ThreadUsage(
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=30,
        cache_creation_5m_input_tokens=40,
        cache_creation_1h_input_tokens=50,
    )


def test_thread_usage_from_api_usage_defaults_missing_fields_to_zero():
    assert cost.ThreadUsage.from_api_usage({}) == cost.ThreadUsage()
    assert cost.ThreadUsage.from_api_usage(None) == cost.ThreadUsage()


def test_thread_usage_addition_sums_all_five_fields():
    a = cost.ThreadUsage(1, 2, 3, 4, 5)
    b = cost.ThreadUsage(10, 20, 30, 40, 50)
    assert a + b == cost.ThreadUsage(11, 22, 33, 44, 55)


def test_thread_usage_round_trips_through_to_dict_from_dict():
    usage = cost.ThreadUsage(1, 2, 3, 4, 5)
    assert cost.ThreadUsage.from_dict(usage.to_dict()) == usage


# --- Pricing table resolution ---------------------------------------------------------


def _pricing_table():
    return cost.load_pricing_table()


def test_resolve_price_tier_picks_the_introductory_sonnet_tier_before_cutoff():
    tier = cost.resolve_price_tier(_pricing_table(), "claude-sonnet-5", on_date=date(2026, 8, 1))
    assert tier.label == "introductory"
    assert tier.input_per_million_usd == 2.00
    assert tier.output_per_million_usd == 10.00


def test_resolve_price_tier_picks_the_standard_sonnet_tier_after_cutoff():
    tier = cost.resolve_price_tier(_pricing_table(), "claude-sonnet-5", on_date=date(2026, 9, 15))
    assert tier.label == "standard"
    assert tier.input_per_million_usd == 3.00
    assert tier.output_per_million_usd == 15.00


def test_resolve_price_tier_finds_haiku_via_a_dated_alias():
    """haiku-swap's declared model id is 'claude-haiku-4-5-20251001' -- must resolve
    to the same 'claude-haiku-4-5' family/pricing as the bare alias."""
    tier = cost.resolve_price_tier(_pricing_table(), "claude-haiku-4-5-20251001", on_date=date(2026, 7, 7))
    assert tier.model_family == "claude-haiku-4-5"
    assert tier.input_per_million_usd == 1.00
    assert tier.output_per_million_usd == 5.00


def test_resolve_price_tier_raises_on_an_unknown_model():
    with pytest.raises(cost.UnknownModelPriceError):
        cost.resolve_price_tier(_pricing_table(), "claude-opus-9-nonexistent", on_date=date(2026, 7, 7))


def test_resolve_price_tier_raises_pricing_drift_when_no_tier_covers_the_date():
    """A model whose only tier has an effective_until in the past, with no
    successor tier defined -- the ADR's concrete 'silently mis-priced' scenario."""
    table = {
        "source_url": "https://example.test",
        "cache_multipliers": {"write_5m": 1.25, "write_1h": 2.0, "read": 0.1},
        "models": {
            "claude-fake-1": {
                "aliases": ["claude-fake-1"],
                "tiers": [
                    {
                        "label": "only-tier",
                        "effective_from": None,
                        "effective_until": "2026-01-01",
                        "input_per_million_usd": 1.0,
                        "output_per_million_usd": 5.0,
                    }
                ],
            }
        },
    }
    with pytest.raises(cost.PricingDriftError):
        cost.resolve_price_tier(table, "claude-fake-1", on_date=date(2026, 7, 7))


def test_price_tier_cost_usd_matches_the_documented_multiplier_arithmetic():
    tier = cost.PriceTier(
        model_family="test",
        label="test",
        input_per_million_usd=2.0,
        output_per_million_usd=10.0,
        cache_write_5m_multiplier=1.25,
        cache_write_1h_multiplier=2.0,
        cache_read_multiplier=0.1,
        effective_from=None,
        effective_until=None,
        source_url="",
        captured_on="",
    )
    usage = cost.ThreadUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_5m_input_tokens=1_000_000,
        cache_creation_1h_input_tokens=1_000_000,
    )
    # input $2 + output $10 + 5m-write (1.25 * $2 = $2.50) + 1h-write (2.0 * $2 =
    # $4.00) + read (0.1 * $2 = $0.20) = $18.70
    assert usage and tier.cost_usd(usage) == pytest.approx(2.0 + 10.0 + 2.5 + 4.0 + 0.20)


# --- price_usage (review-fix: judge cost accounting, ADR-0016 D2 cross-cutting) --------


def test_price_usage_prices_a_flat_usage_dict_against_the_judge_models_rate():
    """The judge model (claude-haiku-4-5, no date suffix) resolves via
    pricing.json's existing 'claude-haiku-4-5' family -- no pricing.json change
    needed for judge pricing to work."""
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
    }
    cost_usd = cost.price_usage(usage, model="claude-haiku-4-5", pricing_table=_pricing_table(), on_date=date(2026, 7, 7))
    # Haiku: $1/M input, $5/M output -> 1000*1e-6 + 500*5e-6 = 0.001 + 0.0025 = 0.0035
    assert cost_usd == pytest.approx(0.0035)


def test_price_usage_fails_loud_for_an_unrecognized_judge_model():
    usage = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_5m_input_tokens": 0, "cache_creation_1h_input_tokens": 0}
    with pytest.raises(cost.UnknownModelPriceError):
        cost.price_usage(usage, model="claude-some-future-judge-model", pricing_table=_pricing_table(), on_date=date(2026, 7, 7))


# --- claude-opus-4-8 pricing (judge methodology v2, 2026-07-07) -----------------------


def test_resolve_price_tier_finds_opus_4_8_at_standard_non_introductory_pricing():
    tier = cost.resolve_price_tier(_pricing_table(), "claude-opus-4-8", on_date=date(2026, 7, 7))
    assert tier.label == "standard"
    assert tier.input_per_million_usd == 5.00
    assert tier.output_per_million_usd == 25.00
    assert tier.effective_until is None  # not an introductory/expiring tier


def test_price_usage_prices_opus_4_8_correctly():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
    }
    cost_usd = cost.price_usage(usage, model="claude-opus-4-8", pricing_table=_pricing_table(), on_date=date(2026, 7, 7))
    # Opus 4.8: $5/M input, $25/M output -> 1000*5e-6 + 500*25e-6 = 0.005 + 0.0125 = 0.0175
    assert cost_usd == pytest.approx(0.0175)


def test_opus_4_8_cache_multipliers_reproduce_the_uniform_ratios():
    """The pricing page's own $6.25/$10/$0.50 cache rates for Opus 4.8 are exactly
    1.25x/2.0x/0.1x its $5 base input rate -- the SAME uniform cache_multipliers
    this table already applies to every other model family, confirming no
    per-model override was needed."""
    tier = cost.resolve_price_tier(_pricing_table(), "claude-opus-4-8", on_date=date(2026, 7, 7))
    assert tier.cache_write_5m_multiplier == pytest.approx(1.25)
    assert tier.cache_write_1h_multiplier == pytest.approx(2.0)
    assert tier.cache_read_multiplier == pytest.approx(0.1)


def test_check_pricing_drift_covers_opus_4_8_with_no_issues():
    issues = cost.check_pricing_drift(_pricing_table(), on_date=date(2026, 7, 7))
    assert issues == []
    assert "claude-opus-4-8" not in " ".join(issues)


# --- price_web_searches (judge methodology v2: web-search tool cost) ------------------


def test_price_web_searches_prices_at_ten_dollars_per_thousand_searches():
    cost_usd = cost.price_web_searches(6, pricing_table=_pricing_table())
    assert cost_usd == pytest.approx(0.06)


def test_price_web_searches_of_zero_costs_nothing():
    assert cost.price_web_searches(0, pricing_table=_pricing_table()) == 0.0


def test_price_web_searches_of_a_thousand_costs_exactly_ten_dollars():
    assert cost.price_web_searches(1000, pricing_table=_pricing_table()) == pytest.approx(10.0)


def test_price_web_searches_fails_loud_when_pricing_json_has_no_web_search_entry():
    table = {"models": {}}  # deliberately missing the top-level "web_search" key
    with pytest.raises(cost.PricingError):
        cost.price_web_searches(5, pricing_table=table)


# --- --check-pricing-drift ------------------------------------------------------------


def test_check_pricing_drift_reports_no_issues_for_a_fully_covered_table():
    issues = cost.check_pricing_drift(_pricing_table(), on_date=date(2026, 7, 7))
    assert issues == []


def test_check_pricing_drift_reports_no_issues_after_the_intro_cutoff_because_a_successor_tier_exists():
    issues = cost.check_pricing_drift(_pricing_table(), on_date=date(2026, 9, 15))
    assert issues == []


def test_check_pricing_drift_flags_a_model_family_with_a_coverage_gap():
    table = {
        "source_url": "https://example.test",
        "cache_multipliers": {"write_5m": 1.25, "write_1h": 2.0, "read": 0.1},
        "models": {
            "claude-fake-1": {
                "aliases": ["claude-fake-1"],
                "tiers": [
                    {
                        "label": "only-tier",
                        "effective_from": None,
                        "effective_until": "2026-01-01",
                        "input_per_million_usd": 1.0,
                        "output_per_million_usd": 5.0,
                    }
                ],
            }
        },
    }
    issues = cost.check_pricing_drift(table, on_date=date(2026, 7, 7))
    assert len(issues) == 1
    assert "claude-fake-1" in issues[0]


# --- mine_session_cost: PRIMARY (candidate-declaration) resolution path ---------------


def test_mine_session_cost_prefers_the_declared_model_over_the_threads_own_embedded_model():
    """The declaration says the coordinator is Sonnet -- even if a thread's own
    embedded agent object claimed something else, the declaration wins (it is the
    git-tracked source of truth for what the candidate's declaration SAYS should
    run, per ADR-0016 D2)."""
    declaration = CandidateDeclaration(
        slug="synthetic",
        directory=Path("."),
        candidate_json={},
        agent=_agent(name="coordinator", model="claude-sonnet-5", agent_id="agent_coord"),
        sub_agents=[_agent(name="x-research-sub-agent", model="claude-haiku-4-5-20251001", agent_id="agent_research")],
    )
    threads = [
        _thread("sthr_1", agent_id="agent_coord", model_id="claude-DIFFERENT-MODEL-ID", parent_thread_id=None, created_at="1"),
        _thread("sthr_2", agent_id="agent_research", model_id="claude-DIFFERENT-MODEL-ID", parent_thread_id="sthr_1", created_at="2"),
    ]

    breakdown = cost.mine_session_cost(
        "sesn_1", threads, pricing_table=cost.load_pricing_table(), on_date=date(2026, 7, 7), candidate_declaration=declaration
    )

    by_role = {t.role: t.model for t in breakdown.threads}
    assert by_role["coordinator"] == "claude-sonnet-5"
    assert by_role["research"] == "claude-haiku-4-5-20251001"


def test_mine_session_cost_falls_back_to_the_threads_own_agent_when_not_in_the_declaration():
    """A thread whose agent_id the declaration doesn't recognize (an orphaned/
    unexpected thread) falls back to the thread's own embedded agent object rather
    than erroring."""
    declaration = CandidateDeclaration(
        slug="synthetic",
        directory=Path("."),
        candidate_json={},
        agent=_agent(name="coordinator", model="claude-sonnet-5", agent_id="agent_coord"),
        sub_agents=[],
    )
    threads = [
        _thread("sthr_1", agent_id="agent_coord", model_id="claude-sonnet-5", parent_thread_id=None, created_at="1"),
        _thread(
            "sthr_2",
            agent_id="agent_UNRECOGNIZED",
            model_id="claude-haiku-4-5-20251001",
            name="unexpected-writing-sub-agent",
            parent_thread_id="sthr_1",
            created_at="2",
        ),
    ]

    breakdown = cost.mine_session_cost(
        "sesn_1", threads, pricing_table=cost.load_pricing_table(), on_date=date(2026, 7, 7), candidate_declaration=declaration
    )

    orphan = next(t for t in breakdown.threads if t.agent_id == "agent_UNRECOGNIZED")
    assert orphan.model == "claude-haiku-4-5-20251001"
    assert orphan.role == "writing"  # keyword-derived from its own name


def test_mine_session_cost_falls_back_to_a_pure_positional_label_when_no_keyword_matches():
    """review-fix, item 10: a sub-agent thread whose name/description contain
    NONE of the daily-ai-brief phase keywords (_PHASE_LABELS) -- unlike the
    orphan-thread test above, which still matches "writing" -- must fall back to
    the pure positional f"sub-agent-{index}" label, never an invented/guessed
    phase name."""
    threads = [
        _thread("sthr_1", agent_id="agent_coord", model_id="claude-sonnet-5", parent_thread_id=None, created_at="1"),
        _thread(
            "sthr_2",
            agent_id="agent_mystery_1",
            model_id="claude-haiku-4-5-20251001",
            name="totally-unrelated-agent-name",
            description="Does something the phase vocabulary has no word for.",
            parent_thread_id="sthr_1",
            created_at="2",
        ),
        _thread(
            "sthr_3",
            agent_id="agent_mystery_2",
            model_id="claude-haiku-4-5-20251001",
            name="another-unrelated-agent-name",
            description="Also nothing recognizable here.",
            parent_thread_id="sthr_1",
            created_at="3",
        ),
    ]

    breakdown = cost.mine_session_cost("sesn_1", threads, pricing_table=cost.load_pricing_table(), on_date=date(2026, 7, 7))

    roles_by_agent_id = {t.agent_id: t.role for t in breakdown.threads}
    assert roles_by_agent_id["agent_mystery_1"] == "sub-agent-1"
    assert roles_by_agent_id["agent_mystery_2"] == "sub-agent-2"


def test_mine_session_cost_declaration_path_also_falls_back_to_a_positional_label():
    """The SAME positional fallback (`_declaration_agent_id_map`, cost.py) applies
    when resolving via the PRIMARY (candidate-declaration) path too, not just the
    thread-fallback path covered above -- a declared sub-agent whose name/
    description match no phase keyword gets f"sub-agent-{index}", never a guess."""
    declaration = CandidateDeclaration(
        slug="synthetic",
        directory=Path("."),
        candidate_json={},
        agent=_agent(name="coordinator", model="claude-sonnet-5", agent_id="agent_coord"),
        sub_agents=[
            _agent(name="totally-unrelated-agent-name", model="claude-haiku-4-5-20251001", agent_id="agent_mystery")
        ],
    )
    threads = [
        _thread("sthr_1", agent_id="agent_coord", model_id="claude-sonnet-5", parent_thread_id=None, created_at="1"),
        _thread("sthr_2", agent_id="agent_mystery", model_id="claude-haiku-4-5-20251001", parent_thread_id="sthr_1", created_at="2"),
    ]

    breakdown = cost.mine_session_cost(
        "sesn_1", threads, pricing_table=cost.load_pricing_table(), on_date=date(2026, 7, 7), candidate_declaration=declaration
    )

    mystery = next(t for t in breakdown.threads if t.agent_id == "agent_mystery")
    assert mystery.role == "sub-agent-1"


def test_mine_session_cost_single_agent_root_thread_is_labeled_primary_not_coordinator():
    threads = [_thread("sthr_1", agent_id="agent_solo", model_id="claude-sonnet-5", parent_thread_id=None, created_at="1")]

    breakdown = cost.mine_session_cost("sesn_1", threads, pricing_table=cost.load_pricing_table(), on_date=date(2026, 7, 7))

    assert breakdown.threads[0].role == "primary"


def test_mine_session_cost_root_thread_role_is_never_keyword_derived_from_its_own_narration():
    """Regression: a coordinator's OWN description routinely narrates every phase
    it orchestrates (e.g. '...research -> selection -> writing ->
    listening-script...') -- the root thread's role must never be keyword-scanned
    off that text, or it collides with a real sub-agent's role label."""
    threads = [
        _thread(
            "sthr_1",
            agent_id="agent_coord",
            model_id="claude-sonnet-5",
            name="the-coordinator",
            description="Orchestrates research, selection, writing, and the listening-script phases.",
            parent_thread_id=None,
            created_at="1",
        ),
        _thread(
            "sthr_2",
            agent_id="agent_ls",
            model_id="claude-haiku-4-5-20251001",
            name="the-listening-script-sub-agent",
            parent_thread_id="sthr_1",
            created_at="2",
        ),
    ]

    breakdown = cost.mine_session_cost("sesn_1", threads, pricing_table=cost.load_pricing_table(), on_date=date(2026, 7, 7))

    roles = [t.role for t in breakdown.threads]
    assert roles == ["coordinator", "listening-script"]


# --- Total rounding: sum-of-raw vs sum-of-rounded ---------------------------------------


def test_total_cost_sums_raw_costs_before_rounding_not_after():
    """Three threads each costing a value that rounds the SAME way individually but
    whose raw (unrounded) sum rounds differently than the sum of the three
    already-rounded numbers -- proving mine_session_cost sums BEFORE rounding."""
    table = {
        "source_url": "https://example.test",
        "cache_multipliers": {"write_5m": 0.0, "write_1h": 0.0, "read": 0.0},
        "models": {"claude-fake-1": {"aliases": ["claude-fake-1"], "tiers": [{"label": "t", "effective_from": None, "effective_until": None, "input_per_million_usd": 1.0, "output_per_million_usd": 1.0}]}},
    }
    # Each thread's raw cost is 0.00005 (rounds to 0.0001 at 4dp via round-half-even
    # quirks aside -- chosen so three of them summed raw (0.00015) rounds to 0.0001
    # or 0.0002 depending on rounding mode, while three PRE-rounded 0.0001 values
    # sum to 0.0003 -- a real, demonstrable divergence). Use input_tokens=50 => 50 *
    # (1/1e6) = 0.00005 raw per thread.
    threads = [
        _thread(f"sthr_{i}", agent_id=f"agent_{i}", model_id="claude-fake-1", parent_thread_id=(None if i == 0 else "sthr_0"), created_at=str(i), input_tokens=50)
        for i in range(3)
    ]

    breakdown = cost.mine_session_cost("sesn_1", threads, pricing_table=table, on_date=date(2026, 7, 7))

    raw_sum = sum(50 * (1.0 / 1_000_000) for _ in range(3))
    assert breakdown.total_cost_usd == round(raw_sum, 4)


# --- fetch_threads (live HTTP entry point) ---------------------------------------------


class _FakeResponse:
    def __init__(self, json_body):
        self._json_body = json_body

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_body


class _FakeClient:
    def __init__(self, body):
        self._body = body
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append(path)
        return _FakeResponse(self._body)


def test_fetch_threads_calls_the_confirmed_endpoint_and_returns_the_data_list():
    client = _FakeClient({"data": [{"id": "sthr_1"}, {"id": "sthr_2"}]})

    threads = cost.fetch_threads(client, "sesn_abc")

    assert [t["id"] for t in threads] == ["sthr_1", "sthr_2"]
    assert client.calls == ["/v1/sessions/sesn_abc/threads"]
