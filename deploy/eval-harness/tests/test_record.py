"""Unit tests for eval_core/record.py (PRD docs/prd/eval-harness.md FR-16/FR-17/FR-3/FR-5,
AC-16/AC-17).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_core.record import (  # noqa: E402
    SCHEMA_VERSION,
    CandidateAggregate,
    CostBreakdownRecord,
    CriterionScore,
    EvalRecord,
    FrozenResearchOutput,
    HumanOverride,
    aggregate_replicates,
)


def _make_record(run_id, *, candidate_config_id="production", scores=None, cost_usd=2.6, overrides=None):
    scores = scores or {}
    return EvalRecord(
        run_id=run_id,
        candidate_config_id=candidate_config_id,
        session_id=f"sesn_{run_id}",
        created_at=1000,
        criterion_scores={
            criterion: CriterionScore(criterion=criterion, score=score, rationale="r", evidence="e")
            for criterion, score in scores.items()
        },
        cost=CostBreakdownRecord(total_cost_usd=cost_usd, phase_costs_usd={"research": cost_usd / 2, "writing": cost_usd / 2}),
        human_overrides=overrides or {},
    )


# --- Schema round-trip + versioning ---------------------------------------------------


def test_record_round_trips_through_to_dict_from_dict():
    record = _make_record("run_1", scores={"content_selection": 4, "factual_accuracy": 5})

    restored = EvalRecord.from_dict(record.to_dict())

    assert restored == record
    assert restored.schema_version == SCHEMA_VERSION


def test_record_to_dict_is_json_safe():
    import json

    record = _make_record("run_1", scores={"dedup": 3})
    json.dumps(record.to_dict())  # must not raise


def test_human_override_round_trips():
    override = HumanOverride(criterion="length_format", agreed=False, overridden_score=2, comment="too long", reviewer="owner", reviewed_at=123)
    assert HumanOverride.from_dict(override.to_dict()) == override


def test_frozen_research_output_round_trips():
    frozen = FrozenResearchOutput(
        frozen_id="frozen_1", session_id="sesn_x", candidates_json=[{"title": "a", "source": "b", "disposition": "included"}],
        research_markdown="# research notes", created_at=999,
    )
    assert FrozenResearchOutput.from_dict(frozen.to_dict()) == frozen


# --- effective_score (human override wins) -------------------------------------------


def test_effective_score_uses_judge_score_when_no_override():
    record = _make_record("run_1", scores={"content_selection": 4})
    assert record.effective_score("content_selection") == 4


def test_effective_score_uses_override_when_present():
    record = _make_record(
        "run_1",
        scores={"content_selection": 4},
        overrides={"content_selection": HumanOverride(criterion="content_selection", agreed=False, overridden_score=2, comment="disagree")},
    )
    assert record.effective_score("content_selection") == 2


def test_effective_score_agreement_without_override_score_keeps_judge_score():
    """A reviewer who AGREES (no overridden_score) must not silently null out the
    judge's own score."""
    record = _make_record(
        "run_1",
        scores={"content_selection": 4},
        overrides={"content_selection": HumanOverride(criterion="content_selection", agreed=True, overridden_score=None, comment="")},
    )
    assert record.effective_score("content_selection") == 4


def test_effective_score_none_for_unknown_criterion():
    record = _make_record("run_1", scores={"content_selection": 4})
    assert record.effective_score("nonexistent") is None


# --- Replicate aggregation (FR-3 default n=3, FR-17) ---------------------------------


def test_aggregate_three_replicates_computes_mean_and_stdev():
    records = [
        _make_record("run_1", scores={"content_selection": 4, "factual_accuracy": 5}, cost_usd=2.5),
        _make_record("run_2", scores={"content_selection": 3, "factual_accuracy": 5}, cost_usd=2.7),
        _make_record("run_3", scores={"content_selection": 5, "factual_accuracy": 4}, cost_usd=2.6),
    ]

    aggregate = aggregate_replicates(records)

    assert isinstance(aggregate, CandidateAggregate)
    assert aggregate.candidate_config_id == "production"
    assert aggregate.replicate_count == 3

    cs = aggregate.criterion_aggregates["content_selection"]
    assert cs.n == 3
    assert cs.mean == 4.0
    assert cs.min == 3
    assert cs.max == 5
    assert cs.stdev is not None and cs.stdev > 0

    assert aggregate.mean_cost_usd == (2.5 + 2.7 + 2.6) / 3
    assert aggregate.cost_stdev_usd is not None


def test_aggregate_handles_partial_insufficient_data_per_criterion():
    """A criterion that was insufficient_data (score=None) on one replicate must be
    aggregated only over the replicates that DID produce a real score -- not coerced
    to 0 and not dropped from the aggregate entirely."""
    records = [
        _make_record("run_1", scores={"content_selection": 4}),
        _make_record("run_2", scores={}),  # insufficient_data on this replicate -- no candidates.json
        _make_record("run_3", scores={"content_selection": 2}),
    ]

    aggregate = aggregate_replicates(records)

    cs = aggregate.criterion_aggregates["content_selection"]
    assert cs.n == 2
    assert cs.mean == 3.0


def test_aggregate_single_replicate_has_no_stdev():
    records = [_make_record("run_1", scores={"content_selection": 4})]

    aggregate = aggregate_replicates(records)

    cs = aggregate.criterion_aggregates["content_selection"]
    assert cs.n == 1
    assert cs.stdev is None
    assert aggregate.cost_stdev_usd is None


def test_aggregate_empty_list_does_not_raise():
    aggregate = aggregate_replicates([])

    assert aggregate.replicate_count == 0
    assert aggregate.criterion_aggregates == {}
    assert aggregate.mean_cost_usd is None


def test_aggregate_prefers_human_override_over_raw_judge_score():
    records = [
        _make_record(
            "run_1",
            scores={"content_selection": 4},
            overrides={"content_selection": HumanOverride(criterion="content_selection", agreed=False, overridden_score=1, comment="bad")},
        ),
        _make_record("run_2", scores={"content_selection": 4}),
    ]

    aggregate = aggregate_replicates(records)

    # (1 override + 4 raw) / 2 = 2.5, not (4+4)/2 = 4 -- proves the override is used.
    assert aggregate.criterion_aggregates["content_selection"].mean == 2.5
