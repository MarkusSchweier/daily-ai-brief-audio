"""Unit tests for functions/deliver/recent_briefs_auth.py (ADR-0014 Decision 2d,
as amended 2026-07-06 by the "Correction: how the read token actually reaches a
`cloud` candidate" -- status ACCEPTED): a SEPARATE, read-only HMAC signing key
gating GET /recent-briefs; a caller must present a valid, unexpired
`recent_briefs_token`-scheme SIGNED token; no configured signing key, no
supplied token, a malformed/tampered/wrong-scope token, or an EXPIRED token all
=> 401.

Adapted from test_delivery_auth.py's coverage (same shape, a DIFFERENT
module/secret) -- 401 on missing/wrong/absent/expired token, and the
query-string fallback deliberately absent here, same reasoning as
delivery_auth.py (this boundary's only caller is a `cloud` candidate's own
scripted `curl`, never a human clicking a link).

The cross-module non-interchangeability proof (the delivery bearer token does NOT
authenticate here, and vice versa) lives in
test_recent_briefs_auth_separation.py -- kept separate from this file since it is
the single most security-critical assertion this whole decision rests on and
deserves its own clearly-named home."""

import recent_briefs_auth
import recent_briefs_token

SIGNING_KEY = "readsecret123"


def _valid_token(signing_key: str = SIGNING_KEY, *, ttl_seconds: int = 900) -> str:
    return recent_briefs_token.generate(signing_key, ttl_seconds=ttl_seconds)


def test_authorized_with_freshly_minted_signed_token():
    token = _valid_token()
    event = {"headers": {"Authorization": f"Bearer {token}"}}
    assert recent_briefs_auth.is_authorized(event, secret=SIGNING_KEY) is True


def test_unauthorized_with_a_plain_static_non_signed_bearer_value():
    """A caller presenting the raw signing-key value itself (the OLD
    static-bearer behavior) must now be rejected -- it is not a
    `recent_briefs_token`-scheme token at all (no `.`, no valid signature)."""
    event = {"headers": {"Authorization": f"Bearer {SIGNING_KEY}"}}
    assert recent_briefs_auth.is_authorized(event, secret=SIGNING_KEY) is False


def test_unauthorized_with_mismatched_signing_key():
    token = recent_briefs_token.generate("a-different-key", ttl_seconds=900)
    event = {"headers": {"Authorization": f"Bearer {token}"}}
    assert recent_briefs_auth.is_authorized(event, secret=SIGNING_KEY) is False


def test_unauthorized_with_expired_token():
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=-1)
    event = {"headers": {"Authorization": f"Bearer {token}"}}
    assert recent_briefs_auth.is_authorized(event, secret=SIGNING_KEY) is False


def test_unauthorized_with_tampered_token():
    token = _valid_token()
    payload_b64url, sig_b64url = token.split(".")
    tampered_sig = sig_b64url[:-4] + ("aaaa" if sig_b64url[-4:] != "aaaa" else "bbbb")
    event = {"headers": {"Authorization": f"Bearer {payload_b64url}.{tampered_sig}"}}
    assert recent_briefs_auth.is_authorized(event, secret=SIGNING_KEY) is False


def test_unauthorized_with_no_secret_configured():
    """No secret configured (e.g. Secrets Manager fetch failed or unset) must fail
    CLOSED, never open -- even with an otherwise perfectly valid signed token."""
    token = _valid_token()
    event = {"headers": {"Authorization": f"Bearer {token}"}}
    assert recent_briefs_auth.is_authorized(event, secret=None) is False


def test_unauthorized_with_no_bearer_supplied():
    event = {"headers": {}}
    assert recent_briefs_auth.is_authorized(event, secret=SIGNING_KEY) is False


def test_unauthorized_with_empty_headers_dict_entirely_absent():
    """No `headers` key at all (rather than an empty dict) must still resolve to
    unauthorized, not raise."""
    event = {}
    assert recent_briefs_auth.is_authorized(event, secret=SIGNING_KEY) is False


def test_unauthorized_with_non_bearer_authorization_scheme():
    token = _valid_token()
    event = {"headers": {"Authorization": f"Basic {token}"}}
    assert recent_briefs_auth.is_authorized(event, secret=SIGNING_KEY) is False


def test_query_string_param_is_not_a_valid_auth_mechanism_here():
    """Deliberately no `?k=` query-string fallback (see module docstring) -- a
    query-string-only request must be rejected even with a valid signed token."""
    token = _valid_token()
    event = {"queryStringParameters": {"k": token}, "headers": {}}
    assert recent_briefs_auth.is_authorized(event, secret=SIGNING_KEY) is False


def test_unauthorized_response_shape():
    response = recent_briefs_auth.unauthorized_response()
    assert response["statusCode"] == 401


def test_is_authorized_uses_configured_secret_when_not_overridden(monkeypatch):
    """Sanity check on the default-argument fetch path (the "__UNSET__" sentinel
    triggers `_get_recent_briefs_read_bearer_secret()`), exercised via
    monkeypatching the cache directly rather than mocking Secrets Manager --
    proving the parameter default actually reaches the module-level cache, not
    just the explicit-secret test-only code path the other tests use."""
    monkeypatch.setattr(recent_briefs_auth, "_secret_cache", "cached-read-secret")
    monkeypatch.setattr(recent_briefs_auth, "_secret_fetch_attempted", True)

    token = recent_briefs_token.generate("cached-read-secret", ttl_seconds=900)
    event = {"headers": {"Authorization": f"Bearer {token}"}}
    assert recent_briefs_auth.is_authorized(event) is True

    event_wrong = {"headers": {"Authorization": "Bearer wrong-token-entirely"}}
    assert recent_briefs_auth.is_authorized(event_wrong) is False
