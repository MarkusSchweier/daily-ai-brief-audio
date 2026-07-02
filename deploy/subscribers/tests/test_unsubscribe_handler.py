"""Unit tests for the GET /unsubscribe Lambda handler.

Covers PRD acceptance criteria: AC-4 (valid unsubscribe link marks the subscriber
unsubscribed), AC-12 (using the link twice is a safe idempotent no-op, not an error, and
does not re-subscribe).
"""

import subscriber_common as common
from conftest import import_handler

_handle = import_handler("unsubscribe")._handle


def _event(email: str, token: str) -> dict:
    return {"queryStringParameters": {"email": email, "token": token}}


def test_valid_token_unsubscribes(subscribers_table):
    email = "leaving@example.com"
    token = "unsub-token"
    subscribers_table.put_item(
        Item={"email": email, "status": common.STATUS_CONFIRMED, "unsubscribeToken": token}
    )

    resp = _handle(_event(email, token), subscribers_table)

    assert resp["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_UNSUBSCRIBED
    assert "unsubscribedAt" in item


def test_double_unsubscribe_is_idempotent_not_an_error(subscribers_table):
    email = "leaving-twice@example.com"
    token = "unsub-token-2"
    subscribers_table.put_item(
        Item={"email": email, "status": common.STATUS_CONFIRMED, "unsubscribeToken": token}
    )

    first = _handle(_event(email, token), subscribers_table)
    second = _handle(_event(email, token), subscribers_table)

    assert first["statusCode"] == 200
    assert second["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_UNSUBSCRIBED  # still unsubscribed, not re-subscribed


def test_wrong_token_is_rejected(subscribers_table):
    email = "protected@example.com"
    subscribers_table.put_item(
        Item={"email": email, "status": common.STATUS_CONFIRMED, "unsubscribeToken": "real-token"}
    )

    resp = _handle(_event(email, "fake-token"), subscribers_table)

    assert resp["statusCode"] == 400
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_CONFIRMED  # unchanged


def test_no_such_subscriber_returns_invalid_response(subscribers_table):
    resp = _handle(_event("nobody@example.com", "whatever"), subscribers_table)
    assert resp["statusCode"] == 400


def test_missing_query_params_returns_400(subscribers_table):
    resp = _handle({"queryStringParameters": {}}, subscribers_table)
    assert resp["statusCode"] == 400
