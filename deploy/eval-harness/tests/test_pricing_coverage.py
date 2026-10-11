"""REVIEWER-ADDED (2026-10-11 model-generation comparison review): cross-module
guards that a model this repo can actually RUN is a model this repo can actually
PRICE -- checked offline, before any spend.

Why these live here and not in `test_cost.py`: they assert a relationship BETWEEN
three independently-edited surfaces (`deploy/candidates/<slug>/model.txt` +
`multiagent.json` rosters, `eval_core/judges/base.JUDGE_MODELS`, and
`pricing.json`), each of which is edited by a different change for a different
reason. Nothing else fails when they drift apart -- the failure surfaces only at
the END of a live run, AFTER the pipeline and judge calls have been paid for:

  - a candidate (or judge) model with no `pricing.json` entry raises
    `UnknownModelPriceError` in `run.py`'s post-trigger bookkeeping phase, which
    marks the repetition failed and discards every score it just paid for;
  - a MULTI-agent candidate declaring a per-request rate-carded model (Claude
    Haiku 5.5) can never be costed at all -- `_price_thread_per_request()` refuses
    a multi-thread session by design, because `span.model_request_end` carries no
    thread identifier -- so every run of that candidate would fail the same way;
  - a JUDGE pointed at a rate-carded model hits `price_usage()`'s refusal (a flat
    usage dict has no per-request breakdown).
"""

from __future__ import annotations

from datetime import date

import pytest
from candidate_sync.loader import load_candidate
from conftest import CANDIDATES_DIR

from eval_core.judges.base import JUDGE_MODELS
from harness import cost

# Any date inside every declared tier's window; the tiers in this repo's table are
# all open-ended, which `test_cost.py` asserts separately.
_ON_DATE = date(2026, 10, 11)


def _candidate_dirs():
    return sorted(p for p in CANDIDATES_DIR.iterdir() if (p / "candidate.json").is_file())


def _declared_agents():
    """`(slug, agent_label, model_id, is_multi_agent)` for every agent of every real
    candidate declaration -- coordinators and sub-agents alike."""
    out = []
    for candidate_dir in _candidate_dirs():
        candidate = load_candidate(candidate_dir)
        out.append((candidate.slug, "primary", candidate.agent.model, candidate.is_multi_agent))
        for index, sub_agent in enumerate(candidate.sub_agents, start=1):
            out.append((candidate.slug, f"sub-agent-{index}", sub_agent.model, True))
    return out


def test_there_are_candidates_to_check():
    """Guard against the two tests below passing vacuously if the candidates
    directory ever moves."""
    assert len(_declared_agents()) >= 20


def test_every_declared_candidate_model_has_a_pricing_entry():
    """A model id in a `model.txt` / roster entry that `pricing.json` has never
    heard of is only discovered after a full paid run."""
    pricing_table = cost.load_pricing_table()

    unpriceable = []
    for slug, label, model_id, _ in _declared_agents():
        try:
            cost.resolve_price_tier(pricing_table, model_id, on_date=_ON_DATE)
        except cost.PricingError as e:
            unpriceable.append(f"{slug}/{label}: {model_id!r} -- {type(e).__name__}")

    assert not unpriceable, "candidate models with no usable pricing.json tier: " + "; ".join(unpriceable)


def test_no_multi_agent_candidate_declares_a_per_request_rate_carded_model():
    """Per-request pricing (Claude Haiku 5.5's two prompt-length cards) is
    supported for SINGLE-thread sessions only -- a multi-agent candidate using
    such a model would fail at cost time on every single run, after paying for
    it. Catch the declaration instead."""
    pricing_table = cost.load_pricing_table()

    offenders = [
        f"{slug}/{label}: {model_id}"
        for slug, label, model_id, is_multi_agent in _declared_agents()
        if is_multi_agent
        and cost.resolve_price_tier(pricing_table, model_id, on_date=_ON_DATE).has_long_prompt_card
    ]

    assert not offenders, (
        "a multi-agent candidate declares a rate-carded model, which harness/cost.py cannot "
        "attribute per request (span.model_request_end carries no thread id): " + "; ".join(offenders)
    )


@pytest.mark.parametrize("criterion", sorted(JUDGE_MODELS))
def test_every_judge_model_is_priceable_and_not_rate_carded(criterion):
    """`run.py::_price_judge_results()` prices each judge call via
    `cost.price_usage()`, which takes a FLAT usage dict. So a judge model must (a)
    exist in `pricing.json` and (b) NOT be priced on per-request rate cards --
    otherwise every repetition dies after the judge calls were already billed."""
    pricing_table = cost.load_pricing_table()
    model = JUDGE_MODELS[criterion]

    tier = cost.resolve_price_tier(pricing_table, model, on_date=_ON_DATE)

    assert tier.has_long_prompt_card is False, f"judge {criterion} is pointed at rate-carded {model}"
    priced = cost.price_usage(
        {
            "input_tokens": 1000,
            "output_tokens": 1000,
            "cache_read_input_tokens": 1000,
            "cache_creation_5m_input_tokens": 1000,
            "cache_creation_1h_input_tokens": 0,
        },
        model=model,
        pricing_table=pricing_table,
        on_date=_ON_DATE,
    )
    assert priced > 0
