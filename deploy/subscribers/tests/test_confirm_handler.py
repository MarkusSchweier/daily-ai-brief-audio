"""Unit tests for the GET /confirm Lambda handler.

Covers PRD acceptance criteria: AC-2 (valid unexpired link confirms), AC-11 (expired link
fails gracefully and does not confirm), plus invalid-token and idempotent-already-confirmed
paths.
"""

import subscriber_common as common
from conftest import import_handler

_handle = import_handler("confirm")._handle


def _event(email: str, token: str) -> dict:
    return {"queryStringParameters": {"email": email, "token": token}}


def test_valid_unexpired_token_confirms_and_issues_unsubscribe_token(subscribers_table):
    email = "confirming@example.com"
    token = "valid-token"
    subscribers_table.put_item(
        Item={
            "email": email,
            "status": common.STATUS_PENDING,
            "confirmToken": token,
            "confirmTokenExpiresAt": common.now_epoch() + 1000,
        }
    )

    resp = _handle(_event(email, token), subscribers_table)

    assert resp["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_CONFIRMED
    assert "unsubscribeToken" in item
    assert "confirmToken" not in item
    assert "confirmTokenExpiresAt" not in item


def test_expired_token_fails_gracefully_and_does_not_confirm(subscribers_table):
    email = "expired@example.com"
    token = "expired-token"
    subscribers_table.put_item(
        Item={
            "email": email,
            "status": common.STATUS_PENDING,
            "confirmToken": token,
            "confirmTokenExpiresAt": common.now_epoch() - 10,  # already in the past
        }
    )

    resp = _handle(_event(email, token), subscribers_table)

    assert resp["statusCode"] == 400
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_PENDING  # unchanged


def test_wrong_token_is_rejected(subscribers_table):
    email = "mismatch@example.com"
    subscribers_table.put_item(
        Item={
            "email": email,
            "status": common.STATUS_PENDING,
            "confirmToken": "correct-token",
            "confirmTokenExpiresAt": common.now_epoch() + 1000,
        }
    )

    resp = _handle(_event(email, "wrong-token"), subscribers_table)

    assert resp["statusCode"] == 400
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_PENDING


def test_no_such_subscriber_returns_neutral_invalid_response(subscribers_table):
    resp = _handle(_event("nobody@example.com", "whatever-token"), subscribers_table)
    assert resp["statusCode"] == 400


def test_already_confirmed_reclick_is_idempotent(subscribers_table):
    email = "already@example.com"
    subscribers_table.put_item(
        Item={
            "email": email,
            "status": common.STATUS_CONFIRMED,
            "unsubscribeToken": "existing-unsub-token",
        }
    )

    resp = _handle(_event(email, "irrelevant-token"), subscribers_table)

    assert resp["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, email)
    assert item["unsubscribeToken"] == "existing-unsub-token"  # untouched


def test_missing_query_params_returns_400(subscribers_table):
    resp = _handle({"queryStringParameters": {}}, subscribers_table)
    assert resp["statusCode"] == 400
