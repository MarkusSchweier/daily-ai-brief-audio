"""Shared reviewer-gating helper (ADR-0013 §E): a single shared bearer secret, held in
Secrets Manager, checked with `hmac.compare_digest` -- the same constant-time-compare
+ ARN-scoped `GetSecretValue` pattern ADR-0011/ADR-0003 already establish elsewhere in
this repo. No secret ⇒ 401. Duplicated (not a shared package) across this app's
function directories the same way `feedback_token.py` is hand-duplicated across three
independent deploy units in this repo (see that module's docstring) -- each Lambda's
code asset is its own directory, so this is copied verbatim into every function
directory that needs it.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

REVIEW_SECRET_ARN = os.environ.get("REVIEW_SECRET_ARN", "")

_secret_cache: str | None = None
_secret_fetch_attempted = False


def _get_review_secret() -> str | None:
    """Fetch the reviewer bearer secret once per cold start. Returns None (never
    raises) when unset or the fetch fails -- the caller treats None the same as "no
    secret configured", which fails CLOSED (401), not open."""
    global _secret_cache, _secret_fetch_attempted
    if _secret_fetch_attempted:
        return _secret_cache
    _secret_fetch_attempted = True
    if not REVIEW_SECRET_ARN:
        return None
    try:
        import boto3

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=REVIEW_SECRET_ARN)
        _secret_cache = response["SecretString"]
    except Exception:  # noqa: BLE001 - fail closed: a fetch glitch must never authorize a request
        _secret_cache = None
    return _secret_cache


def _extract_bearer(event: dict[str, Any]) -> str:
    """Read the reviewer key from either an `Authorization: Bearer <key>` header or a
    `?k=` query-string param (ADR-0013 §E: "the reviewer supplies it once ... or it
    rides in a bookmarked ?k= param the page keeps in sessionStorage")."""
    headers = event.get("headers") or {}
    for name, value in headers.items():
        if name.lower() == "authorization" and value:
            parts = value.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1]
    params = event.get("queryStringParameters") or {}
    return params.get("k") or ""


def is_authorized(event: dict[str, Any], *, secret: str | None = "__UNSET__") -> bool:
    """True only if the request carries a bearer value that constant-time-matches the
    configured reviewer secret. No configured secret, no supplied value, or a
    mismatch all resolve to False (401) -- never a partial/lenient pass."""
    if secret == "__UNSET__":
        secret = _get_review_secret()
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


__all__ = ["is_authorized", "unauthorized_response", "REVIEW_SECRET_ARN"]
