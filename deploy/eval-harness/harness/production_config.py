"""Which candidate declaration mirrors the configuration **production runs today**
-- the ONE place `run.py` (recording a new run) and `ui.py` (labelling the Conduct
dropdown) both read, so the two can never drift apart (review-fix, reviewer M4).

## Today's answer: `haiku-5-5-hardened`

Production's configuration has had three eras, each mirrored by one candidate:

  1. **until 2026-07-13** -- `production-baseline` (Claude Sonnet 5, end-to-end
     single agent).
  2. **2026-07-13 -> 2026-10-11** -- `haiku-swap-hardened` (Claude Haiku 4.5 + the
     checklist-hardened task prompt), the owner's move onto candidate #6 ahead of
     the cost-optimization epic's step C: live agent v2, scheduled deployment
     `depl_016LWhcSEghrvGknVWhZvgdr`, first Haiku production send Tue 2026-07-14
     06:07 Europe/Berlin. See `docs/prd/cost-optimization-candidates.md` §6's first
     DECIDED entry.
  3. **since 2026-10-11** -- `haiku-5-5-hardened` (Claude **Haiku 5.5 at effort
     high**, the hardened task prompt UNCHANGED from era 2), i.e. a pure
     model-generation move: the scheduled production deployment now runs exactly
     this candidate's configuration.

The superseded candidates stay perfectly valid declarations -- they are how the
step-C comparison keeps measuring the older configurations (Sonnet 5 via
`production-baseline`, Haiku 4.5 via `haiku-swap-hardened`) -- they are just no
longer what production runs.

## The recorded flag is HISTORICAL, and is never rewritten

`eval-run.json`'s `is_production_config` means **"this candidate WAS the production
configuration at the time of this run"**, not "is the production configuration
now". It is resolved once, by `run.py`, when the run record is written, and then
belongs to that record forever:

  - `production-baseline`'s July run records legitimately keep `true` -- when they
    were taken, that genuinely was production's configuration. Rewriting them to
    `false` would falsify the comparison table's own history.
  - `haiku-swap-hardened`'s `2026-10-11-104933-dce776-matched-5-5-2026-10-11` record
    legitimately keeps `true`: it ran a few hours BEFORE that day's switch, while
    Haiku 4.5 was still production. Its July record (taken before 2026-07-13)
    legitimately keeps `false` for the same reason.
  - Today's `haiku-5-5-hardened` records legitimately keep `false` -- they were
    taken while it was still only a candidate.
  - The Assess table renders the STORED flag (`EvalRunMeta.is_production_config`)
    and never recomputes it, which is what makes the above hold.

A candidate may also state the answer for itself: `candidate.json`'s optional
`is_production_config` field wins over the slug below, so promoting a different
candidate needs no code change at all (`PRODUCTION_CANDIDATE_SLUG` here is the
fallback for the normal case where no candidate declares anything).
"""

from __future__ import annotations

from typing import Any

# The candidate slug this repo treats as "the configuration production runs today"
# (ADR-0016 D4's production-config marker). History: `production-baseline` until
# 2026-07-13, `haiku-swap-hardened` from then until 2026-10-11, and
# `haiku-5-5-hardened` since the owner's 2026-10-11 move to Haiku 5.5 at effort
# high -- see the module docstring. Changing this NEVER rewrites an existing run
# record; only records written from here on resolve to the new answer.
PRODUCTION_CANDIDATE_SLUG = "haiku-5-5-hardened"


def is_production_config(slug: str, candidate_json: dict[str, Any] | None = None) -> bool:
    """Is candidate `slug` the configuration production runs **right now**?

    An explicit `is_production_config` in the candidate's own `candidate.json`
    wins (so a future production promotion is a one-field declaration edit, not a
    code change); otherwise the answer is "it is the slug named above".

    Callers that record this into a run record must do so ONCE, at record-write
    time -- the stored value is historical and must not be recomputed later (see
    the module docstring).
    """
    candidate_json = candidate_json or {}
    if "is_production_config" in candidate_json:
        return bool(candidate_json["is_production_config"])
    return slug == PRODUCTION_CANDIDATE_SLUG


__all__ = ["PRODUCTION_CANDIDATE_SLUG", "is_production_config"]
