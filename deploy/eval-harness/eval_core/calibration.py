"""FR-15: calibration against real reader feedback (PRD docs/prd/eval-harness.md
§C, AC-15).

Read-only, cross-stack query against the `brief-feedback` DynamoDB table (owned by
`deploy/feedback/`, ADR-0011/ADR-0012). For editions that have BOTH an automated
evaluation and real reader submissions, computes a correlation between judge scores
and reader scores on the axes that map onto each other:
  - contentSelection (reader)  <-> content_selection (FR-6 judge)
  - length (reader)            <-> length_format (FR-9 judge)
FR-7 (factual accuracy) and FR-11 (dedup) have no direct reader-rated equivalent in
the feedback form's seven graded questions -- that's fine, per the PRD; this module
simply does not attempt a correlation for those two axes.

Anonymity: `brief-feedback` rows carry a `briefDate` attribute but ONLY carry an
`identity` attribute on non-anonymous rows (see deploy/feedback/functions/submit/
handler.py). This module never reads, logs, or otherwise touches the `identity`
field -- it only ever needs `briefDate` (to join against an eval record's edition)
and the seven graded-answer attributes (which are not identity). An anonymous
submission (no `identity` key at all) still contributes fully to the score
correlation -- scores are never anonymized, only identity is (per FR-15's explicit
requirement) -- it is simply never a candidate for de-anonymization because this
module has no code path that reads or reconstructs identity from anything.

PORTED, UNCHANGED (ADR-0016 "Eval-harness re-integration" Phase 1, 2026-07-07) from
`deploy/eval/eval_core/calibration.py`. Per ADR-0016's cross-cutting section,
calibration is DE-SCOPED from the harness's core git-native loop -- it is the one
legacy feature that reads an AWS resource (`brief-feedback`, DynamoDB) and is not
part of the §4.1 UI requirements. It is kept here, parked, as an optional,
separately-invoked local script an operator can still run with read-only AWS creds
when wanted (not deleted; not wired into `harness/run.py` or the Flask UI).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

# Maps a v1 judge criterion name (eval_core.record's criterion keys) to the
# corresponding brief-feedback graded-question attribute name (see
# deploy/feedback/functions/submit/handler.py's GRADED_QUESTION_KEYS). Only criteria
# with a genuine reader-rated equivalent appear here -- FR-7/FR-11 are deliberately
# absent (PRD: "that's fine, just correlate what maps").
CRITERION_TO_FEEDBACK_FIELD = {
    "content_selection": "contentSelection",
    "length_format": "length",
}


@dataclass(frozen=True)
class CriterionCorrelation:
    """One criterion's judge-vs-reader correlation across the editions where both
    exist. `insufficient_data=True` (with `correlation=None`) when fewer than 2
    editions have both a judge score and a reader score for this criterion --
    Pearson's r is undefined/meaningless below that, and the PRD explicitly asks for
    "report insufficient feedback to calibrate" rather than a spurious number (§7)."""

    criterion: str
    n_editions: int
    correlation: float | None
    insufficient_data: bool = False


def _pearson_r(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation coefficient, or None if undefined (fewer than 2 points, or
    zero variance in either series -- division by zero)."""
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    sum_sq_x = sum((x - mean_x) ** 2 for x in xs)
    sum_sq_y = sum((y - mean_y) ** 2 for y in ys)
    denominator = (sum_sq_x * sum_sq_y) ** 0.5
    if denominator == 0:
        return None
    return numerator / denominator


def correlate_judge_vs_reader(
    judge_scores_by_date: dict[str, dict[str, int]],
    feedback_rows: list[dict[str, Any]],
) -> dict[str, CriterionCorrelation]:
    """Compute per-criterion correlation between judge scores and reader scores.

    `judge_scores_by_date`: {"2026-07-03": {"content_selection": 4, "length_format": 3, ...}, ...}
      -- one eval record's (or its aggregate's) effective scores, keyed by the
      edition date it evaluated.
    `feedback_rows`: raw `brief-feedback` items (as returned by a DynamoDB Query/Scan),
      each optionally containing `briefDate` and the seven graded-answer attributes.
      Rows with no `briefDate` (the walk-up/no-token case, or a tampered token --
      FR-10/FR-12 of reader-feedback.md) cannot be joined to an edition and are
      skipped -- not an error, just not usable for this join.

    Returns one `CriterionCorrelation` per criterion in `CRITERION_TO_FEEDBACK_FIELD`.
    """
    results: dict[str, CriterionCorrelation] = {}

    # Group feedback rows by briefDate first (a single edition may have multiple
    # reader submissions; average them per-edition so one edition = one data point,
    # matching the judge side which is also one score per edition).
    feedback_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in feedback_rows:
        brief_date = row.get("briefDate")
        if not brief_date:
            continue
        feedback_by_date.setdefault(brief_date, []).append(row)

    for criterion, feedback_field in CRITERION_TO_FEEDBACK_FIELD.items():
        judge_xs: list[float] = []
        reader_ys: list[float] = []
        for brief_date, criteria_scores in judge_scores_by_date.items():
            judge_score = criteria_scores.get(criterion)
            if judge_score is None:
                continue
            rows_for_date = feedback_by_date.get(brief_date)
            if not rows_for_date:
                continue
            reader_values = [row[feedback_field] for row in rows_for_date if feedback_field in row]
            if not reader_values:
                continue
            judge_xs.append(float(judge_score))
            reader_ys.append(statistics.mean(reader_values))

        if len(judge_xs) < 2:
            results[criterion] = CriterionCorrelation(
                criterion=criterion, n_editions=len(judge_xs), correlation=None, insufficient_data=True
            )
            continue

        r = _pearson_r(judge_xs, reader_ys)
        results[criterion] = CriterionCorrelation(
            criterion=criterion,
            n_editions=len(judge_xs),
            correlation=r,
            insufficient_data=r is None,
        )

    return results


def extract_free_text_feedback(feedback_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Surface reader free-text (`additionalSources`, `otherFeedback`) for the review
    UI (PRD FR-15: "surface reader free-text suggestions... into the review context").

    Returns a list of `{"briefDate": ..., "additionalSources": ..., "otherFeedback": ...}`
    dicts, omitting empty strings, and NEVER including any identity field regardless of
    whether the source row was attributed or anonymous -- this function's return shape
    simply has no field for it."""
    surfaced = []
    for row in feedback_rows:
        additional_sources = row.get("additionalSources") or ""
        other_feedback = row.get("otherFeedback") or ""
        if not additional_sources and not other_feedback:
            continue
        entry = {"briefDate": row.get("briefDate") or ""}
        if additional_sources:
            entry["additionalSources"] = additional_sources
        if other_feedback:
            entry["otherFeedback"] = other_feedback
        surfaced.append(entry)
    return surfaced


def query_feedback_table(table) -> list[dict[str, Any]]:
    """Read-only, paginated Scan of the `brief-feedback` table.

    `table` is a `boto3.resource("dynamodb").Table("brief-feedback")` object (its
    `.scan()` already returns plain, deserialized Python dicts under `"Items"`, unlike
    the low-level `boto3.client("dynamodb")` attribute-value wire format) -- this
    matches how `deploy/subscribers`'s handlers already use the resource-level Table
    interface (see `subscriber_common.get_table()`). A Scan (not a Query) is used
    because there is no access pattern here for "all rows" other than a full scan;
    the eval stack's IAM grant for this table is `dynamodb:Scan`/`GetItem` only, never
    Put/Update/Delete (PRD FR-21/AC-21, see `brief_eval/stack.py`).
    """
    items: list[dict[str, Any]] = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


__all__ = [
    "CRITERION_TO_FEEDBACK_FIELD",
    "CriterionCorrelation",
    "correlate_judge_vs_reader",
    "extract_free_text_feedback",
    "query_feedback_table",
]
