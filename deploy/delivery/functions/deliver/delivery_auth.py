"""Bearer-auth gate for the decoupled `deploy/delivery/` boundary (PRD
docs/prd/agent-system-redesign.md FR-3, ADR-0014 Decision 2b): the ONLY way a
content-generation agent reaches AWS delivery is `POST /deliver` +
`GET /deliver/{deliveryId}`, both gated by a single shared bearer token held in
Secrets Manager, checked with `hmac.compare_digest` -- no configured secret, no
supplied value, or a mismatch ALL resolve to unauthorized (401), never a fall-open
to an unauthenticated send (Decision 2b: "the delivery endpoint is the only new
surface that can email real subscribers ... its auth must be the tightest thing in
the redesign").

This is a NEW secret/purpose, not a reuse of `deploy/eval/`'s reviewer bearer secret
(`daily-ai-brief/eval-review-bearer-secret`) -- that secret authorizes a HUMAN
reviewer to trigger/inspect evaluation runs; this one authorizes the
content-generation AGENT (the `cloud` sandbox's own `curl` call) to invoke AWS
delivery. Sharing the two would create an undesirable coupling: rotating this
stack's key (e.g. if a `cloud` session's environment variable were ever exposed in
a log) would, if shared, also lock the eval harness's human reviewer out, and vice
versa -- the same blast-radius-independence reasoning
`deploy/eval/brief_eval/stack.py`'s `_build_anthropic_api_key_secret()` docstring
already gives for keeping ITS secret separate from `deploy/managed-agent`'s
environment key.

The constant-time-compare + ARN-scoped `GetSecretValue` + cached-once-per-cold-start
pattern itself is adapted (not copy-pasted) from
`deploy/eval/functions/common/review_auth.py` -- same shape, this module's own name,
docstrings, and env var (`DELIVERY_BEARER_SECRET_ARN`, not `REVIEW_SECRET_ARN`).
Only the `Authorization: Bearer <token>` header is checked here -- unlike
`review_auth.py`, there is no `?k=` query-string fallback: the delivery boundary has
exactly one caller (the content-generation agent's own scripted `curl`), never a
human clicking a bookmarked link, so the query-param convenience `review_auth.py`
offers a human reviewer has no reason to exist here and would only widen the
credential-leakage surface (query strings routinely end up in access logs).
"""

from __future__ import annotations

import hmac
import os
from typing import Any

DELIVERY_BEARER_SECRET_ARN = os.environ.get("DELIVERY_BEARER_SECRET_ARN", "")

_secret_cache: str | None = None
_secret_fetch_attempted = False


def _get_delivery_bearer_secret() -> str | None:
    """Fetch the delivery bearer secret once per cold start. Returns None (never
    raises) when unset or the fetch fails -- the caller treats None the same as "no
    secret configured", which fails CLOSED (401), not open."""
    global _secret_cache, _secret_fetch_attempted
    if _secret_fetch_attempted:
        return _secret_cache
    _secret_fetch_attempted = True
    if not DELIVERY_BEARER_SECRET_ARN:
        return None
    try:
        import boto3

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=DELIVERY_BEARER_SECRET_ARN)
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
    the configured delivery bearer secret. No configured secret, no supplied value,
    or a mismatch all resolve to False (401) -- never a partial/lenient pass."""
    if secret == "__UNSET__":
        secret = _get_delivery_bearer_secret()
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


__all__ = ["is_authorized", "unauthorized_response", "DELIVERY_BEARER_SECRET_ARN"]
