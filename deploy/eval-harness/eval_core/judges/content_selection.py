"""Content-selection judge -- TARGETED UPGRADE, judge methodology v2 (2026-07-07,
owner-directed, docs/adr/0016 amendment).

The core approach is UNCHANGED (it works): contrast the `candidates.json` artifact
(every story the research process considered, marked included/excluded) against
the brief's actual final content, flagging important stories dropped or low-value
stories kept. Two additions on top:

1. **Server-side web_search/web_fetch tools, capped at `max_uses: 5`** (owner
   spec). Instruction: whenever the judge believes a story should have been
   featured (or a featured story shouldn't have made it), it goes to the
   sources/internet to SHARPEN that view before scoring -- rather than judging
   editorial priority from static familiarity with the topic alone.
2. **A structured `selection_disagreements` array** in the response JSON: for
   every case where the judge concludes it would have selected differently, it
   documents `{story, judge_view, rationale}` -- forcing the disagreement to be
   explicit and reviewable rather than buried in prose.

Model moves to **Opus 4.8** per judge methodology v2's owner-directed "judges must
be stronger than what they judge" principle (`base.JUDGE_MODELS`) -- the owner's
original spec allowed Haiku to stay "unless you find a hard reason otherwise," but
a later, binding direction moved ALL FOUR judges to Opus uniformly; see
`base.py`'s module docstring and the ADR-0016 amendment for the full rationale.

Tool schema/version: SAME as `accuracy.py` (`web_search_20250305` /
`web_fetch_20250910`, no beta header) -- see that module's docstring for the
verification notes; not repeated here.

PORTED base structure (ADR-0016 Phase 1, 2026-07-07) from
`deploy/eval/eval_core/judges/content_selection.py`; `candidates_json` arrives via
`candidate_sync.trigger.fetch_catted_file_contents()` (session-events retrieval)
instead of an S3 read -- unrelated to this v2 rework.
"""

from __future__ import annotations

from typing import Any

from .base import (
    JSON_RESPONSE_INSTRUCTION_WITH_SELECTION_DISAGREEMENTS,
    JUDGE_MODELS,
    JudgeResult,
    run_judge,
)

CRITERION = "content_selection"

# Owner spec: max_uses: 5, applied to BOTH web_search and web_fetch (fetch's cap
# is not itemized by the owner -- the same deliberate, documented safety-bound
# choice as accuracy.py's).
_MAX_TOOL_USES = 5

_MODEL = JUDGE_MODELS[CRITERION]

_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": _MAX_TOOL_USES}
_WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": _MAX_TOOL_USES}

_SYSTEM_PROMPT = (
    "You are a meticulous editorial quality judge for a daily AI news brief. You will be "
    "given the FULL list of stories/topics the research process considered (each marked "
    "included or excluded from the final brief) and the brief's actual final content. "
    "Judge whether the SELECTION was good: were any important, newsworthy stories dropped "
    "that should have been included? Were any low-value, minor, or redundant stories "
    "included that should have been cut?\n\n"
    "You have web_search and web_fetch tools available. Whenever you believe a story "
    "SHOULD have been featured, or that a featured story SHOULDN'T have made it, use "
    "those tools to check the sources/internet and sharpen that view BEFORE you commit "
    "to it -- don't rely on your own static familiarity with a topic to judge its "
    "editorial priority; confirm how significant/developed the story actually turned out "
    "to be. If, after checking, you conclude you would have selected differently, "
    "document it explicitly in the selection_disagreements array below -- do not just "
    "mention it in passing in the rationale.\n\n"
    "Score 1 (poor selection -- clear misses) to 5 (excellent selection -- the right "
    "stories were kept, the right ones were cut). " + JSON_RESPONSE_INSTRUCTION_WITH_SELECTION_DISAGREEMENTS
)


def judge_content_selection(client: Any, *, candidates_json: list[dict] | None, brief_markdown: str) -> JudgeResult:
    """Score content selection, or degrade to insufficient_data when no candidates
    artifact exists for this run (FR-6's documented degrade path -- unchanged by
    the v2 rework)."""
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
            model=_MODEL,
        )

    # Entry shapes vary with the model that wrote candidates.json (2026-07-08:
    # a real all-Haiku run emitted a list of plain STRINGS instead of the
    # {title, source, disposition} dicts the skill contract names -- .get() on a
    # str crashed the whole already-paid run at the judging step). Tolerate:
    # dicts use the contract fields; strings become bare titles; anything else
    # is stringified. Judging a degraded summary honestly beats crashing.
    def _summarize(entry) -> str:
        if isinstance(entry, dict):
            return f"- [{entry.get('disposition', 'unknown')}] {entry.get('title', '(untitled)')} ({entry.get('source', 'unknown source')})"
        return f"- [unknown] {entry} (unknown source)"

    candidates_summary = "\n".join(_summarize(c) for c in candidates_json)
    user_prompt = (
        f"CANDIDATES CONSIDERED DURING RESEARCH:\n{candidates_summary}\n\n"
        f"FINAL BRIEF:\n{brief_markdown}\n\n"
        "Judge the selection quality per your system instructions, using web_search/"
        "web_fetch to sharpen any case where you're inclined to disagree with a selection "
        "decision."
    )
    return run_judge(
        client,
        criterion=CRITERION,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=_MODEL,
        max_tokens=3072,
        tools=[_WEB_SEARCH_TOOL, _WEB_FETCH_TOOL],
    )


__all__ = ["judge_content_selection", "CRITERION"]
