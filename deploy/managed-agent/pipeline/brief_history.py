"""Cross-run brief-history persistence in S3, replacing the local `Daily AI Briefs/`
working folder that Managed Agents sessions cannot share (docs/adr/0005).

Each scheduled run starts in a fresh, empty microVM sandbox — there is no filesystem
carried over from yesterday's run. This module gives the ported research/writing skill
(deploy/managed-agent/skills/daily-ai-brief/SKILL.md) and the pipeline entrypoint a way
to (a) read the single most recent PRIOR day's brief so research avoids repeating
yesterday's stories, and (b) archive today's produced brief durably once the run
completes — both against the existing `cowork-polly-tts-740353583786` bucket, under a
new `briefs/` prefix (no new bucket, per ADR-0005 / PRD AC-12).

Read-latest, not date arithmetic: "yesterday" is resolved by listing `briefs/` and
taking the greatest `YYYY-MM-DD` key strictly less than today's date, not by computing
`today - 1 day`. Because keys are zero-padded ISO dates, lexicographic order is
chronological order, so this is a cheap listing + max — and it is what makes Mondays,
holidays, and missed runs fall out for free (ADR-0005): "yesterday" is simply the most
recent brief that actually exists, exactly like reading the local folder today.

IAM: needs `s3:GetObject`/`s3:PutObject` on the bucket (already granted, unchanged) plus
`s3:ListBucket` on the bucket ARN scoped to the `briefs/*` prefix (the one addition
ADR-0005 requires; see deploy/managed-agent/cdk/managed_agent/stack.py's
`S3ListBriefsPrefix` statement). Credentials come from the ambient boto3 credential
chain (the microVM's IMDSv2-delivered execution role, ADR-0004) — this module never
sets credentials explicitly, exactly like deploy/audio_email.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BUCKET = "cowork-polly-tts-740353583786"
BRIEFS_PREFIX = "briefs/"

# Matches `briefs/YYYY-MM-DD/` — the per-day folder key prefix ADR-0005 defines.
_DATED_PREFIX_RE = re.compile(r"^briefs/(\d{4}-\d{2}-\d{2})/$")


@dataclass(frozen=True)
class PriorBrief:
    """The most recent prior brief found in the store."""

    date: str  # "YYYY-MM-DD"
    markdown: str


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


def read_most_recent_prior_brief(s3_client, today: str, bucket: str = BUCKET) -> PriorBrief | None:
    """Read the most recent brief strictly before `today` (an ISO "YYYY-MM-DD" string).

    Returns None if no such brief exists — the first-ever run (empty store) and a
    listing/read error both degrade to None rather than raising, so the research step
    can proceed with no "avoid-repeats" input, exactly as a first-ever local run would
    (ADR-0005, PRD AC-5's edge cases: weekends, holidays, missed runs, and cold start).
    """
    try:
        dates = [d for d in _list_dated_prefixes(s3_client, bucket) if d < today]
    except Exception as e:
        print("BRIEF_HISTORY_LIST_FAILED:", repr(e))
        return None
    if not dates:
        return None

    most_recent = dates[-1]  # lexicographic max == chronological max (ISO dates)
    key = f"{BRIEFS_PREFIX}{most_recent}/brief.md"
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        markdown = obj["Body"].read().decode("utf-8")
    except Exception as e:
        print("BRIEF_HISTORY_READ_FAILED:", key, repr(e))
        return None

    return PriorBrief(date=most_recent, markdown=markdown)


def archive_todays_brief(
    s3_client,
    today: str,
    *,
    markdown: str,
    html: str | None = None,
    listening_script: str | None = None,
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
