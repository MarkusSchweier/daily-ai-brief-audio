"""Dedup judge -- FEED FIX + richer assessment, judge methodology v2 (2026-07-07,
owner-directed, docs/adr/0016 amendment).

## Why this was reworked (the second live-run finding that motivated it)

A real committed run exposed structural contamination in the priors this judge was
handed: `deploy/eval-harness/runs/multiagent-aggressive-haiku/2026-07-07-174852-
aecc7c-harness-validation-multiagent/repetitions/01/scores.json`'s `dedup` entry
was scored against priors that included the SAME DAY's production brief -- the
delivery endpoint's own `GET /recent-briefs` route excludes only ITS OWN wall-
clock "today" (`_today_local_date()` at request time), which is NOT necessarily
the same as the date the eval candidate's OWN generated brief carries. A "prior"
that is actually the brief-under-test's own day is not a prior at all -- comparing
against it structurally cannot mean anything.

## What changed

1. **FEED FIX, in the harness, not the judge** (`harness/dedup_priors.py`): priors
   are now filtered STRICTLY relative to the brief actually being evaluated (its
   own date, parsed from its artifact filename by `run.py`), not the delivery
   endpoint's wall-clock "today" -- see that module's docstring for the exact
   mechanism (over-fetch + local re-filter, same-or-future dates dropped, one
   entry per date, capped at the requested count).
2. **Each prior's DATE is told to the judge explicitly** in the prompt (was
   previously just a list of undated markdown bodies) -- the judge can no longer
   even in principle mix up which edition is "prior."
3. **Richer, structured assessment.** The judge now documents THREE things per
   duplication, not just a single score/rationale: (a) is this a duplication at
   all vs. a specific prior date; (b) IS it labelled as such in today's brief (a
   "follow-up"/"update" framing); (c) IS that follow-up justified by substantial
   new data/findings, or just a rehash. A structured `findings` array
   (`{story, duplicate_of_date, labelled_as_followup, justified, note}`) makes
   this explicit and reviewable rather than folded into prose.

No web tools needed here (owner spec) -- this judge compares two pieces of TEXT
it already has (today's brief + the dated priors), not a claim needing external
verification.

Model moves to **Opus 4.8** per judge methodology v2's owner-directed "judges must
be stronger than what they judge" principle (`base.JUDGE_MODELS`) -- the owner's
original spec said "model stays Haiku," but a later, binding direction moved ALL
FOUR judges to Opus uniformly; see `base.py`'s module docstring and the ADR-0016
amendment for the full rationale.

PORTED base structure (ADR-0016 Phase 1, 2026-07-07) from
`deploy/eval/eval_core/judges/dedup.py`.
"""

from __future__ import annotations

from typing import Any

from .base import JSON_RESPONSE_INSTRUCTION_WITH_DEDUP_FINDINGS, JUDGE_MODELS, JudgeResult, run_judge

CRITERION = "dedup"

_MODEL = JUDGE_MODELS[CRITERION]

_SYSTEM_PROMPT = (
    "You are a repetition-detection judge for a daily AI news brief. You will be given "
    "today's brief and one or more recent PRIOR editions, each labelled with its own "
    "DATE. Judge whether today's brief repeats a story from a prior edition WITHOUT it "
    "being a genuine, clearly-labeled follow-up.\n\n"
    "For EACH potential duplication you find, assess and document THREE things: (a) is "
    "this actually a duplicate of a specific prior edition (name its date); (b) IS it "
    "labelled as such in today's brief (a 'follow-up', 'update:', or similar framing) -- "
    "or does today's brief present it as if it were new; (c) IS the follow-up justified "
    "by substantial new data/findings (e.g. 'update: X now confirms Y'), or is it just a "
    "bare rehash of the same story with no new information. A follow-up that is BOTH "
    "labelled AND justified is fine and expected, not a dedup failure; an UNLABELLED "
    "repeat, or a labelled 'follow-up' that adds nothing new, IS a dedup failure.\n\n"
    "Score 1 (repeats a prior story with no new information and/or no label) to 5 (no "
    "unlabeled or unjustified repetition -- every story is either new or a genuine, "
    "clearly-flagged, substantively-updated follow-up). If no prior briefs were available "
    "to compare against, say so and treat dedup as inapplicable via insufficient_data. "
    + JSON_RESPONSE_INSTRUCTION_WITH_DEDUP_FINDINGS
)


def judge_dedup(client: Any, *, brief_markdown: str, priors: list[dict[str, str]]) -> JudgeResult:
    """`priors` is a list of `{"date": "YYYY-MM-DD", "markdown": "..."}` dicts --
    per the v2 feed fix, the caller (`run.py`, via
    `harness.dedup_priors.fetch_recent_prior_briefs()`) is responsible for
    resolving these ALREADY FILTERED strictly relative to the brief actually
    being evaluated (this module does not itself call the delivery endpoint, and
    does not itself re-check date ordering -- it trusts the caller's feed and
    only formats each entry's date into the prompt so the judge can reason about
    it explicitly)."""
    if not priors:
        return JudgeResult(
            criterion=CRITERION,
            score=None,
            rationale="No prior briefs were available to compare against (e.g. the first-ever run) -- dedup is not applicable.",
            evidence="",
            insufficient_data=True,
            model=_MODEL,
        )

    priors_section = "\n\n---\n\n".join(f"PRIOR EDITION ({p['date']}):\n{p['markdown']}" for p in priors)
    user_prompt = (
        f"TODAY'S BRIEF:\n{brief_markdown}\n\n"
        f"RECENT PRIOR EDITIONS (each dated):\n{priors_section}\n\n"
        "Judge day-over-day dedup per your system instructions."
    )
    return run_judge(
        client,
        criterion=CRITERION,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=_MODEL,
        max_tokens=2048,
    )


__all__ = ["judge_dedup", "CRITERION"]
