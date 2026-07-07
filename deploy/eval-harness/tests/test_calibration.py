"""Unit tests for eval_core/calibration.py (PRD docs/prd/eval-harness.md FR-15/AC-15).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_core.calibration import (  # noqa: E402
    correlate_judge_vs_reader,
    extract_free_text_feedback,
    query_feedback_table,
)


# --- correlate_judge_vs_reader -------------------------------------------------------


def test_perfect_positive_correlation_on_content_selection():
    judge_scores_by_date = {
        "2026-07-01": {"content_selection": 1},
        "2026-07-02": {"content_selection": 3},
        "2026-07-03": {"content_selection": 5},
    }
    feedback_rows = [
        {"briefDate": "2026-07-01", "contentSelection": 1},
        {"briefDate": "2026-07-02", "contentSelection": 3},
        {"briefDate": "2026-07-03", "contentSelection": 5},
    ]

    result = correlate_judge_vs_reader(judge_scores_by_date, feedback_rows)

    assert result["content_selection"].n_editions == 3
    assert result["content_selection"].correlation == 1.0
    assert result["content_selection"].insufficient_data is False


def test_only_correlates_criteria_with_a_reader_equivalent():
    """FR-7/FR-11 have no reader-rated equivalent -- must never appear in the result."""
    judge_scores_by_date = {"2026-07-01": {"factual_accuracy": 5, "dedup": 5, "content_selection": 4}}
    feedback_rows = [{"briefDate": "2026-07-01", "contentSelection": 4}]

    result = correlate_judge_vs_reader(judge_scores_by_date, feedback_rows)

    assert "factual_accuracy" not in result
    assert "dedup" not in result
    assert "content_selection" in result
    assert "length_format" in result  # reported as insufficient_data, not absent


def test_insufficient_data_when_fewer_than_two_editions_have_both_sides():
    judge_scores_by_date = {"2026-07-01": {"content_selection": 4}}
    feedback_rows = [{"briefDate": "2026-07-01", "contentSelection": 4}]

    result = correlate_judge_vs_reader(judge_scores_by_date, feedback_rows)

    assert result["content_selection"].insufficient_data is True
    assert result["content_selection"].correlation is None
    assert result["content_selection"].n_editions == 1


def test_no_feedback_at_all_is_insufficient_data_not_an_error():
    judge_scores_by_date = {"2026-07-01": {"content_selection": 4}, "2026-07-02": {"content_selection": 2}}

    result = correlate_judge_vs_reader(judge_scores_by_date, feedback_rows=[])

    assert result["content_selection"].insufficient_data is True
    assert result["content_selection"].n_editions == 0


def test_feedback_rows_with_no_brief_date_are_skipped_not_erroring():
    """A walk-up (no-token) or tampered-token feedback row has no briefDate
    (reader-feedback.md FR-10/FR-12) -- must be silently unusable for this join, not
    a crash."""
    judge_scores_by_date = {"2026-07-01": {"content_selection": 4}, "2026-07-02": {"content_selection": 2}}
    feedback_rows = [
        {"contentSelection": 5},  # no briefDate -- walk-up/anonymous, unattributable
        {"briefDate": "2026-07-01", "contentSelection": 4},
        {"briefDate": "2026-07-02", "contentSelection": 2},
    ]

    result = correlate_judge_vs_reader(judge_scores_by_date, feedback_rows)

    assert result["content_selection"].n_editions == 2
    assert result["content_selection"].correlation == 1.0


def test_multiple_reader_submissions_for_one_edition_are_averaged():
    judge_scores_by_date = {"2026-07-01": {"length_format": 3}, "2026-07-02": {"length_format": 5}}
    feedback_rows = [
        {"briefDate": "2026-07-01", "length": 2},
        {"briefDate": "2026-07-01", "length": 4},  # average with the row above = 3
        {"briefDate": "2026-07-02", "length": 5},
    ]

    result = correlate_judge_vs_reader(judge_scores_by_date, feedback_rows)

    assert result["length_format"].n_editions == 2


def test_never_reads_or_returns_the_identity_field():
    """Anonymous submissions (no identity attribute at all) must still fully
    contribute to correlation -- and even for non-anonymous rows, this module must
    never surface the identity value anywhere in its output."""
    judge_scores_by_date = {"2026-07-01": {"content_selection": 4}, "2026-07-02": {"content_selection": 2}}
    feedback_rows = [
        {"briefDate": "2026-07-01", "contentSelection": 4, "identity": "reader@example.com"},
        {"briefDate": "2026-07-02", "contentSelection": 2},  # anonymous -- no identity key at all
    ]

    result = correlate_judge_vs_reader(judge_scores_by_date, feedback_rows)

    assert result["content_selection"].n_editions == 2
    # No identity value anywhere in the returned dataclasses.
    import dataclasses

    for correlation in result.values():
        assert "reader@example.com" not in str(dataclasses.astuple(correlation))


# --- extract_free_text_feedback ------------------------------------------------------


def test_extract_free_text_surfaces_both_fields_when_present():
    rows = [
        {"briefDate": "2026-07-01", "additionalSources": "Cover The Batch too", "otherFeedback": "Loved it", "identity": "x@y.com"},
    ]

    surfaced = extract_free_text_feedback(rows)

    assert surfaced == [
        {"briefDate": "2026-07-01", "additionalSources": "Cover The Batch too", "otherFeedback": "Loved it"}
    ]
    assert "identity" not in surfaced[0]


def test_extract_free_text_omits_rows_with_no_free_text():
    rows = [{"briefDate": "2026-07-01", "contentSelection": 4}]
    assert extract_free_text_feedback(rows) == []


def test_extract_free_text_partial_fields_only_included_when_non_empty():
    rows = [{"briefDate": "2026-07-01", "additionalSources": "", "otherFeedback": "Great work"}]
    surfaced = extract_free_text_feedback(rows)
    assert surfaced == [{"briefDate": "2026-07-01", "otherFeedback": "Great work"}]


# --- query_feedback_table (paginated Scan) ------------------------------------------


class _FakeTable:
    def __init__(self, pages):
        self._pages = list(pages)

    def scan(self, **kwargs):
        return self._pages.pop(0)


def test_query_feedback_table_paginates_through_all_items():
    table = _FakeTable(
        [
            {"Items": [{"submissionId": "1"}], "LastEvaluatedKey": {"submissionId": "1"}},
            {"Items": [{"submissionId": "2"}]},
        ]
    )

    items = query_feedback_table(table)

    assert [i["submissionId"] for i in items] == ["1", "2"]


def test_query_feedback_table_single_page():
    table = _FakeTable([{"Items": [{"submissionId": "1"}, {"submissionId": "2"}]}])

    items = query_feedback_table(table)

    assert len(items) == 2
