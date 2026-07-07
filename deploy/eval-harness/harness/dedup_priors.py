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


def fetch_recent_prior_briefs_markdown(
    *,
    count: int = DEFAULT_COUNT,
    signing_key: str | None = None,
    delivery_base_url: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    client: Any = None,
) -> list[str]:
    """Return up to `count` recent prior briefs' markdown bodies (most-recent-first,
    matching `GET /recent-briefs`'s own ordering), or an empty list if the read
    couldn't be performed for any reason (see module docstring on soft degrade).

    `client` is an optional pre-built `httpx.Client` (or a test double exposing the
    same `.get(url, headers=...)` shape) -- injected so tests never need a real
    network call; defaults to a short-lived real `httpx.Client()`.
    """
    if signing_key is None:
        signing_key = os.environ.get(RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, "")
    if delivery_base_url is None:
        delivery_base_url = os.environ.get(DELIVERY_BASE_URL_ENV_VAR, "")

    if not signing_key or not delivery_base_url:
        return []

    token = recent_briefs_token.generate(signing_key, ttl_seconds=ttl_seconds)
    url = f"{delivery_base_url.rstrip('/')}/recent-briefs?count={count}"
    headers = {"Authorization": f"Bearer {token}"}

    owns_client = client is None
    http_client = client or httpx.Client(timeout=30.0)
    try:
        response = http_client.get(url, headers=headers)
        response.raise_for_status()
        body = response.json()
    except Exception as e:  # noqa: BLE001 - a prior-briefs read glitch must never abort the run
        # review-fix (reviewer Low): the silent degrade-to-empty-list above was
        # UNDIAGNOSABLE -- a real GET /recent-briefs failure (bad signing key,
        # DELIVERY_BASE_URL unreachable, an unexpected response shape) looked
        # IDENTICAL to "no prior briefs exist yet" from the caller's side. Still
        # never aborts the run (dedup just degrades to insufficient_data, per
        # this module's own documented contract), but now leaves a clear,
        # greppable trace of WHY.
        print(f"DEDUP_PRIORS_FETCH_FAILED: {e!r}", file=sys.stderr)
        return []
    finally:
        if owns_client:
            http_client.close()

    briefs = body.get("briefs", [])
    return [b["markdown"] for b in briefs if isinstance(b, dict) and b.get("markdown")]


__all__ = [
    "RECENT_BRIEFS_SIGNING_KEY_ENV_VAR",
    "DELIVERY_BASE_URL_ENV_VAR",
    "DEFAULT_TTL_SECONDS",
    "DEFAULT_COUNT",
    "fetch_recent_prior_briefs_markdown",
]
