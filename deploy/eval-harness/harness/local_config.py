"""Local-operator config resolution for the two recent-briefs values.

Why this exists (2026-07-07, owner-reported UI trigger failure): the harness
originally read `$RECENT_BRIEFS_SIGNING_KEY` / `$DELIVERY_BASE_URL` from the
process environment only -- fine for the CLI (the README says to export them),
but the Flask UI is typically launched by a preview panel / launchd / a shell
that never exported them, so the very first owner-triggered UI run failed with
"RECENT_BRIEFS_SIGNING_KEY is unset". Depending on whoever started a
long-lived server process to have remembered two exports is bad ergonomics for
a single-operator local tool.

Resolution order, mirroring the Anthropic-API-key convention this repo already
uses (`~/.anthropic-managed-agents/ant-api-key.txt`, read fresh at trigger
time, never committed, never logged):

- **Signing key** (secret): `$RECENT_BRIEFS_SIGNING_KEY` if set, else the
  well-known local file `~/.anthropic-managed-agents/recent-briefs-signing-key.txt`
  (stripped), else None. The file is populated once by the operator from the
  `daily-ai-brief/recent-briefs-read-bearer-secret` Secrets Manager secret
  (see README) -- this module NEVER makes an AWS call (ADR-0016 D1).
- **Delivery base URL** (not a secret -- it is committed all over this repo,
  e.g. in deployment.json and every candidate task prompt): `$DELIVERY_BASE_URL`
  if set, else the committed default below.
"""

from __future__ import annotations

import os
from pathlib import Path

RECENT_BRIEFS_SIGNING_KEY_ENV_VAR = "RECENT_BRIEFS_SIGNING_KEY"
DELIVERY_BASE_URL_ENV_VAR = "DELIVERY_BASE_URL"

SIGNING_KEY_FILE = Path.home() / ".anthropic-managed-agents" / "recent-briefs-signing-key.txt"

# The deploy/delivery/ boundary's live HTTP API (BriefDeliveryStack output). Not a
# secret: the same URL is committed in deploy/managed-agent/deployment.json and in
# every candidate's task-prompt.md. Env var overrides for a future stack move.
DEFAULT_DELIVERY_BASE_URL = "https://6nbe4wsng6.execute-api.us-east-1.amazonaws.com"


def resolve_recent_briefs_signing_key() -> str | None:
    """The signing key from the env var, else the well-known local file, else None.

    Never logs or raises on a missing key -- callers decide whether missing is
    fatal (task-prompt substitution: yes, fail loud) or a soft degrade
    (dedup-priors fetch: proceed with no priors).
    """
    from_env = os.environ.get(RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, "").strip()
    if from_env:
        return from_env
    try:
        from_file = SIGNING_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return from_file or None


def resolve_delivery_base_url() -> str:
    """The delivery boundary base URL from the env var, else the committed default."""
    return os.environ.get(DELIVERY_BASE_URL_ENV_VAR, "").strip() or DEFAULT_DELIVERY_BASE_URL


def signing_key_sources_hint() -> str:
    """One human-readable line naming BOTH places the signing key may live --
    used to extend fail-loud error messages so the operator knows the file
    option exists (the original error named only the env var)."""
    return (
        f"set ${RECENT_BRIEFS_SIGNING_KEY_ENV_VAR} or write the key to {SIGNING_KEY_FILE} "
        "(populate once from the 'daily-ai-brief/recent-briefs-read-bearer-secret' Secrets Manager secret)"
    )
