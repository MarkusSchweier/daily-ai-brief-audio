"""FR-11: day-over-day dedup judge (LLM-judge only, no human agree/override step per
PRD §4.B -- reported for visibility).

Given the brief and the recent prior briefs, judges whether any story is a repeat
(verifying the pipeline's existing `brief_history.read_recent_prior_briefs` dedup
mechanism is actually working). Reuses that existing helper rather than
reimplementing prior-brief retrieval -- per this epic's explicit instruction.
"""

from __future__ import annotations

from typing import Any

from .base import JSON_RESPONSE_INSTRUCTION, JudgeResult, run_judge

CRITERION = "dedup"

_SYSTEM_PROMPT = (
    "You are a repetition-detection judge for a daily AI news brief. You will be given "
    "today's brief and one or more recent PRIOR editions. Judge whether today's brief "
    "repeats a story from a prior edition WITHOUT it being a genuine, clearly-labeled "
    "follow-up (a follow-up that adds new information -- e.g. 'update: X now confirms Y' -- "
    "is fine and expected; a bare rehash of the same story with no new information is a "
    "dedup failure). Score 1 (repeats a prior story with no new information) to 5 (no "
    "unlabeled repetition -- every story is either new or a genuine, clearly-flagged "
    "follow-up). If no prior briefs were available to compare against, say so and treat "
    "dedup as inapplicable via insufficient_data. " + JSON_RESPONSE_INSTRUCTION
)


def judge_dedup(client: Any, *, brief_markdown: str, prior_briefs_markdown: list[str]) -> JudgeResult:
    """`prior_briefs_markdown` is the list of `.markdown` bodies from
    `brief_history.PriorBrief` objects (i.e. the caller should pass
    `[p.markdown for p in brief_history.read_recent_prior_briefs(...)]`) -- this
    module does not itself call S3; it only judges once the caller has already
    resolved the prior briefs via the existing helper."""
    if not prior_briefs_markdown:
        return JudgeResult(
            criterion=CRITERION,
            score=None,
            rationale="No prior briefs were available to compare against (e.g. the first-ever run) -- dedup is not applicable.",
            evidence="",
            insufficient_data=True,
        )

    priors_section = "\n\n---\n\n".join(prior_briefs_markdown)
    user_prompt = (
        f"TODAY'S BRIEF:\n{brief_markdown}\n\n"
        f"RECENT PRIOR EDITIONS:\n{priors_section}\n\n"
        "Judge day-over-day dedup per your system instructions."
    )
    return run_judge(client, criterion=CRITERION, system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt)


__all__ = ["judge_dedup", "CRITERION"]
