"""FR-6: content-selection judge (kept, full human review).

Contrasts the candidates.json artifact (every story considered, PRD FR-4) against the
final brief's actual story list, flagging important stories that were dropped and
low-value stories that were included. Degrades gracefully -- reports
"insufficient data" rather than erroring -- when `candidates_json` is absent (a run
from before Phase 1 shipped, or the live skill-version push per ADR-0008 hasn't
happened yet).

PORTED, UNCHANGED (ADR-0016 Phase 1, 2026-07-07) from
`deploy/eval/eval_core/judges/content_selection.py`. `candidates_json` now arrives via
`candidate_sync.trigger.fetch_catted_file_contents()` (session-events retrieval)
instead of an S3 read -- this module itself is retrieval-agnostic and needed no change.
"""

from __future__ import annotations

from typing import Any

from .base import JSON_RESPONSE_INSTRUCTION, JudgeResult, run_judge

CRITERION = "content_selection"

_SYSTEM_PROMPT = (
    "You are a meticulous editorial quality judge for a daily AI news brief. You will be "
    "given the FULL list of stories/topics the research process considered (each marked "
    "included or excluded from the final brief) and the brief's actual final content. "
    "Judge whether the SELECTION was good: were any important, newsworthy stories dropped "
    "that should have been included? Were any low-value, minor, or redundant stories "
    "included that should have been cut? Score 1 (poor selection -- clear misses) to 5 "
    "(excellent selection -- the right stories were kept, the right ones were cut). "
    + JSON_RESPONSE_INSTRUCTION
)


def judge_content_selection(client: Any, *, candidates_json: list[dict] | None, brief_markdown: str) -> JudgeResult:
    """Score content selection, or degrade to insufficient_data when no candidates
    artifact exists for this run (FR-6's documented degrade path)."""
    if not candidates_json:
        return JudgeResult(
            criterion=CRITERION,
            score=None,
            rationale=(
                "No candidates.json artifact was available for this run (an older run, or "
                "one from before the live skill version emits it) -- content-selection "
                "quality cannot be judged without the full considered-vs-chosen contrast."
            ),
            evidence="",
            insufficient_data=True,
        )

    candidates_summary = "\n".join(
        f"- [{c.get('disposition', 'unknown')}] {c.get('title', '(untitled)')} ({c.get('source', 'unknown source')})"
        for c in candidates_json
    )
    user_prompt = (
        f"CANDIDATES CONSIDERED DURING RESEARCH:\n{candidates_summary}\n\n"
        f"FINAL BRIEF:\n{brief_markdown}\n\n"
        "Judge the selection quality per your system instructions."
    )
    return run_judge(client, criterion=CRITERION, system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt)


__all__ = ["judge_content_selection", "CRITERION"]
