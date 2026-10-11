"""Fetch recent prior briefs for the dedup judge (ADR-0016 cross-cutting section:
"the dedup judge's prior briefs input comes from the same GET /recent-briefs route
the candidates already fetch at run time... no S3").

Pure local, no AWS call: mints its OWN short-lived signed read-capability token via
the same `candidate_sync.recent_briefs_token.generate()` scheme candidates use for
the `__RECENT_BRIEFS_TOKEN__` task-prompt placeholder, and reads
`$RECENT_BRIEFS_SIGNING_KEY` / `$DELIVERY_BASE_URL` from the local environment --
never an AWS call, preserving this harness's "pure local tool, no AWS" property
(ADR-0016 D1).

Degrades SOFTLY (returns an empty list, never raises) when the env vars are unset
or the HTTP call fails -- dedup is one criterion among a SELECTABLE subset (PRD
§4.1: "which eval criteria... a subset"), not a hard requirement of every eval run.
A caller that selected "dedup" will see the judge itself report
`insufficient_data=True` (its own documented degrade path, see
`eval_core/judges/dedup.py`) rather than the whole eval run failing over a
prior-briefs fetch glitch. A genuine HTTP/transport failure (as opposed to the
env vars simply being unset) prints a `DEDUP_PRIORS_FETCH_FAILED:` diagnostic to
stderr (review-fix: reviewer Low -- "silent degradation... undiagnosable today")
so an operator can tell "no priors configured" apart from "the fetch broke."

## FEED FIX (judge methodology v2, 2026-07-07, owner-directed, docs/adr/0016
amendment)

A real committed run exposed structural contamination: the dedup judge was handed
a "prior" that was actually the SAME-DAY production brief. The root cause is that
`GET /recent-briefs` filters against the DELIVERY LAMBDA'S OWN wall-clock "today"
(`_today_local_date()` at REQUEST time, see `deploy/delivery/functions/deliver/
handler.py`'s `_handle_recent_briefs()`), not against the date of whichever brief
this harness is actually evaluating. On an ordinary eval run those two "today"s
usually agree (the harness typically triggers on the same calendar day the
candidate's own brief is dated) -- but nothing guarantees it, and the endpoint has
no parameter to say "filter relative to THIS brief's date" (nor should it grow
one just for this -- it is deliberately a thin, stateless, wall-clock read used by
production candidates too).

So the fix lives HERE, in the harness, not the judge and not the delivery
endpoint: `fetch_recent_prior_briefs()` (renamed from the v1
`fetch_recent_prior_briefs_markdown()`, whose return shape changes too -- see
below) takes the eval brief's OWN date explicitly (`brief_date`, parsed by the
caller, `run.py`, from the brief's artifact filename) and:

1. Over-fetches `count + _OVER_FETCH_MARGIN` entries from `GET /recent-briefs`
   (a cheap, harmless margin -- the endpoint already caps at
   `MAX_RECENT_BRIEFS_COUNT`, so this can never explode).
2. Drops any entry whose own `date` is the SAME AS OR AFTER `brief_date` (a
   same-day or future entry is never a "prior" of the brief under test, no
   matter what the delivery endpoint's own wall-clock filter did or didn't
   exclude).
3. Dedupes by `date`, keeping the first (most-recent, since the endpoint already
   returns most-recent-first) occurrence per date -- defensive; the endpoint
   should never emit two entries for one date, but this guarantees "one brief
   per prior day" regardless.
4. Caps the result at `count`.

The return shape also changes from `list[str]` (bare markdown bodies) to
`list[dict[str, str]]` (`{"date": ..., "markdown": ...}`) -- the v2 dedup judge
needs each prior's date told to it explicitly in the prompt (so it can document
`duplicate_of_date` in its structured findings), not just an undated blob of text.

## The UNFILTERED read (2026-10-11)

`fetch_recent_briefs_unfiltered()` is a second, deliberately different read of the
SAME endpoint: the raw last-N entries, with no `brief_date` filter and no
one-per-day dedupe, and -- unlike everything above -- it RAISES
(`RecentBriefsUnavailableError`) instead of degrading to `[]`. It exists for
`run.py`'s captured-brief guard, which must be able to tell "this artifact IS a
feed brief" apart from "I couldn't ask the feed": the first means a repetition
captured a PRIOR brief instead of the candidate's own output (observed live
2026-10-11: a mangled final `cat ... | head -100` left only the priors the agent
had read, and the judges scored Friday's production brief as the candidate's
work), the second must only warn.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx
from candidate_sync import recent_briefs_token

RECENT_BRIEFS_SIGNING_KEY_ENV_VAR = "RECENT_BRIEFS_SIGNING_KEY"
DELIVERY_BASE_URL_ENV_VAR = "DELIVERY_BASE_URL"

DEFAULT_TTL_SECONDS = 5 * 60
DEFAULT_COUNT = 3

# How many feed entries `fetch_recent_briefs_unfiltered()` reads by default --
# enough to cover any prior brief a candidate plausibly read during its mandatory
# prior-reading step (the hardened prompts read the last few days), small enough
# to stay a cheap single call.
DEFAULT_UNFILTERED_COUNT = 5

# How many EXTRA entries to over-fetch beyond `count`, so that dropping any
# same-or-future-dated entries (relative to `brief_date`) still leaves enough
# genuine priors to satisfy `count` where possible. Owner spec: "e.g. a count+2
# fetch."
_OVER_FETCH_MARGIN = 2


def _build_http_client() -> Any:
    """The real `httpx.Client` used when a caller injects none -- its own tiny
    factory function SPECIFICALLY so the test suite can monkeypatch exactly this
    seam (see `tests/conftest.py`'s autouse no-network guard) instead of the shared
    `httpx` module object every other module imports too. Mirrors `ui.py`'s
    `_launch_run_subprocess()`, factored out for the same reason."""
    return httpx.Client(timeout=30.0)


class RecentBriefsUnavailableError(RuntimeError):
    """The `GET /recent-briefs` read could not be performed at all -- no signing
    key / base URL configured, or a transport/HTTP/shape failure.

    Raised ONLY by `fetch_recent_briefs_unfiltered()` (2026-10-11), whose caller
    (`run.py`'s captured-brief guard) MUST be able to tell "the feed says this
    artifact is a prior brief" apart from "I could not ask the feed" -- the first
    is a fail-loud condition, the second must not block a run.
    `fetch_recent_prior_briefs()` keeps its original soft degrade (returns `[]`)
    because for the dedup judge "no priors" is a legitimate answer."""


def _get_recent_briefs(
    *,
    fetch_count: int,
    signing_key: str,
    delivery_base_url: str,
    ttl_seconds: int,
    client: Any,
) -> list[dict[str, str]]:
    """The raw `GET /recent-briefs?count=<fetch_count>` read, RAISING
    `RecentBriefsUnavailableError` on any failure -- the shared core of both public
    fetch functions (one wraps it in a soft degrade, the other lets it raise)."""
    token = recent_briefs_token.generate(signing_key, ttl_seconds=ttl_seconds)
    url = f"{delivery_base_url.rstrip('/')}/recent-briefs?count={fetch_count}"
    headers = {"Authorization": f"Bearer {token}"}

    owns_client = client is None
    http_client = client or _build_http_client()
    try:
        response = http_client.get(url, headers=headers)
        response.raise_for_status()
        body = response.json()
    except Exception as e:  # noqa: BLE001 - normalized into ONE typed error for both callers
        raise RecentBriefsUnavailableError(f"GET /recent-briefs failed: {e!r}") from e
    finally:
        if owns_client:
            http_client.close()

    briefs = (body or {}).get("briefs", [])
    return [
        {"date": b["date"], "markdown": b["markdown"]}
        for b in briefs
        if isinstance(b, dict) and b.get("markdown") and b.get("date")
    ]


def _fetch_raw(
    *,
    fetch_count: int,
    signing_key: str,
    delivery_base_url: str,
    ttl_seconds: int,
    client: Any,
) -> list[dict[str, str]]:
    """The unfiltered `GET /recent-briefs?count=<fetch_count>` read -- returns the
    raw `[{"date": ..., "markdown": ...}, ...]` list exactly as the endpoint sent
    it (most-recent-first), or `[]` on any transport/HTTP failure (see module
    docstring on soft degrade). Factored out from `fetch_recent_prior_briefs()` so
    the over-fetch-then-locally-filter split (the v2 feed fix) is a single,
    obvious seam."""
    try:
        return _get_recent_briefs(
            fetch_count=fetch_count,
            signing_key=signing_key,
            delivery_base_url=delivery_base_url,
            ttl_seconds=ttl_seconds,
            client=client,
        )
    except RecentBriefsUnavailableError as e:
        # review-fix (reviewer Low): the silent degrade-to-empty-list above was
        # UNDIAGNOSABLE -- a real GET /recent-briefs failure (bad signing key,
        # DELIVERY_BASE_URL unreachable, an unexpected response shape) looked
        # IDENTICAL to "no prior briefs exist yet" from the caller's side. Still
        # never aborts the run (dedup just degrades to insufficient_data, per
        # this module's own documented contract), but now leaves a clear,
        # greppable trace of WHY.
        print(f"DEDUP_PRIORS_FETCH_FAILED: {e.__cause__!r}", file=sys.stderr)
        return []


def fetch_recent_briefs_unfiltered(
    *,
    count: int = DEFAULT_UNFILTERED_COUNT,
    signing_key: str | None = None,
    delivery_base_url: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    client: Any = None,
) -> list[dict[str, str]]:
    """The last `count` entries of the delivery feed, EXACTLY as the endpoint sent
    them -- no `brief_date` filter, no one-per-day dedupe (2026-10-11).

    Added for `run.py`'s captured-brief guard: a repetition whose final `cat` was
    mangled (e.g. `cat "..." | head -100`) can leave the harness holding ONLY the
    prior briefs the agent read during its mandatory prior-reading step, and those
    were then judged as if the candidate had written them. The guard compares the
    selected brief's CONTENT against this feed, so a date rule (which cannot help
    when the feed already carries a same-dated production brief) is not relied on.

    Raises `RecentBriefsUnavailableError` -- never degrades to `[]` -- when the
    read cannot be performed, so the caller can distinguish "not a prior" from
    "could not check" and warn instead of blocking."""
    if signing_key is None:
        signing_key = os.environ.get(RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, "")
    if delivery_base_url is None:
        delivery_base_url = os.environ.get(DELIVERY_BASE_URL_ENV_VAR, "")

    if not signing_key or not delivery_base_url:
        raise RecentBriefsUnavailableError(
            "the recent-briefs feed is not configured locally "
            f"(${RECENT_BRIEFS_SIGNING_KEY_ENV_VAR} / ${DELIVERY_BASE_URL_ENV_VAR} or their "
            "local-config equivalents are unset)"
        )

    return _get_recent_briefs(
        fetch_count=count,
        signing_key=signing_key,
        delivery_base_url=delivery_base_url,
        ttl_seconds=ttl_seconds,
        client=client,
    )


def fetch_recent_prior_briefs(
    *,
    brief_date: str,
    count: int = DEFAULT_COUNT,
    signing_key: str | None = None,
    delivery_base_url: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    client: Any = None,
) -> list[dict[str, str]]:
    """Return up to `count` prior briefs STRICTLY BEFORE `brief_date` (the eval
    brief's OWN date, "YYYY-MM-DD" -- the caller, `run.py`, parses this from the
    brief's artifact filename), most-recent-first, each
    `{"date": "YYYY-MM-DD", "markdown": "..."}`. See module docstring ("FEED FIX")
    for why this filters LOCALLY against `brief_date` rather than trusting the
    delivery endpoint's own wall-clock exclusion window, and why the return shape
    carries each entry's date.

    Returns `[]` if the read couldn't be performed for any reason (missing
    config, transport failure -- see module docstring on soft degrade), or if
    every fetched entry turned out to be same-day-or-later relative to
    `brief_date` (a young store, or a run whose priors haven't accumulated yet).

    `client` is an optional pre-built `httpx.Client` (or a test double exposing
    the same `.get(url, headers=...)` shape) -- injected so tests never need a
    real network call; defaults to a short-lived real `httpx.Client()`.
    """
    if signing_key is None:
        signing_key = os.environ.get(RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, "")
    if delivery_base_url is None:
        delivery_base_url = os.environ.get(DELIVERY_BASE_URL_ENV_VAR, "")

    if not signing_key or not delivery_base_url:
        return []

    raw = _fetch_raw(
        fetch_count=count + _OVER_FETCH_MARGIN,
        signing_key=signing_key,
        delivery_base_url=delivery_base_url,
        ttl_seconds=ttl_seconds,
        client=client,
    )

    # Drop same-or-future dates relative to brief_date (a "prior" can only ever
    # be a day STRICTLY before the brief under test), then dedupe by date keeping
    # the first (most-recent, per the endpoint's own ordering) occurrence, then
    # cap at `count`. ISO "YYYY-MM-DD" strings compare lexicographically exactly
    # like calendar dates, so plain string comparison is correct here (no date
    # parsing needed).
    filtered: list[dict[str, str]] = []
    seen_dates: set[str] = set()
    for entry in raw:
        date = entry["date"]
        if date >= brief_date:
            continue
        if date in seen_dates:
            continue
        seen_dates.add(date)
        filtered.append(entry)
        if len(filtered) >= count:
            break

    return filtered


__all__ = [
    "RECENT_BRIEFS_SIGNING_KEY_ENV_VAR",
    "DELIVERY_BASE_URL_ENV_VAR",
    "DEFAULT_TTL_SECONDS",
    "DEFAULT_COUNT",
    "DEFAULT_UNFILTERED_COUNT",
    "RecentBriefsUnavailableError",
    "fetch_recent_prior_briefs",
    "fetch_recent_briefs_unfiltered",
]
