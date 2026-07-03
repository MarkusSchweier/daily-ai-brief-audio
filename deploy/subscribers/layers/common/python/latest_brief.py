"""Read-only helper: resolve the single most recently archived brief, for the
welcome-send Lambda (PRD docs/prd/instant-welcome-brief.md FR-2, AC-3).

Deliberately NOT an import of `deploy/managed-agent/pipeline/brief_history.py`: that
module lives in a different CDK app/deploy unit (the Managed Agents pipeline) than this
Lambda's own app (`deploy/subscribers/`), and the PRD (§6 "Constraints & dependencies")
calls out that cross-stack boundary explicitly. A same-account, cross-CDK-app Python
import would couple two independently packaged/deployed units at build time for the sake
of ~20 lines of S3-listing logic, so this module duplicates that logic instead. The
constants below (bucket name, the `briefs/` prefix, the `audio-pointer.json` filename)
MUST stay in sync with `brief_history.py`'s -- there is no automated cross-check for this
(the same pragmatic limitation the PRD accepts for the send-time/schedule consistency
check, FR-12); a future maintainer changing one must change the other by hand.

Unlike `brief_history.read_recent_prior_briefs` (Markdown, most-recent-N, for the
research step to avoid repeating stories), this resolves exactly ONE brief's rendered
HTML plus its resolved audio pointer -- the artifact and shape the welcome email needs,
not the research-oriented one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

BUCKET = "cowork-polly-tts-740353583786"
BRIEFS_PREFIX = "briefs/"
# Must match deploy/managed-agent/pipeline/brief_history.AUDIO_POINTER_FILENAME.
AUDIO_POINTER_FILENAME = "audio-pointer.json"

# Matches `briefs/YYYY-MM-DD/` -- same per-day folder key shape brief_history.py defines.
_DATED_PREFIX_RE = re.compile(r"^briefs/(\d{4}-\d{2}-\d{2})/$")


@dataclass(frozen=True)
class LatestBrief:
    """Result of resolving the most recently archived brief -- an explicit,
    non-exceptional model of both degrade paths FR-2/AC-3 require:

    - `found=False` (all other fields None): no brief has ever been archived (the
      cold-start case, PRD FR-8) -- an empty store, not an error.
    - `found=True`, `audio_key=None`: a brief exists but has no audio pointer (that
      day's audio synthesis failed, or the day predates this pointer feature) -- also
      not an error; the written brief is still usable (AC-2/AC-5).
    """

    found: bool
    date: str | None = None
    html: str | None = None
    audio_key: str | None = None


def _latest_dated_prefix(s3_client, bucket: str) -> str | None:
    """Return the single most recent `YYYY-MM-DD` with a `briefs/<date>/` folder, or
    None if the store is empty. A cheap one-level, delimiter-based listing (folders
    only) -- same technique as brief_history._list_dated_prefixes, but this helper only
    ever needs the single latest date, not the full sorted list."""
    latest: str | None = None
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=BRIEFS_PREFIX, Delimiter="/"):
        for common_prefix in page.get("CommonPrefixes", []):
            match = _DATED_PREFIX_RE.match(common_prefix.get("Prefix", ""))
            if match and (latest is None or match.group(1) > latest):
                latest = match.group(1)
    return latest


def resolve_latest_brief(s3_client, bucket: str = BUCKET) -> LatestBrief:
    """Resolve the most recently archived brief's HTML and (if present) its audio
    pointer's S3 key.

    Degrades gracefully at every step (FR-2): an empty store, a listing failure, or a
    read failure on the found date's `brief.html` all resolve to `found=False` -- the
    same "no brief" welcome-only path the cold-start case uses -- rather than raising;
    the welcome send must never fail because of this helper. A missing or unreadable
    audio pointer resolves to `audio_key=None`, NOT `found=False` -- the brief content
    is still usable with no audio pointer (see AC-2/AC-5). This function never verifies
    that the pointed-to MP3 object still exists (the 7-day `audio/` lifecycle can have
    expired it) -- that check is the caller's job (welcome-send handler.py), not this
    read-only resolver's.
    """
    try:
        date = _latest_dated_prefix(s3_client, bucket)
    except Exception as e:
        print("LATEST_BRIEF_LIST_FAILED:", repr(e))
        return LatestBrief(found=False)

    if date is None:
        return LatestBrief(found=False)

    html_key = f"{BRIEFS_PREFIX}{date}/brief.html"
    try:
        html_obj = s3_client.get_object(Bucket=bucket, Key=html_key)
        html = html_obj["Body"].read().decode("utf-8")
    except Exception as e:
        print("LATEST_BRIEF_READ_FAILED:", html_key, repr(e))
        return LatestBrief(found=False)

    audio_key: str | None = None
    pointer_key = f"{BRIEFS_PREFIX}{date}/{AUDIO_POINTER_FILENAME}"
    try:
        pointer_obj = s3_client.get_object(Bucket=bucket, Key=pointer_key)
        pointer = json.loads(pointer_obj["Body"].read().decode("utf-8"))
        audio_key = pointer.get("audio_key")
    except Exception as e:
        # An absent pointer (no audio that day, or a day archived before this feature
        # shipped) is the expected common case, not a failure worth alarming on.
        print("LATEST_BRIEF_POINTER_NOT_FOUND:", pointer_key, repr(e))

    return LatestBrief(found=True, date=date, html=html, audio_key=audio_key)
