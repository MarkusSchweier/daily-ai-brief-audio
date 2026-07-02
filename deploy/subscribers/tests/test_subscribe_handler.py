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


def test_missing_first_or_last_name_is_rejected_without_creating_a_record(subscribers_table, ses_client):
    # FR-3: email, first name, and last name are all required.
    for missing_field in ("firstName", "lastName"):
        body = {"email": "ada@example.com", "firstName": "Ada", "lastName": "Lovelace"}
        body[missing_field] = "   "  # whitespace-only, clamp_name reduces it to empty
        resp = _handle(_event(body), subscribers_table, ses_client)
        assert resp["statusCode"] == 400
        assert common.get_subscriber(subscribers_table, "ada@example.com") is None


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


def test_confirm_link_url_encodes_email_with_special_characters(subscribers_table):
    """A local-part containing '&' or '=' is syntactically valid (is_valid_email does not
    forbid it) but, if interpolated unescaped into the confirm link's query string, would
    inject bogus query params and break the link. Regression test: assert the *actual*
    outgoing SES message contains a correctly-encoded single email= param, not a raw,
    query-string-breaking one."""
    from urllib.parse import parse_qs, urlparse

    module = import_handler("subscribe")
    email = "a&b=c@example.com"

    class RecordingSesClient:
        def __init__(self):
            self.sent = []

        def send_email(self, **kwargs):
            self.sent.append(kwargs)
            return {"MessageId": "fake-id"}

    ses = RecordingSesClient()
    resp = module._handle(
        _event({"email": email, "firstName": "Weird", "lastName": "Address"}),
        subscribers_table,
        ses,
    )
    assert resp["statusCode"] == 200
    assert len(ses.sent) == 1

    text_body = ses.sent[0]["Message"]["Body"]["Text"]["Data"]
    link_line = next(line for line in text_body.splitlines() if "/confirm?" in line)
    parsed = urlparse(link_line)
    params = parse_qs(parsed.query)
    # Exactly one email param (not split into email=a & b=c by an unescaped "&"/"=").
    assert params["email"] == [email]
    assert "token" in params


def test_ses_send_failure_still_creates_row_and_returns_neutral_response(subscribers_table, monkeypatch):
    """If SES send_email raises (e.g. transient SES error), the pending row must still
    exist (so a re-submit per AC-10 can trigger a resend) and the response must remain the
    same neutral 200 — a send failure must not surface as a distinguishable error to the
    caller nor abort the row creation."""
    module = import_handler("subscribe")

    class FailingSesClient:
        def send_email(self, **kwargs):
            raise RuntimeError("simulated SES outage")

    resp = module._handle(
        _event({"email": "sesdown@example.com", "firstName": "Down", "lastName": "SES"}),
        subscribers_table,
        FailingSesClient(),
    )

    assert resp["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, "sesdown@example.com")
    assert item is not None
    assert item["status"] == common.STATUS_PENDING


def test_names_are_length_clamped_through_the_handler(subscribers_table, ses_client):
    long_first = "F" * 500
    long_last = "L" * 500
    resp = _handle(
        _event({"email": "longname@example.com", "firstName": long_first, "lastName": long_last}),
        subscribers_table,
        ses_client,
    )
    assert resp["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, "longname@example.com")
    assert len(item["firstName"]) == common.MAX_NAME_LENGTH
    assert len(item["lastName"]) == common.MAX_NAME_LENGTH


def test_malformed_email_variants_are_all_rejected(subscribers_table, ses_client):
    for bad_email in ["no-at-sign.example.com", "double@@example.com", "trailing@dot.", "has space@example.com"]:
        resp = _handle(
            _event({"email": bad_email, "firstName": "Bad", "lastName": "Email"}),
            subscribers_table,
            ses_client,
        )
        assert resp["statusCode"] == 400, f"expected 400 for {bad_email!r}"
        normalized = common.normalize_email(bad_email)
        if normalized:
            assert common.get_subscriber(subscribers_table, normalized) is None

    # Empty email is its own case: normalize_email("") == "" and DynamoDB rejects an
    # empty-string partition key on GetItem, so we only assert the rejection itself here.
    resp = _handle(
        _event({"email": "", "firstName": "Bad", "lastName": "Email"}),
        subscribers_table,
        ses_client,
    )
    assert resp["statusCode"] == 400
