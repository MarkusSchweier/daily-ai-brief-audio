"""Unit tests for the POST /subscribe Lambda handler.

Covers PRD acceptance criteria: AC-1 (new signup creates a pending row + sends a
confirmation email), AC-9 (already-confirmed re-submit is a no-op, neutral response),
AC-10 (unconfirmed re-submit refreshes the token and resends), AC-13 (invalid email
rejected, no record/email), AC-14 (honeypot silently dropped), AC-15 (re-subscribe after
unsubscribe creates a fresh pending row).
"""

import json

import subscriber_common as common
from conftest import import_handler

_handle = import_handler("subscribe")._handle


def _event(body: dict, source_ip: str = "203.0.113.5") -> dict:
    return {
        "body": json.dumps(body),
        "headers": {"content-type": "application/json"},
        "requestContext": {"http": {"sourceIp": source_ip}},
    }


def test_new_signup_creates_pending_row_and_sends_email(subscribers_table, ses_client):
    resp = _handle(
        _event({"email": "New.Subscriber@Example.com", "firstName": "Ada", "lastName": "Lovelace"}),
        subscribers_table,
        ses_client,
    )
    assert resp["statusCode"] == 200

    item = common.get_subscriber(subscribers_table, "new.subscriber@example.com")
    assert item is not None
    assert item["status"] == common.STATUS_PENDING
    assert item["firstName"] == "Ada"
    assert item["lastName"] == "Lovelace"
    assert "confirmToken" in item
    assert item["confirmTokenExpiresAt"] > common.now_epoch()


def test_invalid_email_is_rejected_without_creating_a_record(subscribers_table, ses_client):
    resp = _handle(
        _event({"email": "not-an-email", "firstName": "Ada", "lastName": "Lovelace"}),
        subscribers_table,
        ses_client,
    )
    assert resp["statusCode"] == 400
    assert common.get_subscriber(subscribers_table, "not-an-email") is None


def test_honeypot_filled_is_silently_dropped(subscribers_table, ses_client):
    resp = _handle(
        _event(
            {
                "email": "bot@example.com",
                "firstName": "Bot",
                "lastName": "Actor",
                "website": "http://spam.example",
            }
        ),
        subscribers_table,
        ses_client,
    )
    # Looks like a normal success response...
    assert resp["statusCode"] == 200
    # ...but nothing was actually created.
    assert common.get_subscriber(subscribers_table, "bot@example.com") is None


def test_already_confirmed_resubmit_does_not_reset_or_leak_status(subscribers_table, ses_client):
    email = "confirmed@example.com"
    subscribers_table.put_item(
        Item={
            "email": email,
            "firstName": "Grace",
            "lastName": "Hopper",
            "status": common.STATUS_CONFIRMED,
            "unsubscribeToken": "existing-token",
            "confirmedAt": common.now_epoch() - 1000,
        }
    )

    resp = _handle(
        _event({"email": email, "firstName": "Grace", "lastName": "Hopper"}),
        subscribers_table,
        ses_client,
    )

    assert resp["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_CONFIRMED
    assert item["unsubscribeToken"] == "existing-token"  # untouched


def test_unconfirmed_resubmit_refreshes_token_and_resends(subscribers_table, ses_client):
    email = "pending@example.com"
    old_token = "old-token"
    subscribers_table.put_item(
        Item={
            "email": email,
            "firstName": "Pending",
            "lastName": "Person",
            "status": common.STATUS_PENDING,
            "confirmToken": old_token,
            "confirmTokenExpiresAt": common.now_epoch() + 1000,
            "createdAt": common.now_epoch() - 500,
        }
    )

    resp = _handle(
        _event({"email": email, "firstName": "Pending", "lastName": "Person"}),
        subscribers_table,
        ses_client,
    )

    assert resp["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_PENDING
    assert item["confirmToken"] != old_token


def test_resubscribe_after_unsubscribe_creates_fresh_pending_row(subscribers_table, ses_client):
    email = "returning@example.com"
    subscribers_table.put_item(
        Item={
            "email": email,
            "firstName": "Returning",
            "lastName": "Reader",
            "status": common.STATUS_UNSUBSCRIBED,
            "unsubscribeToken": "old-unsub-token",
            "unsubscribedAt": common.now_epoch() - 100,
        }
    )

    resp = _handle(
        _event({"email": email, "firstName": "Returning", "lastName": "Reader"}),
        subscribers_table,
        ses_client,
    )

    assert resp["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_PENDING
    assert "confirmToken" in item


def test_form_urlencoded_body_is_also_accepted(subscribers_table, ses_client):
    event = {
        "body": "email=formuser%40example.com&firstName=Form&lastName=User",
        "headers": {"content-type": "application/x-www-form-urlencoded"},
        "requestContext": {"http": {"sourceIp": "203.0.113.9"}},
    }
    resp = _handle(event, subscribers_table, ses_client)
    assert resp["statusCode"] == 200
    assert common.get_subscriber(subscribers_table, "formuser@example.com") is not None
