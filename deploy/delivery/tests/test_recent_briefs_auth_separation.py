"""THE security-critical test for ADR-0014 Decision 2d: proves the delivery
bearer token and the recent-briefs read-only bearer token are NOT interchangeable.

This is what preserves FR-1/FR-7 (PRD docs/prd/agent-system-redesign.md) by
construction: a `cloud` candidate given ONLY the read token must be structurally
unable to authenticate to `POST /deliver` (and so can never trigger a real
send/fan-out to subscribers), and conversely the delivery/send token must not
authenticate `GET /recent-briefs` either (so a leaked/rotated delivery secret does
not also grant read access, keeping the two blast radii independent).

Exercised at the top-level `handler()` dispatch (not just each auth module in
isolation) -- this is the strongest possible proof, since it confirms the ACTUAL
routing+auth wiring in handler.py keeps the two secrets separate end-to-end, not
merely that the two auth modules are separately correct in a vacuum."""

from __future__ import annotations

import json

import handler as handler_module


DELIVERY_TOKEN = "delivery-secret-abc"
READ_TOKEN = "read-only-secret-xyz"


def _configure_both_secrets(monkeypatch, *, delivery_secret: str, read_secret: str) -> None:
    """Wire handler.py's two imported auth modules to fixed, KNOWN, DIFFERENT
    secrets for this test -- bypassing any real Secrets Manager fetch. Each
    module's real `is_authorized()` already supports a `secret=` override
    (exactly for this kind of test); this binds that override to a fixed known
    value via a small closure capturing the ORIGINAL (unpatched) function object
    -- so `handler()`'s call site (`is_authorized(event)`, no `secret=` kwarg)
    still exercises the REAL constant-time-compare/header-parsing logic in each
    module -- only the secret VALUE is faked, not the comparison itself."""
    original_delivery_is_authorized = handler_module.delivery_auth.is_authorized
    original_recent_briefs_is_authorized = handler_module.recent_briefs_auth.is_authorized
    monkeypatch.setattr(
        handler_module.delivery_auth,
        "is_authorized",
        lambda event: original_delivery_is_authorized(event, secret=delivery_secret),
    )
    monkeypatch.setattr(
        handler_module.recent_briefs_auth,
        "is_authorized",
        lambda event: original_recent_briefs_is_authorized(event, secret=read_secret),
    )


def _event_with_bearer(token: str, *, method: str, path: str, path_params: dict | None = None) -> dict:
    event = {
        "requestContext": {"http": {"method": method}},
        "headers": {"Authorization": f"Bearer {token}"},
        "rawPath": path,
    }
    if path_params is not None:
        event["pathParameters"] = path_params
    if method == "POST":
        event["body"] = json.dumps(
            {
                "contractVersion": 1,
                "brief_markdown": "# Brief",
                "listening_script": "Script.",
            }
        )
    return event


# ---------------------------------------------------------------------------
# The read token must NOT authenticate the delivery/send routes.
# ---------------------------------------------------------------------------


def test_read_token_does_not_authenticate_post_deliver(monkeypatch):
    _configure_both_secrets(monkeypatch, delivery_secret=DELIVERY_TOKEN, read_secret=READ_TOKEN)

    event = _event_with_bearer(READ_TOKEN, method="POST", path="/deliver")
    result = handler_module.handler(event, None)

    assert result["statusCode"] == 401


def test_read_token_does_not_authenticate_get_deliver_poll(monkeypatch):
    _configure_both_secrets(monkeypatch, delivery_secret=DELIVERY_TOKEN, read_secret=READ_TOKEN)

    event = _event_with_bearer(
        READ_TOKEN, method="GET", path="/deliver/some-id", path_params={"deliveryId": "some-id"}
    )
    result = handler_module.handler(event, None)

    assert result["statusCode"] == 401


# ---------------------------------------------------------------------------
# The delivery/send token must NOT authenticate GET /recent-briefs.
# ---------------------------------------------------------------------------


def test_delivery_token_does_not_authenticate_get_recent_briefs(monkeypatch):
    _configure_both_secrets(monkeypatch, delivery_secret=DELIVERY_TOKEN, read_secret=READ_TOKEN)

    event = _event_with_bearer(DELIVERY_TOKEN, method="GET", path="/recent-briefs")
    result = handler_module.handler(event, None)

    assert result["statusCode"] == 401


# ---------------------------------------------------------------------------
# Each token DOES authenticate its OWN intended route(s) -- confirms the 401s
# above are genuinely about token/route mismatch, not a broken auth gate that
# rejects everything.
# ---------------------------------------------------------------------------


def test_read_token_does_authenticate_get_recent_briefs(monkeypatch):
    _configure_both_secrets(monkeypatch, delivery_secret=DELIVERY_TOKEN, read_secret=READ_TOKEN)
    monkeypatch.setattr(
        handler_module,
        "_handle_recent_briefs",
        lambda event, s3_client: {"statusCode": 200, "body": json.dumps({"contractVersion": 1, "briefs": []})},
    )

    event = _event_with_bearer(READ_TOKEN, method="GET", path="/recent-briefs")
    result = handler_module.handler(event, None)

    assert result["statusCode"] == 200


def test_delivery_token_does_authenticate_get_deliver_poll(monkeypatch, deliveries_table):
    _configure_both_secrets(monkeypatch, delivery_secret=DELIVERY_TOKEN, read_secret=READ_TOKEN)
    monkeypatch.setattr(handler_module, "DELIVERIES_TABLE_NAME", "brief-deliveries-test")

    event = _event_with_bearer(
        DELIVERY_TOKEN, method="GET", path="/deliver/does-not-exist", path_params={"deliveryId": "does-not-exist"}
    )
    result = handler_module.handler(event, None)

    # 404 (unknown deliveryId), NOT 401 -- proves the delivery token DID pass the
    # bearer-auth gate for its own intended route; the auth check itself is not
    # what produced this status.
    assert result["statusCode"] == 404


# ---------------------------------------------------------------------------
# Misconfiguration / malformed-input, exercised THROUGH the real handler()
# dispatch (not just each module in isolation) -- this file's whole point is
# proving the property end-to-end, so the "neither secret configured" and
# "malformed Authorization header" interactions belong here too (reviewer
# follow-up on Decision 2d).
# ---------------------------------------------------------------------------


def test_recent_briefs_fails_closed_through_dispatch_when_no_secret_configured(monkeypatch):
    """If the read secret is unresolved (None -- e.g. a fetch glitch or an
    unpopulated secret), GET /recent-briefs must 401 through the real dispatch,
    never fall open to an unauthenticated read."""
    original = handler_module.recent_briefs_auth.is_authorized
    monkeypatch.setattr(
        handler_module.recent_briefs_auth, "is_authorized", lambda event: original(event, secret=None)
    )

    event = _event_with_bearer(READ_TOKEN, method="GET", path="/recent-briefs")
    result = handler_module.handler(event, None)

    assert result["statusCode"] == 401


def test_recent_briefs_fails_closed_through_dispatch_on_malformed_authorization_header(monkeypatch):
    """A non-Bearer / malformed Authorization header on GET /recent-briefs must
    401 through the real dispatch -- the read secret is configured, but the header
    carries no usable bearer token."""
    _configure_both_secrets(monkeypatch, delivery_secret=DELIVERY_TOKEN, read_secret=READ_TOKEN)

    event = {
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Basic {READ_TOKEN}"},  # wrong scheme, not "Bearer"
        "rawPath": "/recent-briefs",
    }
    result = handler_module.handler(event, None)

    assert result["statusCode"] == 401


def test_non_get_to_recent_briefs_is_405_not_a_fallthrough_to_delivery(monkeypatch):
    """A (gateway-impossible) non-GET to /recent-briefs must NOT fall through to
    the delivery-auth/trigger path -- it is captured by the path-based read branch,
    authenticated by the READ secret, and then rejected 405. This pins the
    '/recent-briefs is self-contained under the read secret' hardening: a non-GET
    here can never reach POST /deliver's trigger logic."""
    _configure_both_secrets(monkeypatch, delivery_secret=DELIVERY_TOKEN, read_secret=READ_TOKEN)

    event = _event_with_bearer(READ_TOKEN, method="POST", path="/recent-briefs")
    result = handler_module.handler(event, None)

    assert result["statusCode"] == 405
    # The delivery token must NOT unlock this path either -- confirms the 405 branch
    # is still behind the READ secret, not the delivery one.
    event_delivery = _event_with_bearer(DELIVERY_TOKEN, method="POST", path="/recent-briefs")
    assert handler_module.handler(event_delivery, None)["statusCode"] == 401


# ---------------------------------------------------------------------------
# Structural proof: the two auth checks are genuinely independent function
# calls against genuinely independent modules/secrets, not one shared code path
# that happens to be parameterized identically.
# ---------------------------------------------------------------------------


def test_delivery_auth_and_recent_briefs_auth_are_distinct_modules_with_distinct_env_vars():
    """`handler.py` imports two genuinely SEPARATE Python modules -- not one
    module reused/aliased twice -- each reading its OWN env var
    (`DELIVERY_BEARER_SECRET_ARN` vs. `RECENT_BRIEFS_READ_BEARER_SECRET_ARN`),
    which in turn (per stack.py) resolve to two separate Secrets Manager ARNs.
    This is the module-identity half of the separation property; the behavioral
    half (the tokens don't actually cross-authenticate) is proven by the tests
    above."""
    assert handler_module.delivery_auth is not handler_module.recent_briefs_auth
    assert handler_module.delivery_auth.__name__ != handler_module.recent_briefs_auth.__name__
