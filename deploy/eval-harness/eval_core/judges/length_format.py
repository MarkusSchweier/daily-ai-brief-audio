"""FR-9: length/format compliance judge (kept, full human review).

Judges the brief against the `daily-ai-brief` skill's own stated length/format target
(deploy/managed-agent/skills/daily-ai-brief/SKILL.md's "Output contract" section):
8-15 headline bullets, 5-10 deep-dive items, tiered section structure, omitting empty
sections. Flags under- or over-shoot.

PORTED, UNCHANGED (ADR-0016 Phase 1, 2026-07-07) from
`deploy/eval/eval_core/judges/length_format.py`.

UNCHANGED by judge methodology v2 (2026-07-07, owner-directed, docs/adr/0016
amendment), per the owner's explicit instruction -- this judge's PROMPT and
approach stay exactly as they were (a length/format check needs no live web
research, and its v1 approach was never implicated by either live-run finding
that motivated the v2 rework). The ONE thing that changes is its MODEL: it now
resolves `claude-opus-4-8` from `base.JUDGE_MODELS` (the same "judges must be
stronger than what they judge" principle applied uniformly to all four judges),
not the previous shared `claude-haiku-4-5`.
"""

from __future__ import annotations

from typing import Any

from .base import JSON_RESPONSE_INSTRUCTION, JUDGE_MODELS, JudgeResult, run_judge

CRITERION = "length_format"
_MODEL = JUDGE_MODELS[CRITERION]

# The skill's own stated target (SKILL.md's Output Contract + "Rank & select" section),
# restated here so the judge's rubric is explicit and doesn't require it to re-derive
# the target from a separately-fetched copy of the skill file.
_SKILL_TARGET_DESCRIPTION = (
    "The skill's stated target: 8-15 skimmable Headlines bullets, 5-10 deep-dive items "
    "total across the tiered sections (Research & Models; Industry, Deals & Strategy; "
    "Products, Tools & Releases; Benchmarks & Evals; Policy, Safety & Society), each "
    "deep-dive a dense 3-6 sentence summary, empty sections omitted, and a quiet day "
    "may legitimately be shorter than the target (not itself a defect)."
)

_SYSTEM_PROMPT = (
    "You are a format-compliance judge for a daily AI news brief. You will be given the "
    f"brief's own stated length/format target and the brief's actual content. {_SKILL_TARGET_DESCRIPTION} "
    "Judge whether the brief's ACTUAL headline count, deep-dive count, and structure "
    "comply with that target -- flag meaningful under-shoot (too thin, missing "
    "required structure) or over-shoot (bloated, padded, exceeding the stated range "
    "without a quiet-day justification). A shorter brief on a genuinely quiet news day "
    "is fine and should NOT be penalized as under-shoot if there's little padding. "
    "Score 1 (badly out of compliance) to 5 (fully compliant with the stated target). "
    + JSON_RESPONSE_INSTRUCTION
)


def judge_length_format(client: Any, *, brief_markdown: str) -> JudgeResult:
    user_prompt = f"BRIEF:\n{brief_markdown}\n\nJudge length/format compliance against the stated target per your system instructions."
    return run_judge(client, criterion=CRITERION, system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt, model=_MODEL)


__all__ = ["judge_length_format", "CRITERION"]
