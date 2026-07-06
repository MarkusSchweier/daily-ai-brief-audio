"""Cross-run brief-history persistence in S3, replacing the local `Daily AI Briefs/`
working folder that Managed Agents sessions cannot share (docs/adr/0005).

Each scheduled run starts in a fresh, empty microVM sandbox — there is no filesystem
carried over from prior runs. This module gives the ported research/writing skill
(deploy/managed-agent/skills/daily-ai-brief/SKILL.md) and the pipeline entrypoint a way
to (a) read the most recent PRIOR briefs (plural — see below) so research avoids
repeating recent stories and can correctly label genuine follow-ups, and (b) archive
today's produced brief durably once the run completes — both against the existing
`cowork-polly-tts-740353583786` bucket, under a new `briefs/` prefix (no new bucket,
per ADR-0005 / PRD AC-12).

Read-latest N, not date arithmetic: the prior briefs are resolved by listing `briefs/`
and taking the greatest `YYYY-MM-DD` keys strictly less than today's date, not by
computing `today - 1 day`, `today - 2 days`, etc. Because keys are zero-padded ISO
dates, lexicographic order is chronological order, so this is a cheap listing + slice
from the end — and it is what makes Mondays, holidays, and missed runs fall out for
free (ADR-0005): the "N most recent priors" are simply whichever briefs actually exist,
exactly like scanning a local folder for the last few dated files.

IAM: needs `s3:GetObject`/`s3:PutObject` on the bucket (already granted, unchanged) plus
`s3:ListBucket` on the bucket ARN scoped to the `briefs/*` prefix (the one addition
ADR-0005 requires; see deploy/managed-agent/cdk/managed_agent/stack.py's
`S3ListBriefsPrefix` statement). Credentials come from the ambient boto3 credential
chain (the microVM's IMDSv2-delivered execution role, ADR-0004) — this module never
sets credentials explicitly, exactly like deploy/audio_email.py.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

BUCKET = "cowork-polly-tts-740353583786"
BRIEFS_PREFIX = "briefs/"
# Filename for the durable MP3 pointer written alongside a day's archive (PRD
# instant-welcome-brief.md FR-1/AC-1). deploy/subscribers' welcome-send Lambda
# (layers/common/python/latest_brief.py) reads this same filename from a *different*
# CDK app/deploy unit -- it deliberately does not import this module (see that module's
# docstring) so this constant's name/value must stay in sync there by hand.
AUDIO_POINTER_FILENAME = "audio-pointer.json"
# Filename for the durable "candidates considered" research artifact (PRD
# docs/prd/eval-harness.md FR-4/AC-5, ADR-0013 §D). Written by the `daily-ai-brief`
# skill itself to WORKING_FOLDER as `candidates.json` -- this constant is the archived
# object's key *segment* under `briefs/<date>/`, matching the skill's own output
# filename so a caller passing the file's contents straight through needs no renaming.
# deploy/eval/'s judges (a different, standalone CDK app) read this same filename from
# S3 -- kept in sync by hand, same convention as AUDIO_POINTER_FILENAME above.
CANDIDATES_FILENAME = "candidates.json"
# Filename for the durable per-brief "source usage" record (PRD
# docs/prd/agent-system-redesign.md FR-8a, ADR-0014 -- realizes GitHub issue #28).
# Written by the `daily-ai-brief` skill itself to WORKING_FOLDER as `source-usage.json`
# -- a direct sibling of CANDIDATES_FILENAME above, same additive/best-effort pattern.
# This constant is the archived object's key *segment* under `briefs/<date>/`, matching
# the skill's own output filename so a caller passing the file's contents straight
# through needs no renaming.
SOURCE_USAGE_FILENAME = "source-usage.json"

# Matches `briefs/YYYY-MM-DD/` — the per-day folder key prefix ADR-0005 defines.
_DATED_PREFIX_RE = re.compile(r"^briefs/(\d{4}-\d{2}-\d{2})/$")


@dataclass(frozen=True)
class PriorBrief:
    """One prior brief found in the store."""

    date: str  # "YYYY-MM-DD"
    markdown: str


DEFAULT_RECENT_BRIEFS_COUNT = 3


def _list_dated_prefixes(s3_client, bucket: str = BUCKET) -> list[str]:
    """Return every `YYYY-MM-DD` date string that has a `briefs/<date>/` folder,
    sorted ascending. Uses a delimiter so this is a cheap one-level listing (folders
    only), not a scan of every object under the prefix.

    A listing failure (e.g. transient S3 error) is treated as "no prior briefs found"
    by the caller — reading yesterday's brief must degrade gracefully, never abort the
    run (ADR-0005: "must ensure the read tolerates an empty listing").
    """
    dates: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=BRIEFS_PREFIX, Delimiter="/"):
        for common_prefix in page.get("CommonPrefixes", []):
            match = _DATED_PREFIX_RE.match(common_prefix.get("Prefix", ""))
            if match:
                dates.append(match.group(1))
    return sorted(dates)


def read_recent_prior_briefs(
    s3_client, today: str, count: int = DEFAULT_RECENT_BRIEFS_COUNT, bucket: str = BUCKET
) -> list[PriorBrief]:
    """Read up to `count` most recent briefs strictly before `today` (an ISO
    "YYYY-MM-DD" string), most recent first.

    Returns an empty list if none exist, and returns fewer than `count` whenever fewer
    exist (a first-ever run, an early run with a young store, weekends/holidays that
    just don't have that many priors yet) — this always degrades to whatever was
    actually found rather than raising, so the research step can proceed with partial
    or no "avoid-repeats" input, exactly as a first-ever local run would (ADR-0005,
    PRD AC-5's edge cases: weekends, holidays, missed runs, and cold start). A read
    failure on any individual date is logged and that date is skipped, not treated as
    a reason to abort reading the others.
    """
    try:
        dates = [d for d in _list_dated_prefixes(s3_client, bucket) if d < today]
    except Exception as e:
        print("BRIEF_HISTORY_LIST_FAILED:", repr(e))
        return []
    if not dates:
        return []

    # Lexicographic order == chronological order for zero-padded ISO dates, so the
    # last `count` entries are the `count` most recent; reversed to most-recent-first.
    recent_dates = list(reversed(dates[-count:]))

    results: list[PriorBrief] = []
    for date in recent_dates:
        key = f"{BRIEFS_PREFIX}{date}/brief.md"
        try:
            obj = s3_client.get_object(Bucket=bucket, Key=key)
            markdown = obj["Body"].read().decode("utf-8")
        except Exception as e:
            print("BRIEF_HISTORY_READ_FAILED:", key, repr(e))
            continue
        results.append(PriorBrief(date=date, markdown=markdown))

    return results


def archive_todays_brief(
    s3_client,
    today: str,
    *,
    markdown: str,
    html: str | None = None,
    listening_script: str | None = None,
    audio_key: str | None = None,
    bucket: str = BUCKET,
) -> None:
    """Archive today's produced brief under `briefs/<today>/`, so tomorrow's run can
    read it back and the owner has a durable record (ADR-0005, PRD FR-9/AC-6).

    The Markdown is the canonical archived artifact and is always written. HTML and the
    listening script are written alongside it when provided, so a day is a
    self-contained folder easy to inspect and diff against the local task's output
    during the parallel-run validation window (ADR-0005, ADR-0007). Archiving is
    best-effort per PRD's "never lose the brief over a glitch" fail-safe philosophy: a
    write failure is logged, not raised, so it can never block the audio/email send that
    already has the brief in hand.

    `audio_key`, when given, is that run's actual Polly `OutputUri`-derived `audio/…` S3
    key (never a reconstructed one -- CLAUDE.md's "use OutputUri, never build the S3 key"
    invariant) and is written as a small durable pointer object,
    `briefs/<today>/audio-pointer.json`, so a later reader (the welcome-send Lambda, PRD
    instant-welcome-brief.md FR-1/FR-2) can find the MP3 without reconstructing the key.
    Callers pass `audio_key=None` on an audio-failure day (PRD AC-2) so no pointer is
    written then -- the read side (FR-2) treats a missing pointer as "brief, no audio",
    not an error. The pointer write is its own best-effort step, isolated from the
    brief/html/script writes above: a pointer-write failure must not affect (or be
    affected by) whether those succeeded.
    """
    objects = {f"{BRIEFS_PREFIX}{today}/brief.md": markdown}
    if html is not None:
        objects[f"{BRIEFS_PREFIX}{today}/brief.html"] = html
    if listening_script is not None:
        objects[f"{BRIEFS_PREFIX}{today}/listening-script.txt"] = listening_script

    for key, body in objects.items():
        try:
            s3_client.put_object(
                Bucket=bucket, Key=key, Body=body.encode("utf-8"), ContentType="text/plain; charset=utf-8"
            )
            print("BRIEF_ARCHIVED", key)
        except Exception as e:
            print("BRIEF_ARCHIVE_FAILED:", key, repr(e))

    if audio_key is not None:
        pointer_key = f"{BRIEFS_PREFIX}{today}/{AUDIO_POINTER_FILENAME}"
        try:
            s3_client.put_object(
                Bucket=bucket,
                Key=pointer_key,
                Body=json.dumps({"audio_key": audio_key}).encode("utf-8"),
                ContentType="application/json",
            )
            print("BRIEF_ARCHIVED", pointer_key)
        except Exception as e:
            print("BRIEF_ARCHIVE_FAILED:", pointer_key, repr(e))


def archive_candidates_file(
    s3_client,
    today: str,
    *,
    working_folder: str,
    bucket: str = BUCKET,
    candidates_filename: str = CANDIDATES_FILENAME,
) -> bool:
    """Archive the skill's `candidates.json` (PRD docs/prd/eval-harness.md FR-4,
    ADR-0013 §D) to `briefs/<today>/candidates.json`, if the skill wrote one this run.

    Additive and best-effort, mirroring `archive_todays_brief`'s own fail-safe
    philosophy (CLAUDE.md: never lose the brief over a glitch):
      - A missing file (an older run, or a run before this feature shipped, or a run
        whose skill version hasn't yet had the candidates instruction pushed live --
        ADR-0008's lockstep push is a separate, later step from this wrapper change)
        is the expected common case, not an error -- logged and skipped, never raised.
      - A read or S3-write failure is logged, never raised -- archiving this artifact
        must never fail (or even slow) the run that already has the brief in hand.

    Returns True if the artifact was archived, False otherwise (missing file or a
    failure) -- purely informational for the caller's own logging, never meant to gate
    anything downstream.
    """
    local_path = os.path.join(working_folder, candidates_filename)
    if not os.path.exists(local_path):
        print("CANDIDATES_ARCHIVE_SKIPPED: no candidates.json found at", local_path)
        return False

    try:
        with open(local_path, encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        print("CANDIDATES_ARCHIVE_READ_FAILED:", local_path, repr(e))
        return False

    key = f"{BRIEFS_PREFIX}{today}/{candidates_filename}"
    try:
        s3_client.put_object(
            Bucket=bucket, Key=key, Body=raw.encode("utf-8"), ContentType="application/json"
        )
        print("BRIEF_ARCHIVED", key)
        return True
    except Exception as e:
        print("BRIEF_ARCHIVE_FAILED:", key, repr(e))
        return False


def archive_source_usage_file(
    s3_client,
    today: str,
    *,
    working_folder: str,
    bucket: str = BUCKET,
    source_usage_filename: str = SOURCE_USAGE_FILENAME,
) -> bool:
    """Archive the skill's `source-usage.json` (PRD docs/prd/agent-system-redesign.md
    FR-8a, ADR-0014 -- realizes GitHub issue #28) to
    `briefs/<today>/source-usage.json`, if the skill wrote one this run.

    A direct sibling of `archive_candidates_file()` above -- same additive,
    best-effort philosophy (CLAUDE.md: never lose the brief over a glitch):
      - A missing file (an older run, a run before this feature shipped, or a run
        whose skill version hasn't yet had the source-usage instruction pushed live --
        ADR-0008's lockstep push is a separate, later step from this wrapper change)
        is the expected common case, not an error -- logged and skipped, never raised.
      - A read or S3-write failure is logged, never raised -- archiving this artifact
        must never fail (or even slow) the run that already has the brief in hand.

    Returns True if the artifact was archived, False otherwise (missing file or a
    failure) -- purely informational for the caller's own logging, never meant to gate
    anything downstream.
    """
    local_path = os.path.join(working_folder, source_usage_filename)
    if not os.path.exists(local_path):
        print("SOURCE_USAGE_ARCHIVE_SKIPPED: no source-usage.json found at", local_path)
        return False

    try:
        with open(local_path, encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        print("SOURCE_USAGE_ARCHIVE_READ_FAILED:", local_path, repr(e))
        return False

    key = f"{BRIEFS_PREFIX}{today}/{source_usage_filename}"
    try:
        s3_client.put_object(
            Bucket=bucket, Key=key, Body=raw.encode("utf-8"), ContentType="application/json"
        )
        print("BRIEF_ARCHIVED", key)
        return True
    except Exception as e:
        print("BRIEF_ARCHIVE_FAILED:", key, repr(e))
        return False
