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


def test_sonnet_5_is_priced_at_two_and_ten_on_every_date():
    """CORRECTION (2026-10-11): this table used to carry a 'standard from
    2026-09-01' Sonnet 5 tier at $3/$15, transcribed from a then-scheduled
    increase. The pricing page's own footnote now states that increase WILL NOT
    OCCUR and $2/$10 is the standard price -- so one tier, every date, and in
    particular the same price before AND after the phantom cutover (which is what
    keeps every committed pre-correction run record re-pricing identically)."""
    table = _pricing_table()
    for on_date in (date(2026, 7, 7), date(2026, 8, 1), date(2026, 9, 15), date(2026, 10, 11)):
        tier = cost.resolve_price_tier(table, "claude-sonnet-5", on_date=on_date)
        assert tier.label == "standard"
        assert tier.effective_until is None
        assert tier.input_per_million_usd == 2.00
        assert tier.output_per_million_usd == 10.00
        # Cache read stays the GLOBAL 0.1x default ($0.20/MTok) -- the 0.05x
        # override belongs to Sonnet 5.5/Opus 5.5 only, never to Sonnet 5.
        assert tier.cache_read_multiplier == pytest.approx(0.1)


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


def test_opus_4_8_inherits_the_global_default_cache_multipliers():
    """The pricing page's own $6.25/$10/$0.50 cache rates for Opus 4.8 are exactly
    1.25x/2.0x/0.1x its $5 base input rate -- i.e. exactly `pricing.json`'s GLOBAL
    defaults, so Opus 4.8 declares no per-model override and must keep resolving
    to them (the ratios are NO LONGER uniform across the table -- Opus 5.5 and
    Sonnet 5.5 override the read ratio -- which is exactly why this stays
    asserted: a legacy family must not pick up a newer family's override)."""
    tier = cost.resolve_price_tier(_pricing_table(), "claude-opus-4-8", on_date=date(2026, 7, 7))
    assert tier.cache_write_5m_multiplier == pytest.approx(1.25)
    assert tier.cache_write_1h_multiplier == pytest.approx(2.0)
    assert tier.cache_read_multiplier == pytest.approx(0.1)
    assert tier.has_long_prompt_card is False


def test_check_pricing_drift_covers_opus_4_8_with_no_issues():
    issues = cost.check_pricing_drift(_pricing_table(), on_date=date(2026, 7, 7))
    assert issues == []
    assert "claude-opus-4-8" not in " ".join(issues)


# --- The 5.5 families: per-model cache overrides (2026-10-11) -------------------------


def test_opus_5_5_overrides_only_the_cache_read_multiplier():
    """Pricing page (2026-10-11): Opus 5.5 is $4/$20 with 5m write $5, 1h write
    $8, cache read $0.20 -- so the write ratios are the GLOBAL 1.25x/2x of its own
    $4 base input rate, and ONLY the read ratio is overridden to 0.05x (the page's
    footnote: 'Cache hits and refreshes on Claude Opus 5.5 and Claude Sonnet 5.5
    are priced at 0.05x the base input price')."""
    tier = cost.resolve_price_tier(_pricing_table(), "claude-opus-5-5", on_date=date(2026, 10, 11))
    assert tier.input_per_million_usd == 4.00
    assert tier.output_per_million_usd == 20.00
    assert tier.cache_read_multiplier == pytest.approx(0.05)
    # Inherited, NOT overridden -- the merge is per key, not wholesale replacement.
    assert tier.cache_write_5m_multiplier == pytest.approx(1.25)
    assert tier.cache_write_1h_multiplier == pytest.approx(2.0)
    # The resolved per-MTok cache rates the page itself prints.
    assert tier.input_per_million_usd * tier.cache_write_5m_multiplier == pytest.approx(5.00)
    assert tier.input_per_million_usd * tier.cache_write_1h_multiplier == pytest.approx(8.00)
    assert tier.input_per_million_usd * tier.cache_read_multiplier == pytest.approx(0.20)


def test_sonnet_5_5_overrides_only_the_cache_read_multiplier():
    """Pricing page (2026-10-11): Sonnet 5.5 is $2/$10 with 5m write $2.50, 1h
    write $4, cache read $0.10 -- the same 0.05x read override as Opus 5.5."""
    tier = cost.resolve_price_tier(_pricing_table(), "claude-sonnet-5-5", on_date=date(2026, 10, 11))
    assert tier.input_per_million_usd == 2.00
    assert tier.output_per_million_usd == 10.00
    assert tier.cache_read_multiplier == pytest.approx(0.05)
    assert tier.cache_write_5m_multiplier == pytest.approx(1.25)
    assert tier.cache_write_1h_multiplier == pytest.approx(2.0)
    assert tier.input_per_million_usd * tier.cache_write_5m_multiplier == pytest.approx(2.50)
    assert tier.input_per_million_usd * tier.cache_write_1h_multiplier == pytest.approx(4.00)
    assert tier.input_per_million_usd * tier.cache_read_multiplier == pytest.approx(0.10)


def test_opus_5_5_prices_a_million_of_every_bucket_to_the_hand_computed_total():
    """Hand-computed against the page's per-MTok rates: 1M of EACH bucket =
    input $4 + output $20 + 5m write $5 + 1h write $8 + cache read $0.20 =
    $37.20. (Under the OLD uniform 0.1x read ratio this would have been $37.60 --
    so this number proves the override is actually applied, not merely stored.)"""
    tier = cost.resolve_price_tier(_pricing_table(), "claude-opus-5-5", on_date=date(2026, 10, 11))
    usage = cost.ThreadUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_5m_input_tokens=1_000_000,
        cache_creation_1h_input_tokens=1_000_000,
    )
    assert tier.cost_usd(usage) == pytest.approx(37.20)


def test_sonnet_5_5_prices_a_million_of_every_bucket_to_the_hand_computed_total():
    """input $2 + output $10 + 5m write $2.50 + 1h write $4 + cache read $0.10 =
    $18.60 (vs $18.70 under the old uniform 0.1x read ratio)."""
    tier = cost.resolve_price_tier(_pricing_table(), "claude-sonnet-5-5", on_date=date(2026, 10, 11))
    usage = cost.ThreadUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_5m_input_tokens=1_000_000,
        cache_creation_1h_input_tokens=1_000_000,
    )
    assert tier.cost_usd(usage) == pytest.approx(18.60)


def test_price_usage_applies_the_opus_5_5_cache_read_override_for_judge_calls():
    """A realistic Opus 5.5 JUDGE call (the 2026-10-11 judge model): 1,000 full
    input + 500 output + 200,000 cache-read tokens = $0.004 + $0.010 + $0.040
    (200,000 x $0.20/MTok) = $0.054. At the old 0.1x read ratio the cache read
    alone would have been $0.08."""
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 200_000,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
    }
    cost_usd = cost.price_usage(
        usage, model="claude-opus-5-5", pricing_table=_pricing_table(), on_date=date(2026, 10, 11)
    )
    assert cost_usd == pytest.approx(0.054)


def test_a_per_model_override_never_leaks_onto_another_family():
    """Regression guard for the merge: resolving Opus 5.5 (override) and then
    Haiku 4.5 (no override) in sequence must not contaminate the second -- the
    merge builds a fresh dict per resolution, never mutating the shared table."""
    table = _pricing_table()
    cost.resolve_price_tier(table, "claude-opus-5-5", on_date=date(2026, 10, 11))
    haiku = cost.resolve_price_tier(table, "claude-haiku-4-5", on_date=date(2026, 10, 11))
    assert haiku.cache_read_multiplier == pytest.approx(0.1)
    assert table["cache_multipliers"]["read"] == pytest.approx(0.1)


# --- Claude Haiku 5.5: TWO rate cards, priced per request (2026-10-11) ----------------


def _haiku_5_5_tier() -> cost.PriceTier:
    return cost.resolve_price_tier(_pricing_table(), "claude-haiku-5-5", on_date=date(2026, 10, 11))


def _model_request_end(input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0) -> dict:
    """One `span.model_request_end` event in the confirmed live shape (its
    `model_usage` carries a FLAT `cache_creation_input_tokens`, no TTL split, and
    the event itself carries NO thread identifier)."""
    return {
        "id": "sevt_fake",
        "is_error": False,
        "model_request_start_id": "sevt_fake_start",
        "type": "span.model_request_end",
        "processed_at": "2026-10-11T00:00:00Z",
        "model_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    }


# Three requests straddling the 100,000-token threshold, with hand-computed costs.
# STANDARD card: $0.10/$0.50 per MTok (5m write x1.25 = $0.125, read x0.1 = $0.01).
# LONG-PROMPT card: $0.50/$2.50 (5m write $0.625, read $0.05).
_REQUEST_SHORT = _model_request_end(input_tokens=10, output_tokens=500, cache_read=50_000, cache_creation=1_000)
_REQUEST_SHORT_COST = 1e-6 + 250e-6 + 125e-6 + 500e-6  # $0.000876, prompt 51,010 tokens
_REQUEST_LONG = _model_request_end(input_tokens=5, output_tokens=1_000, cache_read=150_000)
_REQUEST_LONG_COST = 2.5e-6 + 2500e-6 + 7500e-6  # $0.0100025, prompt 150,005 tokens
_REQUEST_EXACTLY_AT_THRESHOLD = _model_request_end(output_tokens=100, cache_read=100_000)
_REQUEST_EXACTLY_AT_THRESHOLD_COST = 50e-6 + 1000e-6  # $0.00105 on the STANDARD card


def test_haiku_5_5_resolves_both_rate_cards():
    tier = _haiku_5_5_tier()
    assert tier.input_per_million_usd == 0.10
    assert tier.output_per_million_usd == 0.50
    assert tier.has_long_prompt_card is True
    assert tier.long_prompt_threshold_tokens == 100_000
    assert tier.long_prompt_input_per_million_usd == 0.50
    assert tier.long_prompt_output_per_million_usd == 2.50
    # Both cards use the GLOBAL cache ratios against their OWN input rate -- which
    # reproduces the page's printed per-MTok cache rates for each card.
    assert tier.input_per_million_usd * tier.cache_write_5m_multiplier == pytest.approx(0.125)
    assert tier.input_per_million_usd * tier.cache_write_1h_multiplier == pytest.approx(0.20)
    assert tier.input_per_million_usd * tier.cache_read_multiplier == pytest.approx(0.01)
    assert tier.long_prompt_input_per_million_usd * tier.cache_write_5m_multiplier == pytest.approx(0.625)
    assert tier.long_prompt_input_per_million_usd * tier.cache_write_1h_multiplier == pytest.approx(1.00)
    assert tier.long_prompt_input_per_million_usd * tier.cache_read_multiplier == pytest.approx(0.05)


def test_request_prompt_tokens_counts_cache_reads_and_writes():
    """The page: 'A request's prompt length counts all of its input tokens,
    including cache reads and cache writes' -- and NOT its output tokens."""
    request = cost.RequestUsage.from_model_usage(
        {"input_tokens": 10, "output_tokens": 999_999, "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 1_000}
    )
    assert request.prompt_tokens == 51_010


def test_card_selection_is_strictly_above_the_threshold():
    """Exactly 100,000 prompt tokens pays the STANDARD card (the cheap card covers
    'prompts up to 100,000 tokens'); 100,001 crosses over."""
    tier = _haiku_5_5_tier()
    assert tier.card_for(cost.RequestUsage(cache_read_input_tokens=99_999)) == cost.CARD_STANDARD
    assert tier.card_for(cost.RequestUsage(cache_read_input_tokens=100_000)) == cost.CARD_STANDARD
    assert tier.card_for(cost.RequestUsage(cache_read_input_tokens=100_001)) == cost.CARD_LONG_PROMPT


def test_haiku_5_5_prices_each_request_on_its_own_card():
    tier = _haiku_5_5_tier()
    requests = cost.extract_request_usages(
        [_REQUEST_SHORT, _REQUEST_LONG, _REQUEST_EXACTLY_AT_THRESHOLD, {"type": "agent.message", "content": "ignored"}]
    )
    assert len(requests) == 3

    total, counts = cost.price_requests(requests, price_tier=tier)

    assert counts == {cost.CARD_STANDARD: 2, cost.CARD_LONG_PROMPT: 1}
    assert total == pytest.approx(_REQUEST_SHORT_COST + _REQUEST_LONG_COST + _REQUEST_EXACTLY_AT_THRESHOLD_COST)
    # The whole point: the same tokens priced on ONE card would be wrong. The
    # long-prompt request alone costs ~5x what the standard card would charge.
    standard_only = tier._cost_on_card(
        input_tokens=5,
        output_tokens=1_000,
        cache_read_input_tokens=150_000,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        input_per_million_usd=tier.input_per_million_usd,
        output_per_million_usd=tier.output_per_million_usd,
    )
    assert _REQUEST_LONG_COST == pytest.approx(standard_only * 5)


def test_extract_request_usages_ignores_other_events_and_usage_less_requests():
    events = [
        {"type": "session.status_running", "id": "sevt_1"},
        {"type": "span.model_request_start", "id": "sevt_2"},
        {"type": "span.model_request_end", "id": "sevt_3"},  # no model_usage at all
        _REQUEST_SHORT,
    ]
    assert cost.extract_request_usages(events) == [
        cost.RequestUsage(input_tokens=10, output_tokens=500, cache_read_input_tokens=50_000, cache_creation_input_tokens=1_000)
    ]


def test_aggregate_pricing_refuses_a_rate_carded_tier():
    """A thread's aggregate usage cannot say which card each request was billed
    on, so `cost_usd()` must refuse rather than pick one."""
    with pytest.raises(cost.PerRequestPricingError):
        _haiku_5_5_tier().cost_usd(cost.ThreadUsage(input_tokens=100))


def test_price_usage_refuses_a_rate_carded_model():
    """`price_usage()` (judge-call pricing) takes a FLAT usage dict -- same
    refusal, so a judge accidentally pointed at Haiku 5.5 fails loud instead of
    being mis-priced."""
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
    }
    with pytest.raises(cost.PerRequestPricingError):
        cost.price_usage(usage, model="claude-haiku-5-5", pricing_table=_pricing_table(), on_date=date(2026, 10, 11))


# --- Per-request integrity check + the fail-loud gates ---------------------------------


def _haiku_5_5_thread(**overrides) -> dict:
    """A single-thread Haiku 5.5 session whose usage EXACTLY matches the three
    hand-computed requests above (input 15, output 1,600, cache read 300,000,
    cache write 1,000 -- all 5m)."""
    defaults = dict(
        input_tokens=15,
        output_tokens=1_600,
        cache_read=300_000,
        cache_5m=1_000,
        cache_1h=0,
    )
    defaults.update(overrides)
    return _thread(
        "sthr_1",
        agent_id="agent_haiku55",
        model_id="claude-haiku-5-5",
        parent_thread_id=None,
        created_at="1",
        **defaults,
    )


_HAIKU_5_5_EVENTS = [_REQUEST_SHORT, _REQUEST_LONG, _REQUEST_EXACTLY_AT_THRESHOLD]
_HAIKU_5_5_EXPECTED_TOTAL = _REQUEST_SHORT_COST + _REQUEST_LONG_COST + _REQUEST_EXACTLY_AT_THRESHOLD_COST


def test_mine_session_cost_prices_a_rate_carded_thread_per_request():
    breakdown = cost.mine_session_cost(
        "sesn_1",
        [_haiku_5_5_thread()],
        pricing_table=_pricing_table(),
        on_date=date(2026, 10, 11),
        events=_HAIKU_5_5_EVENTS,
    )

    assert breakdown.total_cost_usd == round(_HAIKU_5_5_EXPECTED_TOTAL, 4)
    thread = breakdown.threads[0]
    assert thread.model == "claude-haiku-5-5"
    assert thread.request_card_counts == {"standard": 2, "long_prompt": 1}
    # Transparency: the breakdown survives serialization for cost.json readers.
    assert thread.to_dict()["request_card_counts"] == {"standard": 2, "long_prompt": 1}
    assert cost.ThreadCost.from_dict(thread.to_dict()) == thread


def test_mine_session_cost_raises_when_a_rate_carded_model_has_no_events():
    with pytest.raises(cost.PerRequestPricingError, match="no session events were supplied"):
        cost.mine_session_cost(
            "sesn_1", [_haiku_5_5_thread()], pricing_table=_pricing_table(), on_date=date(2026, 10, 11)
        )


def test_mine_session_cost_raises_for_a_rate_carded_model_in_a_multi_thread_session():
    """`span.model_request_end` carries NO thread identifier (confirmed across
    every stored run), so a request cannot be attributed to a thread -- a
    multi-thread session containing a rate-carded agent is refused by name."""
    threads = [
        _thread("sthr_1", agent_id="agent_coord", model_id="claude-sonnet-5-5", parent_thread_id=None, created_at="1"),
        _haiku_5_5_thread() | {"id": "sthr_2", "parent_thread_id": "sthr_1", "created_at": "2"},
    ]
    with pytest.raises(cost.PerRequestPricingError, match="single-thread sessions only"):
        cost.mine_session_cost(
            "sesn_1", threads, pricing_table=_pricing_table(), on_date=date(2026, 10, 11), events=_HAIKU_5_5_EVENTS
        )


def test_mine_session_cost_raises_when_the_events_carry_no_model_requests():
    with pytest.raises(cost.PerRequestPricingError, match="no span.model_request_end event"):
        cost.mine_session_cost(
            "sesn_1",
            [_haiku_5_5_thread()],
            pricing_table=_pricing_table(),
            on_date=date(2026, 10, 11),
            events=[{"type": "session.status_idle", "id": "sevt_1"}],
        )


def test_mine_session_cost_raises_when_the_thread_reports_one_hour_cache_writes():
    """The flat per-request `cache_creation_input_tokens` cannot be split back
    into TTL buckets, so a non-zero 1h bucket is unattributable -- refuse."""
    thread = _haiku_5_5_thread(cache_5m=500, cache_1h=500)
    with pytest.raises(cost.PerRequestPricingError, match="1-HOUR cache-write tokens"):
        cost.mine_session_cost(
            "sesn_1", [thread], pricing_table=_pricing_table(), on_date=date(2026, 10, 11), events=_HAIKU_5_5_EVENTS
        )


def test_mine_session_cost_raises_when_the_per_request_sums_miss_the_thread_totals():
    """The integrity check: a dropped/truncated event list under-sums the thread's
    own totals and must fail loud, never silently under-report cost."""
    with pytest.raises(cost.PerRequestPricingError, match="do NOT sum to the thread's own usage totals"):
        cost.mine_session_cost(
            "sesn_1",
            [_haiku_5_5_thread()],
            pricing_table=_pricing_table(),
            on_date=date(2026, 10, 11),
            events=[_REQUEST_SHORT, _REQUEST_LONG],  # the third request is missing
        )


def test_verify_request_usages_names_every_mismatching_field():
    requests = [cost.RequestUsage(input_tokens=1, output_tokens=2, cache_read_input_tokens=3, cache_creation_input_tokens=4)]
    usage = cost.ThreadUsage(
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=30,
        cache_creation_5m_input_tokens=40,
        cache_creation_1h_input_tokens=0,
    )
    with pytest.raises(cost.PerRequestPricingError) as excinfo:
        cost.verify_request_usages_match_thread(requests, usage, model_id="claude-haiku-5-5", thread_id="sthr_1")
    message = str(excinfo.value)
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation"):
        assert field in message
    assert "sthr_1" in message


def test_verify_request_usages_passes_on_an_exact_match():
    """The real invariant, as confirmed against the stored single-thread runs on
    2026-10-11: per-request sums equal the thread's own totals to the token."""
    requests = cost.extract_request_usages(_HAIKU_5_5_EVENTS)
    usage = cost.ThreadUsage.from_api_usage(_haiku_5_5_thread()["usage"])
    cost.verify_request_usages_match_thread(requests, usage, model_id="claude-haiku-5-5", thread_id="sthr_1")


def test_events_are_ignored_for_a_non_rate_carded_model():
    """A family priced off one card must behave IDENTICALLY with and without
    events -- the new parameter changes nothing for Sonnet/Haiku 4.5/Opus."""
    thread = _thread(
        "sthr_1",
        agent_id="agent_x",
        model_id="claude-sonnet-5",
        parent_thread_id=None,
        created_at="1",
        input_tokens=1_000,
        output_tokens=2_000,
    )
    table = _pricing_table()
    without = cost.mine_session_cost("sesn_1", [thread], pricing_table=table, on_date=date(2026, 10, 11))
    with_events = cost.mine_session_cost(
        "sesn_1", [thread], pricing_table=table, on_date=date(2026, 10, 11), events=_HAIKU_5_5_EVENTS
    )
    assert without == with_events
    assert without.threads[0].request_card_counts is None
    assert "request_card_counts" not in without.threads[0].to_dict()


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


def test_check_pricing_drift_reports_no_issues_for_todays_capture_date():
    """`--check-pricing-drift` must stay green for EVERY declared family on the
    2026-10-11 capture date -- including the three new 5.5 families and the two
    retained legacy ones."""
    issues = cost.check_pricing_drift(_pricing_table(), on_date=date(2026, 10, 11))
    assert issues == []


def test_check_pricing_drift_covers_every_declared_family_including_the_new_ones():
    table = _pricing_table()
    assert set(table["models"]) == {
        "claude-sonnet-5",
        "claude-sonnet-5-5",
        "claude-haiku-4-5",
        "claude-haiku-5-5",
        "claude-opus-4-8",
        "claude-opus-5-5",
    }
    for family in table["models"]:
        # Resolves on the capture date AND a year later (no tier expires) -- the
        # drift check's own guarantee, asserted per family so a future edit that
        # adds an effective_until without a successor fails HERE, by name.
        assert cost.resolve_price_tier(table, family, on_date=date(2026, 10, 11))
        assert cost.resolve_price_tier(table, family, on_date=date(2027, 10, 11))


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


# --- REVIEWER-ADDED (2026-10-11 review): rate-card edge cases + path equivalence ------


def test_the_threshold_counts_cache_WRITE_tokens_too_at_the_boundary():
    """The existing boundary test straddles 100,000 using cache READS only. The
    page's wording covers writes as well ("including cache reads and cache
    writes"), and the two are different fields on the event -- so pin the boundary
    for a write-only prompt and for a mixed one. Billing a 100,001-token prompt on
    the cheap card would under-price that request ~5x."""
    tier = _haiku_5_5_tier()
    assert tier.card_for(cost.RequestUsage(cache_creation_input_tokens=100_000)) == cost.CARD_STANDARD
    assert tier.card_for(cost.RequestUsage(cache_creation_input_tokens=100_001)) == cost.CARD_LONG_PROMPT
    # Mixed: 40,000 input + 55,000 read + 5,000 write = exactly 100,000 -> cheap card;
    # one more token of ANY of the three crosses over.
    assert (
        tier.card_for(
            cost.RequestUsage(input_tokens=40_000, cache_read_input_tokens=55_000, cache_creation_input_tokens=5_000)
        )
        == cost.CARD_STANDARD
    )
    assert (
        tier.card_for(
            cost.RequestUsage(input_tokens=40_001, cache_read_input_tokens=55_000, cache_creation_input_tokens=5_000)
        )
        == cost.CARD_LONG_PROMPT
    )


def test_output_tokens_never_push_a_request_onto_the_long_prompt_card():
    """`prompt_tokens` is input-side only. A request with a small prompt and a huge
    completion must stay on the cheap card -- counting output would over-price it
    5x (and, since output is the one bucket a cheap model spends most of its money
    on, badly distort a Haiku 5.5 total)."""
    tier = _haiku_5_5_tier()
    request = cost.RequestUsage(input_tokens=100_000, output_tokens=1_000_000)
    assert request.prompt_tokens == 100_000
    assert tier.card_for(request) == cost.CARD_STANDARD
    # 100,000 x $0.10/MTok + 1,000,000 x $0.50/MTok
    assert tier.cost_usd_for_request(request) == pytest.approx(0.01 + 0.50)


def test_per_request_and_aggregate_pricing_agree_for_a_single_card_model():
    """`price_requests()` is documented as safe for a tier WITHOUT a long-prompt
    card. Prove the two code paths are arithmetically identical there -- including
    for a family with a per-model cache-read OVERRIDE (Sonnet 5.5's 0.05x), which
    is the case where a second implementation of the same arithmetic could quietly
    disagree."""
    tier = cost.resolve_price_tier(_pricing_table(), "claude-sonnet-5-5", on_date=date(2026, 10, 11))
    requests = [
        cost.RequestUsage(input_tokens=10, output_tokens=500, cache_read_input_tokens=50_000, cache_creation_input_tokens=1_000),
        cost.RequestUsage(input_tokens=5, output_tokens=1_000, cache_read_input_tokens=250_000),
        cost.RequestUsage(input_tokens=0, output_tokens=0),
    ]
    aggregate = cost.ThreadUsage(
        input_tokens=sum(r.input_tokens for r in requests),
        output_tokens=sum(r.output_tokens for r in requests),
        cache_read_input_tokens=sum(r.cache_read_input_tokens for r in requests),
        cache_creation_5m_input_tokens=sum(r.cache_creation_input_tokens for r in requests),
    )

    per_request, counts = cost.price_requests(requests, price_tier=tier)

    assert counts == {cost.CARD_STANDARD: 3, cost.CARD_LONG_PROMPT: 0}
    assert per_request == pytest.approx(tier.cost_usd(aggregate))


def test_every_pricing_json_family_reproduces_the_pages_printed_per_mtok_rates():
    """ONE table-driven pin of every number on the 2026-10-11 pricing page, so a
    future edit to any single family's base rate or cache override has to update
    this table too. Rates are per MTok: (input, 5m write, 1h write, cache read,
    output)."""
    expected = {
        "claude-opus-5-5": (4.0, 5.0, 8.0, 0.20, 20.0),
        "claude-sonnet-5-5": (2.0, 2.50, 4.0, 0.10, 10.0),
        "claude-sonnet-5": (2.0, 2.50, 4.0, 0.20, 10.0),
        "claude-opus-4-8": (5.0, 6.25, 10.0, 0.50, 25.0),
        "claude-haiku-4-5": (1.0, 1.25, 2.0, 0.10, 5.0),
        "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    }
    table = _pricing_table()
    assert set(expected) == set(table["models"]), "a family was added/removed without updating this pin"

    for model, (base_in, write_5m, write_1h, read, base_out) in expected.items():
        tier = cost.resolve_price_tier(table, model, on_date=date(2026, 10, 11))
        assert tier.input_per_million_usd == pytest.approx(base_in), model
        assert tier.output_per_million_usd == pytest.approx(base_out), model
        assert tier.input_per_million_usd * tier.cache_write_5m_multiplier == pytest.approx(write_5m), model
        assert tier.input_per_million_usd * tier.cache_write_1h_multiplier == pytest.approx(write_1h), model
        assert tier.input_per_million_usd * tier.cache_read_multiplier == pytest.approx(read), model

    # Haiku 5.5's SECOND card, likewise ($0.50 / $0.625 / $1.00 / $0.05 / $2.50).
    haiku = cost.resolve_price_tier(table, "claude-haiku-5-5", on_date=date(2026, 10, 11))
    assert haiku.long_prompt_input_per_million_usd == pytest.approx(0.50)
    assert haiku.long_prompt_output_per_million_usd == pytest.approx(2.50)
    assert haiku.long_prompt_input_per_million_usd * haiku.cache_write_5m_multiplier == pytest.approx(0.625)
    assert haiku.long_prompt_input_per_million_usd * haiku.cache_write_1h_multiplier == pytest.approx(1.00)
    assert haiku.long_prompt_input_per_million_usd * haiku.cache_read_multiplier == pytest.approx(0.05)
    assert haiku.long_prompt_threshold_tokens == 100_000


def test_a_rate_carded_model_is_never_priced_off_aggregate_usage_by_any_entry_point():
    """The one invariant that protects the batch's dollar figures from being
    silently 2.5x too low: NO public entry point may cost a rate-carded tier from
    aggregate usage. Asserted per entry point so a future convenience wrapper
    can't quietly become a fourth, unguarded one."""
    table = _pricing_table()
    usage = cost.ThreadUsage(input_tokens=15, output_tokens=1_600, cache_read_input_tokens=300_000)

    with pytest.raises(cost.PerRequestPricingError):
        _haiku_5_5_tier().cost_usd(usage)
    with pytest.raises(cost.PerRequestPricingError):
        cost.price_usage(usage.to_dict(), model="claude-haiku-5-5", pricing_table=table, on_date=date(2026, 10, 11))
    with pytest.raises(cost.PerRequestPricingError):
        cost.mine_session_cost(
            "sesn_1", [_haiku_5_5_thread()], pricing_table=table, on_date=date(2026, 10, 11), events=None
        )


# --- Malformed pricing tables fail LOUD (review-fix, reviewer M1) ---------------------


def _one_family_table(
    *,
    cache_multipliers: dict | None = {"write_5m": 1.25, "write_1h": 2.0, "read": 0.1},
    model_cache_multipliers: dict | None = None,
    long_prompt: dict | None = None,
    include_long_prompt_key: bool = False,
) -> dict:
    """A minimal one-family table whose cache-multiplier and `long_prompt` shapes
    the caller controls -- the only way to exercise the malformed paths, since the
    committed `pricing.json` is (and must stay) well-formed."""
    tier: dict = {
        "label": "standard",
        "effective_from": None,
        "effective_until": None,
        "input_per_million_usd": 1.0,
        "output_per_million_usd": 5.0,
    }
    if include_long_prompt_key:
        tier["long_prompt"] = long_prompt
    entry: dict = {"aliases": ["claude-fake-1"], "tiers": [tier]}
    if model_cache_multipliers is not None:
        entry["cache_multipliers"] = model_cache_multipliers
    table: dict = {"source_url": "https://example.test", "models": {"claude-fake-1": entry}}
    if cache_multipliers is not None:
        table["cache_multipliers"] = cache_multipliers
    return table


def test_the_committed_pricing_table_still_loads_and_resolves_every_family():
    """The guard rails below must not have made the REAL table unloadable."""
    table = _pricing_table()
    for family in table["models"]:
        assert cost.resolve_price_tier(table, family, on_date=date(2026, 10, 11))


@pytest.mark.parametrize("missing_key", ["write_5m", "write_1h", "read"])
def test_a_missing_global_cache_multiplier_raises_instead_of_pricing_that_bucket_free(missing_key):
    """Each of the three was previously read with `.get(key, 0.0)`, so a typo'd or
    forgotten key priced that WHOLE cache bucket at $0 -- and cache tokens dominate
    every run this harness measures."""
    multipliers = {"write_5m": 1.25, "write_1h": 2.0, "read": 0.1}
    del multipliers[missing_key]
    table = _one_family_table(cache_multipliers=multipliers)

    with pytest.raises(cost.PricingError) as excinfo:
        cost.resolve_price_tier(table, "claude-fake-1", on_date=date(2026, 10, 11))

    assert isinstance(excinfo.value, cost.MalformedPricingTableError)
    assert missing_key in str(excinfo.value)


def test_a_table_with_no_cache_multipliers_block_at_all_raises():
    table = _one_family_table(cache_multipliers=None)

    with pytest.raises(cost.MalformedPricingTableError):
        cost.resolve_price_tier(table, "claude-fake-1", on_date=date(2026, 10, 11))


def test_a_per_model_override_cannot_remove_a_required_multiplier_but_may_narrow_it():
    """A per-model override is MERGED over the defaults, so it may legitimately
    carry just one key (Opus 5.5 / Sonnet 5.5's `{"read": 0.05}`) -- what must
    never happen is a required key being absent after that merge."""
    table = _one_family_table(model_cache_multipliers={"read": 0.05})
    tier = cost.resolve_price_tier(table, "claude-fake-1", on_date=date(2026, 10, 11))
    assert tier.cache_read_multiplier == pytest.approx(0.05)
    assert tier.cache_write_5m_multiplier == pytest.approx(1.25)  # inherited

    broken = _one_family_table(cache_multipliers={"write_5m": 1.25, "read": 0.1})
    with pytest.raises(cost.MalformedPricingTableError) as excinfo:
        cost.resolve_price_tier(broken, "claude-fake-1", on_date=date(2026, 10, 11))
    assert "write_1h" in str(excinfo.value)


def test_a_non_numeric_cache_multiplier_raises():
    table = _one_family_table(cache_multipliers={"write_5m": 1.25, "write_1h": "two", "read": 0.1})

    with pytest.raises(cost.MalformedPricingTableError):
        cost.resolve_price_tier(table, "claude-fake-1", on_date=date(2026, 10, 11))


def test_a_negative_cache_multiplier_raises():
    table = _one_family_table(cache_multipliers={"write_5m": 1.25, "write_1h": -2.0, "read": 0.1})

    with pytest.raises(cost.MalformedPricingTableError):
        cost.resolve_price_tier(table, "claude-fake-1", on_date=date(2026, 10, 11))


@pytest.mark.parametrize("missing_key", list(cost.REQUIRED_LONG_PROMPT_KEYS))
def test_a_half_declared_long_prompt_card_raises(missing_key):
    """A tier that mentions `long_prompt` at all must declare the whole card. The
    threshold case is the expensive one: it used to read as "no second card",
    aggregate-pricing a rate-carded model on its CHEAP card (4.7x under on a real
    Haiku 5.5 run); a missing rate priced long-prompt requests at $0."""
    card = {"threshold_prompt_tokens": 100_000, "input_per_million_usd": 0.5, "output_per_million_usd": 2.5}
    del card[missing_key]
    table = _one_family_table(include_long_prompt_key=True, long_prompt=card)

    with pytest.raises(cost.PricingError) as excinfo:
        cost.resolve_price_tier(table, "claude-fake-1", on_date=date(2026, 10, 11))

    assert isinstance(excinfo.value, cost.MalformedPricingTableError)
    assert missing_key in str(excinfo.value)


@pytest.mark.parametrize("threshold", [0, -1, None])
def test_a_long_prompt_card_with_a_non_positive_threshold_raises(threshold):
    """`threshold_prompt_tokens: 0` is the exact silent-aggregate-pricing bug: the
    old `if long_prompt.get("threshold_prompt_tokens")` treated falsy as absent."""
    table = _one_family_table(
        include_long_prompt_key=True,
        long_prompt={
            "threshold_prompt_tokens": threshold,
            "input_per_million_usd": 0.5,
            "output_per_million_usd": 2.5,
        },
    )

    with pytest.raises(cost.MalformedPricingTableError):
        cost.resolve_price_tier(table, "claude-fake-1", on_date=date(2026, 10, 11))


@pytest.mark.parametrize("card", [{}, None, "long"])
def test_an_empty_or_non_object_long_prompt_card_raises(card):
    table = _one_family_table(include_long_prompt_key=True, long_prompt=card)

    with pytest.raises(cost.MalformedPricingTableError):
        cost.resolve_price_tier(table, "claude-fake-1", on_date=date(2026, 10, 11))


def test_a_tier_with_no_long_prompt_key_at_all_is_still_perfectly_valid():
    tier = cost.resolve_price_tier(_one_family_table(), "claude-fake-1", on_date=date(2026, 10, 11))
    assert tier.has_long_prompt_card is False


def test_a_hand_built_tier_with_a_threshold_but_no_rates_refuses_to_price_a_long_request():
    """`resolve_price_tier()` can no longer produce this, but a hand-built
    PriceTier can -- and `or 0.0` used to price such a request at exactly $0."""
    tier = cost.PriceTier(
        model_family="test",
        label="t",
        input_per_million_usd=0.1,
        output_per_million_usd=0.5,
        cache_write_5m_multiplier=1.25,
        cache_write_1h_multiplier=2.0,
        cache_read_multiplier=0.1,
        effective_from=None,
        effective_until=None,
        source_url="",
        captured_on="",
        long_prompt_threshold_tokens=100_000,
    )
    long_request = cost.RequestUsage(input_tokens=200_000, output_tokens=10)

    with pytest.raises(cost.MalformedPricingTableError):
        tier.cost_usd_for_request(long_request)


# --- Both event shapes are accepted (review-fix, reviewer L2) -------------------------


def test_extract_request_usages_accepts_the_bare_event_list():
    events = [_model_request_end(input_tokens=10, output_tokens=20), {"type": "agent.tool_use"}]

    usages = cost.extract_request_usages(events)

    assert [u.input_tokens for u in usages] == [10]


def test_extract_request_usages_accepts_the_events_json_wrapper():
    """`run_store.write_events()` writes `{"events": [...]}`, so anything reading
    that file back hands in the WRAPPER -- iterating which used to yield the string
    key "events" and silently find zero model requests."""
    wrapped = {"events": [_model_request_end(input_tokens=10, output_tokens=20)]}

    usages = cost.extract_request_usages(wrapped)

    assert [u.input_tokens for u in usages] == [10]


def test_normalize_events_handles_the_empty_and_junk_shapes():
    assert cost.normalize_events([]) == []
    assert cost.normalize_events({}) == []
    assert cost.normalize_events({"events": None}) == []
    assert cost.normalize_events({"not_events": [1]}) == []


def test_mine_session_cost_prices_a_rate_carded_thread_from_the_events_json_wrapper():
    """End-to-end: the same rate-carded session priced from the bare list and from
    the on-disk wrapper must produce the IDENTICAL cost."""
    table = _pricing_table()
    events = _HAIKU_5_5_EVENTS

    from_list = cost.mine_session_cost(
        "sesn_1", [_haiku_5_5_thread()], pricing_table=table, on_date=date(2026, 10, 11), events=events
    )
    from_wrapper = cost.mine_session_cost(
        "sesn_1",
        [_haiku_5_5_thread()],
        pricing_table=table,
        on_date=date(2026, 10, 11),
        events={"events": events},
    )

    assert from_wrapper.total_cost_usd == from_list.total_cost_usd
    assert from_wrapper.total_cost_usd > 0
