"""Bearer-auth gate for `GET /recent-briefs` (PRD docs/prd/agent-system-redesign.md
FR-8/AC-8, ADR-0014 Decision 2d): a `cloud` candidate reads the same recent priors
production reads from S3 via this route, WITHOUT holding any AWS credential itself.

This module deliberately checks a SEPARATE Secrets Manager secret
(`RECENT_BRIEFS_READ_BEARER_SECRET_ARN`) from `delivery_auth.py`'s
`DELIVERY_BEARER_SECRET_ARN` -- the central constraint Decision 2d establishes is
that "read capability must NOT confer send capability": a candidate holding only the
read token must be structurally unable to authenticate to `POST /deliver` /
`GET /deliver/{deliveryId}`, and the delivery/send token must not authenticate here.
Two distinct secrets is what makes that separation hold at the TOKEN level, not
merely by trusting the candidate not to call `/deliver` -- see `test_delivery_auth.py`
/ this module's own test file for the non-interchangeability proof.

A sibling module (not a generalized/parameterized `delivery_auth.py`) by deliberate
choice: `delivery_auth.py` is an already-reviewed, security-critical, fully-tested
module (ADR-0014 Decision 2b: "its auth must be the tightest thing in the redesign").
Parameterizing it over "which secret ARN" would touch that file's tested code paths
for a second, distinct use; a sibling module with its own env var and its own cache
keeps `delivery_auth.py` byte-identical (its existing tests pass unchanged) while
reusing the exact same fail-closed shape: `hmac.compare_digest`, constant-time
comparison, `Authorization: Bearer <token>` header only (no query-string fallback --
this boundary's only caller is a `cloud` candidate's own scripted `curl`, never a
human clicking a link), and no configured secret / no supplied token / a mismatch all
resolve to 401, never a fall-open.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

RECENT_BRIEFS_READ_BEARER_SECRET_ARN = os.environ.get("RECENT_BRIEFS_READ_BEARER_SECRET_ARN", "")

_secret_cache: str | None = None
_secret_fetch_attempted = False


def _get_recent_briefs_read_bearer_secret() -> str | None:
    """Fetch the recent-briefs read bearer secret once per cold start. Returns
    None (never raises) when unset or the fetch fails -- the caller treats None
    the same as "no secret configured", which fails CLOSED (401), not open."""
    global _secret_cache, _secret_fetch_attempted
    if _secret_fetch_attempted:
        return _secret_cache
    _secret_fetch_attempted = True
    if not RECENT_BRIEFS_READ_BEARER_SECRET_ARN:
        return None
    try:
        import boto3

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=RECENT_BRIEFS_READ_BEARER_SECRET_ARN)
        _secret_cache = response["SecretString"]
    except Exception:  # noqa: BLE001 - fail closed: a fetch glitch must never authorize a request
        _secret_cache = None
    return _secret_cache


def _extract_bearer(event: dict[str, Any]) -> str:
    """Read the caller's bearer token from an `Authorization: Bearer <token>`
    header. No query-string fallback -- see module docstring."""
    headers = event.get("headers") or {}
    for name, value in headers.items():
        if name.lower() == "authorization" and value:
            parts = value.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1]
    return ""


def is_authorized(event: dict[str, Any], *, secret: str | None = "__UNSET__") -> bool:
    """True only if the request carries a bearer value that constant-time-matches
    the configured recent-briefs read bearer secret. No configured secret, no
    supplied value, or a mismatch all resolve to False (401) -- never a
    partial/lenient pass. Deliberately independent of `delivery_auth.is_authorized()`
    -- the two check two different secrets, by design (Decision 2d)."""
    if secret == "__UNSET__":
        secret = _get_recent_briefs_read_bearer_secret()
    if not secret:
        return False
    supplied = _extract_bearer(event)
    if not supplied:
        return False
    return hmac.compare_digest(supplied, secret)


def unauthorized_response() -> dict[str, Any]:
    return {
        "statusCode": 401,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error": "unauthorized"}',
    }


__all__ = ["is_authorized", "unauthorized_response", "RECENT_BRIEFS_READ_BEARER_SECRET_ARN"]
