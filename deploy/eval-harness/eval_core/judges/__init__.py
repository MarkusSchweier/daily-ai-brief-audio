"""LLM-as-judge scoring for the v1 evaluation criteria set (PRD docs/prd/eval-harness.md
§4.B, FR-6/FR-7/FR-9/FR-11 -- FR-8/FR-10/FR-12/FR-13 are explicitly OUT OF SCOPE for
this epic and are not implemented here).

Each judge takes the run's artifacts and produces a `JudgeResult` (score + rationale +
evidence) via a G-Eval-style call to the standard Anthropic Messages API: give the
judge a rubric + scoring scale, ask for reasoning before a score, then parse a
structured result. These are fast, cheap, one-shot scoring calls (Haiku is plenty for
this narrower, well-scoped scoring task) -- NOT Managed Agents sessions.

PORTED, UNCHANGED (ADR-0016 "Eval-harness re-integration" Phase 1, 2026-07-07) from
`deploy/eval/eval_core/judges/`; see that package's own docstring and each module's
header for the full original PRD/FR references, kept intact as historical context.
"""

from .base import JudgeResult, run_judge
from .accuracy import judge_factual_accuracy
from .content_selection import judge_content_selection
from .dedup import judge_dedup
from .length_format import judge_length_format

__all__ = [
    "JudgeResult",
    "run_judge",
    "judge_content_selection",
    "judge_factual_accuracy",
    "judge_length_format",
    "judge_dedup",
]
