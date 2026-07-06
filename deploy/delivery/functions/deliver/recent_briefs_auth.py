"""Signed-read-token auth gate for `GET /recent-briefs` (PRD
docs/prd/agent-system-redesign.md FR-8/AC-8, ADR-0014 Decision 2d, as amended
2026-07-06 by Decision 2d's "Correction: how the read token actually reaches a
`cloud` candidate" -- status ACCEPTED, ratified by the human): a `cloud`
candidate reads the same recent priors production reads from S3 via this
route, WITHOUT holding any AWS credential itself.

The Secrets Manager secret this module reads (`RECENT_BRIEFS_READ_BEARER_SECRET_ARN`
-- SEPARATE from `delivery_auth.py`'s `DELIVERY_BEARER_SECRET_ARN`) is no longer
compared directly as a static bearer value. It is now an HMAC SIGNING KEY: a
caller must present a `recent_briefs_token`-scheme SIGNED, SHORT-LIVED token
(`<payload_b64url>.<sig_b64url>`, carrying an `exp` claim) minted by the trigger
side (`candidate_sync/trigger.py`) using this SAME secret value, and this module
verifies the signature AND the expiry (`recent_briefs_token.verify()`) rather
than comparing the presented value to the secret byte-for-byte. This closes the
one gap a static bearer had: `initial_events` (the only channel available today
to get a per-run value into a `cloud` sandbox) is echoed into the session
transcript, so a static token would need rotation after every run -- a signed
token with a short TTL is instead dead within minutes on its own, with nothing
to rotate.

The central constraint Decision 2d establishes is unchanged by this signing
scheme: "read capability must NOT confer send capability" -- a candidate
holding only a validly-signed read token must be structurally unable to
authenticate to `POST /deliver` / `GET /deliver/{deliveryId}` (that endpoint
checks a completely different secret, `DELIVERY_BEARER_SECRET_ARN`, via
`delivery_auth.py`'s own static-bearer compare, unaffected by this change), and
the delivery/send token must not authenticate here (it was never signed with
THIS secret as a `recent-briefs`-scoped token, so `recent_briefs_token.verify()`
rejects it). Two distinct secrets is what makes that separation hold at the KEY
level, not merely by trusting the candidate not to call `/deliver` -- see
`test_delivery_auth.py` / this module's own test file /
`test_recent_briefs_auth_separation.py` for the non-interchangeability proof.

A sibling module (not a generalized/parameterized `delivery_auth.py`) by deliberate
choice: `delivery_auth.py` is an already-reviewed, security-critical, fully-tested
module (ADR-0014 Decision 2b: "its auth must be the tightest thing in the redesign").
Parameterizing it over "which secret ARN, and whether to sign-verify vs. compare
directly" would touch that file's tested code paths for a second, distinct use; a
sibling module with its own env var and its own cache keeps `delivery_auth.py`
byte-identical (its existing tests pass unchanged) while reusing the same
fail-closed shape: `Authorization: Bearer <token>` header only (no query-string
fallback -- this boundary's only caller is a `cloud` candidate's own scripted
`curl`, never a human clicking a link), and no configured secret / no supplied
token / a malformed, expired, or wrong-scope/wrong-signature token all resolve
to 401, never a fall-open.
"""

from __future__ import annotations

import os
from typing import Any

import recent_briefs_token

RECENT_BRIEFS_READ_BEARER_SECRET_ARN = os.environ.get("RECENT_BRIEFS_READ_BEARER_SECRET_ARN", "")

_secret_cache: str | None = None
_secret_fetch_attempted = False


def _get_recent_briefs_read_bearer_secret() -> str | None:
    """Fetch the recent-briefs HMAC signing key once per cold start (the same
    Secrets Manager value that used to be compared directly as a static bearer
    token -- it is now used as `recent_briefs_token.verify()`'s signing key
    instead). Returns None (never raises) when unset or the fetch fails -- the
    caller treats None the same as "no secret configured", which fails CLOSED
    (401), not open."""
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
    """True only if the request carries a `recent_briefs_token`-scheme token
    whose HMAC signature verifies under the configured signing key AND whose
    `exp` claim has not yet passed (`recent_briefs_token.verify()` -- constant-
    time signature compare internally). No configured signing key, no supplied
    token, a malformed token, a wrong/tampered signature, a wrong scope, or an
    EXPIRED token all resolve to False (401) -- never a partial/lenient pass.
    Deliberately independent of `delivery_auth.is_authorized()` -- the two
    check two different secrets, by design (Decision 2d)."""
    if secret == "__UNSET__":
        secret = _get_recent_briefs_read_bearer_secret()
    if not secret:
        return False
    supplied = _extract_bearer(event)
    if not supplied:
        return False
    return recent_briefs_token.verify(secret, supplied)


def unauthorized_response() -> dict[str, Any]:
    return {
        "statusCode": 401,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error": "unauthorized"}',
    }


__all__ = ["is_authorized", "unauthorized_response", "RECENT_BRIEFS_READ_BEARER_SECRET_ARN"]
