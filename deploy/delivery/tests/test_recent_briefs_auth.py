"""Unit tests for functions/deliver/recent_briefs_auth.py (ADR-0014 Decision 2d: a
SEPARATE, read-only bearer secret gating GET /recent-briefs; no secret => 401).

Adapted from test_delivery_auth.py's coverage (same shape, a DIFFERENT
module/secret) -- 401 on missing/wrong/absent token, and the query-string
fallback deliberately absent here, same reasoning as delivery_auth.py (this
boundary's only caller is a `cloud` candidate's own scripted `curl`, never a
human clicking a link).

The cross-module non-interchangeability proof (the delivery bearer token does NOT
authenticate here, and vice versa) lives in
test_recent_briefs_auth_separation.py -- kept separate from this file since it is
the single most security-critical assertion this whole decision rests on and
deserves its own clearly-named home."""

import recent_briefs_auth


def test_authorized_with_matching_bearer_header():
    event = {"headers": {"Authorization": "Bearer readsecret123"}}
    assert recent_briefs_auth.is_authorized(event, secret="readsecret123") is True


def test_unauthorized_with_mismatched_bearer_header():
    event = {"headers": {"Authorization": "Bearer wrong"}}
    assert recent_briefs_auth.is_authorized(event, secret="readsecret123") is False


def test_unauthorized_with_no_secret_configured():
    """No secret configured (e.g. Secrets Manager fetch failed or unset) must fail
    CLOSED, never open."""
    event = {"headers": {"Authorization": "Bearer anything"}}
    assert recent_briefs_auth.is_authorized(event, secret=None) is False


def test_unauthorized_with_no_bearer_supplied():
    event = {"headers": {}}
    assert recent_briefs_auth.is_authorized(event, secret="readsecret123") is False


def test_unauthorized_with_empty_headers_dict_entirely_absent():
    """No `headers` key at all (rather than an empty dict) must still resolve to
    unauthorized, not raise."""
    event = {}
    assert recent_briefs_auth.is_authorized(event, secret="readsecret123") is False


def test_unauthorized_with_non_bearer_authorization_scheme():
    event = {"headers": {"Authorization": "Basic readsecret123"}}
    assert recent_briefs_auth.is_authorized(event, secret="readsecret123") is False


def test_query_string_param_is_not_a_valid_auth_mechanism_here():
    """Deliberately no `?k=` query-string fallback (see module docstring) -- a
    query-string-only request must be rejected even with the right value."""
    event = {"queryStringParameters": {"k": "readsecret123"}, "headers": {}}
    assert recent_briefs_auth.is_authorized(event, secret="readsecret123") is False


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

    event = {"headers": {"Authorization": "Bearer cached-read-secret"}}
    assert recent_briefs_auth.is_authorized(event) is True

    event_wrong = {"headers": {"Authorization": "Bearer wrong-secret"}}
    assert recent_briefs_auth.is_authorized(event_wrong) is False
