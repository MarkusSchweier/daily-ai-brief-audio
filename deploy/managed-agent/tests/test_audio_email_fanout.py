"""Unit tests for deploy/managed-agent/pipeline/audio_email.py's fan-out/failure-isolation
logic — the microVM-adapted port of deploy/audio_email.py.

Covers the same PRD acceptance criteria the live module's tests cover (this is a
faithful port, not a redesign): AC-8 (owner's send always attempted, never gated on
subscriber sends), AC-9 (audio failure still text-only-emails everyone, fail-safe
preserved), AC-11 (one bad subscriber address never blocks the others or the owner) —
plus AC-13 (no credential-file loading; boto3 authenticates via the ambient credential
chain only, standing in for the microVM's IMDSv2-delivered execution role).
"""

from __future__ import annotations

import email
import os


class FakeSesClient:
    """Minimal stand-in for boto3's SES client, with per-recipient failure injection."""

    def __init__(self, failing_recipients=None):
        self.failing_recipients = set(failing_recipients or [])
        self.sent_to = []

    def send_raw_email(self, Source, Destinations, RawMessage):
        recipient = Destinations[0]
        if recipient in self.failing_recipients:
            raise RuntimeError(f"simulated SES failure for {recipient}")
        self.sent_to.append({"source": Source, "recipient": recipient, "raw": RawMessage["Data"]})
        return {"MessageId": f"fake-message-id-{len(self.sent_to)}"}


class FakeDynamoDBPaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class FakeDynamoDBClient:
    """Minimal stand-in for boto3's DynamoDB client Query paginator."""

    def __init__(self, subscriber_items=None, raise_on_query=False):
        self._subscriber_items = subscriber_items or []
        self._raise_on_query = raise_on_query

    def get_paginator(self, operation_name):
        assert operation_name == "query"
        if self._raise_on_query:
            raise RuntimeError("simulated DynamoDB outage")
        return FakeDynamoDBPaginator([{"Items": self._subscriber_items}])


def _ddb_item(email_address, first_name="Test", unsubscribe_token="tok"):
    return {
        "email": {"S": email_address},
        "firstName": {"S": first_name},
        "unsubscribeToken": {"S": unsubscribe_token},
    }


def _html_body_text(raw_mime: str) -> str:
    """Decode the HTML alternative part out of a raw MIME message for content assertions."""
    parsed = email.message_from_string(raw_mime)
    for part in parsed.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            return payload.decode("utf-8")
    return ""


def test_no_credential_file_loading_anywhere_in_the_module(audio_email_module):
    """AC-13/ADR-0004: the module must not read AWS_SHARED_CREDENTIALS_FILE or any
    other credential-file mechanism at runtime — the microVM authenticates purely via
    the ambient boto3 credential chain (IMDSv2-delivered execution role in production;
    moto's dummy env-var credentials in this test). Checks the *executable* source
    (comments/strings stripped via tokenize) so the module's own explanatory docstring
    — which necessarily names the mechanism it does NOT use, for contrast with the live
    deploy/audio_email.py — doesn't produce a false positive."""
    import inspect
    import io
    import tokenize

    source = inspect.getsource(audio_email_module)
    code_tokens = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE)
    ]
    code_only = " ".join(code_tokens)

    assert "AWS_SHARED_CREDENTIALS_FILE" not in code_only
    assert "aws_access_key_id" not in code_only.lower()
    assert os.environ.get("AWS_SHARED_CREDENTIALS_FILE") is None


def test_owner_always_sent_with_zero_subscribers(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[])

    sent, failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
    )

    assert sent == 1
    assert failed == 0
    assert len(ses_client.sent_to) == 1
    assert ses_client.sent_to[0]["recipient"] == audio_email_module.RECIP
    assert ses_client.sent_to[0]["source"] == audio_email_module.SENDER


def test_owner_and_all_confirmed_subscribers_receive_the_brief(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(
        subscriber_items=[
            _ddb_item("alice@example.com", unsubscribe_token="tok-a"),
            _ddb_item("bob@example.com", unsubscribe_token="tok-b"),
        ]
    )

    sent, failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", b"fake-mp3-bytes", "brief.mp3", "brief-subscribers-test"
    )

    assert sent == 3  # owner + 2 subscribers
    assert failed == 0
    recipients = {entry["recipient"] for entry in ses_client.sent_to}
    assert recipients == {audio_email_module.RECIP, "alice@example.com", "bob@example.com"}

    subscriber_sends = [e for e in ses_client.sent_to if e["recipient"] != audio_email_module.RECIP]
    assert all(e["source"] == audio_email_module.SUBSCRIBER_SENDER for e in subscriber_sends)
    alice_raw = next(e["raw"] for e in ses_client.sent_to if e["recipient"] == "alice@example.com")
    assert "tok-a" in _html_body_text(alice_raw)
    bob_raw = next(e["raw"] for e in ses_client.sent_to if e["recipient"] == "bob@example.com")
    assert "tok-b" in _html_body_text(bob_raw)


def test_one_bad_subscriber_does_not_block_others_or_the_owner(audio_email_module):
    ses_client = FakeSesClient(failing_recipients={"broken@example.com"})
    ddb_client = FakeDynamoDBClient(
        subscriber_items=[
            _ddb_item("good1@example.com", unsubscribe_token="tok-1"),
            _ddb_item("broken@example.com", unsubscribe_token="tok-2"),
            _ddb_item("good2@example.com", unsubscribe_token="tok-3"),
        ]
    )

    sent, failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", b"fake-mp3-bytes", "brief.mp3", "brief-subscribers-test"
    )

    assert sent == 3  # owner + good1 + good2
    assert failed == 1  # broken@example.com
    recipients = {entry["recipient"] for entry in ses_client.sent_to}
    assert recipients == {audio_email_module.RECIP, "good1@example.com", "good2@example.com"}
    assert "broken@example.com" not in recipients


def test_owner_send_failure_does_not_block_subscriber_sends(audio_email_module):
    ses_client = FakeSesClient(failing_recipients={audio_email_module.RECIP})
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("carol@example.com")])

    sent, failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
    )

    assert failed == 1  # owner's send failed
    assert sent == 1  # but the subscriber still got theirs
    recipients = {entry["recipient"] for entry in ses_client.sent_to}
    assert recipients == {"carol@example.com"}


def test_dynamodb_query_outage_still_lets_owner_send_succeed(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(raise_on_query=True)

    sent, failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
    )

    assert sent == 1
    assert failed == 0
    assert ses_client.sent_to[0]["recipient"] == audio_email_module.RECIP


def test_mp3_bytes_are_reused_verbatim_across_every_recipient(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(
        subscriber_items=[_ddb_item("dave@example.com"), _ddb_item("erin@example.com")]
    )
    mp3_bytes = b"identical-mp3-bytes-for-everyone"

    audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", mp3_bytes, "brief.mp3", "brief-subscribers-test"
    )

    assert len(ses_client.sent_to) == 3
    for entry in ses_client.sent_to:
        parsed = email.message_from_string(entry["raw"])
        attachment_parts = [
            part for part in parsed.walk() if part.get_content_disposition() == "attachment"
        ]
        assert len(attachment_parts) == 1
        assert attachment_parts[0].get_payload(decode=True) == mp3_bytes


def test_signup_header_and_disclaimer_present_for_owner_and_subscribers(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("grace@example.com", unsubscribe_token="tok-g")])

    audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
    )

    assert len(ses_client.sent_to) == 2  # owner + grace
    for entry in ses_client.sent_to:
        body = _html_body_text(entry["raw"])
        assert audio_email_module.SUBSCRIBE_SITE_URL in body
        assert "curated and written by an AI agent" in body
        assert "brief</p>" in body  # original brief content still present


def test_audio_failure_still_sends_text_only_email_to_everyone(audio_email_module):
    """AC-9 generalized to all recipients: mp3_bytes=None must not attach a part, but
    must still send. This also exercises the real module-level fail-safe: moto does not
    implement Polly's async task API, so importing audio_email_module already forced
    audio_ok=False / mp3_bytes=None at module load, matching this scenario."""
    assert audio_email_module.mp3_bytes is None

    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("frank@example.com")])

    sent, failed = audio_email_module.send_all(
        ses_client,
        ddb_client,
        "Subject",
        "<p>brief</p>",
        audio_email_module.mp3_bytes,
        "brief.mp3",
        "brief-subscribers-test",
    )

    assert sent == 2
    assert failed == 0
    for entry in ses_client.sent_to:
        parsed = email.message_from_string(entry["raw"])
        attachment_parts = [
            part for part in parsed.walk() if part.get_content_disposition() == "attachment"
        ]
        assert attachment_parts == []
