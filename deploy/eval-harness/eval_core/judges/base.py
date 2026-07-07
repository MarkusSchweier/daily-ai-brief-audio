"""Shared judge plumbing: the `JudgeResult` shape and the one Anthropic Messages API
call wrapper every v1 judge (content selection, accuracy, length/format, dedup) uses.

G-Eval style: the caller supplies a rubric + scoring scale in the prompt and asks the
judge to reason ("rationale") before committing to a score, then to also cite
"evidence" (a short quote/example) supporting that score -- matching PRD FR-6/FR-15's
"score plus judge rationale plus supporting evidence" shape for every kept criterion.

Uses the standard Anthropic **Messages API** (a normal one-shot API call), not a
Managed Agents session -- these are narrow, well-scoped scoring tasks, not open-ended
research, so a modest model (Haiku) is appropriate and keeps judge cost low relative
to the pipeline run being judged (PRD §7: "the judge's own cost should be reported so
it isn't confused with pipeline cost").

PORTED, UNCHANGED (ADR-0016 "Eval-harness re-integration" Phase 1, 2026-07-07) from
`deploy/eval/eval_core/judges/base.py` -- a judge call is a plain Messages API call
regardless of where the run's artifacts it scores came from (S3, in the old
backbone, vs. `candidate_sync.trigger.fetch_catted_file_contents()`'s session-events
retrieval, in this one), so this plumbing needed no change at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

JUDGE_MODEL = "claude-haiku-4-5"


@dataclass(frozen=True)
class JudgeResult:
    """One judge's output for one criterion: PRD FR-6/FR-15's "score plus judge
    rationale plus supporting evidence" shape.

    `score` is on the judge's own stated 1-5 scale (matching the reader-feedback
    table's 1-5 grading, so FR-15's calibration join can compare like-for-like without
    a rescale). `insufficient_data=True` marks a judge's graceful degrade (e.g.
    content-selection with no candidates.json, FR-6's documented degrade path) --
    when True, `score` is None and `rationale` explains why, rather than a
    fabricated/guessed score.
    """

    criterion: str
    score: int | None
    rationale: str
    evidence: str
    insufficient_data: bool = False


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first top-level JSON object from `text`. Judge prompts ask for a
    JSON-only response, but models occasionally wrap it in prose or a code fence --
    tolerate both by finding the outermost {...} span rather than requiring the whole
    response to be valid JSON on its own."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in judge response: {text!r}")
    return json.loads(text[start : end + 1])


def run_judge(
    client: Any,
    *,
    criterion: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1024,
) -> JudgeResult:
    """Call the Messages API once and parse a `JudgeResult` from the response.

    `client` is an Anthropic SDK client (or a test double exposing the same
    `messages.create(...)` shape) -- injected so tests never need a real API key or
    network access. The judge is instructed to respond with a single JSON object:
    `{"score": 1-5 | null, "rationale": "...", "evidence": "...",
    "insufficient_data": bool}`. A malformed/unparseable response degrades to an
    `insufficient_data=True` result rather than raising -- a judge call must never
    crash the harness's evaluation run over an LLM formatting slip.
    """
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")

    try:
        parsed = _extract_json_object(text)
    except (ValueError, json.JSONDecodeError):
        return JudgeResult(
            criterion=criterion,
            score=None,
            rationale=f"Judge response could not be parsed as JSON: {text[:500]!r}",
            evidence="",
            insufficient_data=True,
        )

    insufficient = bool(parsed.get("insufficient_data", False))
    raw_score = parsed.get("score")
    score = None if insufficient or raw_score is None else int(raw_score)

    return JudgeResult(
        criterion=criterion,
        score=score,
        rationale=str(parsed.get("rationale", "")),
        evidence=str(parsed.get("evidence", "")),
        insufficient_data=insufficient,
    )


JSON_RESPONSE_INSTRUCTION = (
    "Respond with ONLY a single JSON object, no other text, no markdown code fence, "
    'in exactly this shape: {"score": <integer 1-5, or null if insufficient_data>, '
    '"rationale": "<your reasoning, 2-4 sentences, written BEFORE you decide the '
    'score>", "evidence": "<a short supporting quote or concrete example from the '
    'input>", "insufficient_data": <true only if you genuinely cannot judge this '
    "from the given input, false otherwise>}."
)

__all__ = ["JudgeResult", "run_judge", "JUDGE_MODEL", "JSON_RESPONSE_INSTRUCTION"]
