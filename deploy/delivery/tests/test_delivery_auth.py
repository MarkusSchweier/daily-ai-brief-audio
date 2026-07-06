"""Unit tests for functions/deliver/delivery_auth.py (ADR-0014 Decision 2b: a
single shared bearer secret, hmac.compare_digest-checked; no secret => 401).

Adapted from deploy/eval/tests/test_review_auth.py's coverage (same shape,
different module/secret) -- 401 on missing/wrong/absent token, and the
query-string fallback deliberately absent here (see delivery_auth.py's module
docstring for why -- this boundary has exactly one scripted caller, never a human
clicking a link)."""

import delivery_auth


def test_authorized_with_matching_bearer_header():
    event = {"headers": {"Authorization": "Bearer secret123"}}
    assert delivery_auth.is_authorized(event, secret="secret123") is True


def test_unauthorized_with_mismatched_bearer_header():
    event = {"headers": {"Authorization": "Bearer wrong"}}
    assert delivery_auth.is_authorized(event, secret="secret123") is False


def test_unauthorized_with_no_secret_configured():
    """No secret configured (e.g. Secrets Manager fetch failed or unset) must fail
    CLOSED, never open."""
    event = {"headers": {"Authorization": "Bearer anything"}}
    assert delivery_auth.is_authorized(event, secret=None) is False


def test_unauthorized_with_no_bearer_supplied():
    event = {"headers": {}}
    assert delivery_auth.is_authorized(event, secret="secret123") is False


def test_unauthorized_with_empty_headers_dict_entirely_absent():
    """No `headers` key at all (rather than an empty dict) must still resolve to
    unauthorized, not raise."""
    event = {}
    assert delivery_auth.is_authorized(event, secret="secret123") is False


def test_unauthorized_with_non_bearer_authorization_scheme():
    event = {"headers": {"Authorization": "Basic secret123"}}
    assert delivery_auth.is_authorized(event, secret="secret123") is False


def test_query_string_param_is_not_a_valid_auth_mechanism_here():
    """Deliberately DIFFERENT from review_auth.py: delivery_auth.py has no `?k=`
    query-string fallback (see module docstring) -- a query-string-only request
    must be rejected even with the right value, unlike the eval review UI's
    reviewer-convenience mechanism."""
    event = {"queryStringParameters": {"k": "secret123"}, "headers": {}}
    assert delivery_auth.is_authorized(event, secret="secret123") is False


def test_unauthorized_response_shape():
    response = delivery_auth.unauthorized_response()
    assert response["statusCode"] == 401


def test_is_authorized_uses_configured_secret_when_not_overridden(monkeypatch):
    """Sanity check on the default-argument fetch path (the "__UNSET__" sentinel
    triggers `_get_delivery_bearer_secret()`), exercised via monkeypatching the
    cache directly rather than mocking Secrets Manager -- proving the parameter
    default actually reaches the module-level cache, not just the explicit-secret
    test-only code path the other tests use."""
    monkeypatch.setattr(delivery_auth, "_secret_cache", "cached-secret")
    monkeypatch.setattr(delivery_auth, "_secret_fetch_attempted", True)

    event = {"headers": {"Authorization": "Bearer cached-secret"}}
    assert delivery_auth.is_authorized(event) is True

    event_wrong = {"headers": {"Authorization": "Bearer wrong-secret"}}
    assert delivery_auth.is_authorized(event_wrong) is False


# ---------------------------------------------------------------------------
# Malformed/extra whitespace in the Authorization header (reviewer-noted,
# not-blocking follow-up): confirmed safe today -- every variant below falls
# through to a hmac.compare_digest mismatch (never authorizes), but there was
# no test pinning that behavior explicitly before this.
# ---------------------------------------------------------------------------


def test_unauthorized_with_double_space_between_bearer_and_token():
    """`"Bearer  secret123"` (two spaces): `_extract_bearer()`'s
    `value.split(" ", 1)` yields `" secret123"` (a leading space folded into
    the extracted token), which correctly fails to match the real secret."""
    event = {"headers": {"Authorization": "Bearer  secret123"}}
    assert delivery_auth.is_authorized(event, secret="secret123") is False


def test_unauthorized_with_leading_and_trailing_whitespace_on_the_whole_header():
    """`"  Bearer secret123  "`: the leading whitespace breaks the
    `parts[0].lower() == "bearer"` scheme check entirely (the header no longer
    starts with the literal `Bearer` token), so no bearer value is extracted at
    all -- correctly unauthorized, not a partial/lenient match."""
    event = {"headers": {"Authorization": "  Bearer secret123  "}}
    assert delivery_auth.is_authorized(event, secret="secret123") is False


def test_unauthorized_with_tab_character_instead_of_space():
    """A tab (not a literal space) between `Bearer` and the token: `split(" ", 1)`
    never finds a plain space to split on, so `parts` has only one element and
    the length check fails -- correctly unauthorized."""
    event = {"headers": {"Authorization": "Bearer\tsecret123"}}
    assert delivery_auth.is_authorized(event, secret="secret123") is False


def test_unauthorized_with_trailing_whitespace_appended_to_the_token_itself():
    """`"Bearer secret123  "` (trailing spaces after an otherwise-correct
    token): the extracted value includes the trailing whitespace verbatim, so
    it no longer equals the real secret exactly -- correctly unauthorized, not
    a lenient/stripped match."""
    event = {"headers": {"Authorization": "Bearer secret123  "}}
    assert delivery_auth.is_authorized(event, secret="secret123") is False
