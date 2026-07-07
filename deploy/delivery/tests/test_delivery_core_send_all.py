"""Unit tests for delivery_core.py's fan-out/failure-isolation logic -- ported from
deploy/managed-agent/tests/test_audio_email_fanout.py's `send_all()` coverage (same
PRD acceptance criteria this is a faithful port, not a redesign, of): owner's send
always attempted, never gated on subscriber sends; audio failure still text-only-
emails everyone (fail-safe preserved); one bad subscriber address never blocks the
others or the owner; the confirmation email's wording variants.

Reuses the SAME FakeSesClient/FakeDynamoDBClient/FakeDynamoDBPaginator test-double
conventions as the ported module's own tests, per this phase's brief ("reuse that
style for your new tests rather than inventing a different mocking approach").

Adapted (not copy-pasted) for `delivery_core.send_all()`'s different signature: the
ported function takes `secretsmanager_client`/`brief_date`/
`subscribers_api_base_url`/`feedback_base_url`/`feedback_token_secret_arn` as
explicit parameters (this module has no module-level env reads at all -- see
delivery_core.py's docstring), rather than reading them from `os.environ` at import
time like audio_email.py does.
"""

from __future__ import annotations

import email

import pytest

import delivery_core


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


class FakeSecretsManagerClient:
    """Stand-in for boto3's secretsmanager client -- no feedback secret configured
    by default (feedback_token_secret_arn="" in every test below unless a test
    opts in), matching the common case these send_all() tests exercise."""

    def __init__(self, secret_value=None):
        self._secret_value = secret_value

    def get_secret_value(self, SecretId):
        if self._secret_value is None:
            raise RuntimeError("no secret configured")
        return {"SecretString": self._secret_value}


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


def _plain_body_text(raw_mime: str) -> str:
    parsed = email.message_from_string(raw_mime)
    for part in parsed.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            return payload.decode("utf-8")
    return ""


@pytest.fixture(autouse=True)
def _reset_feedback_secret_cache():
    """delivery_core's module-level feedback-secret cache (mirroring
    audio_email.py's own cold-start cache) must not leak state between tests."""
    delivery_core._feedback_secret_cache = None
    delivery_core._feedback_secret_fetch_attempted = False
    yield
    delivery_core._feedback_secret_cache = None
    delivery_core._feedback_secret_fetch_attempted = False


def _send_all(ses_client, ddb_client, subject="Subject", brief_html="<p>brief</p>", mp3_bytes=None, **kwargs):
    return delivery_core.send_all(
        ses_client,
        ddb_client,
        FakeSecretsManagerClient(),
        subject,
        brief_html,
        mp3_bytes,
        "brief.mp3",
        "brief-subscribers-test",
        "2026-07-06",
        **kwargs,
    )


def test_owner_always_sent_with_zero_subscribers():
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[])

    sent, failed, sub_sent, sub_failed, query_failed = _send_all(ses_client, ddb_client)

    assert sent == 1
    assert failed == 0
    assert sub_sent == 0
    assert sub_failed == 0
    assert query_failed is False
    assert len(ses_client.sent_to) == 1
    assert ses_client.sent_to[0]["recipient"] == delivery_core.RECIP
    assert ses_client.sent_to[0]["source"] == delivery_core.SENDER


def test_skip_subscriber_fanout_sends_only_the_owner():
    """The manual-validation/eval escape hatch: with skip_subscriber_fanout=True,
    the owner's copy still goes out but the DynamoDB query / subscriber loop never
    runs at all -- proven here by a DynamoDB client that raises if queried."""

    class RaisesIfQueried:
        def get_paginator(self, name):
            raise AssertionError("subscriber fan-out must not query DynamoDB when skipped")

    ses_client = FakeSesClient()

    sent, failed, sub_sent, sub_failed, query_failed = _send_all(
        ses_client, RaisesIfQueried(), skip_subscriber_fanout=True
    )

    assert sent == 1
    assert failed == 0
    assert sub_sent == 0
    assert sub_failed == 0
    assert query_failed is False
    assert len(ses_client.sent_to) == 1
    assert ses_client.sent_to[0]["recipient"] == delivery_core.RECIP


def test_owner_and_all_confirmed_subscribers_receive_the_brief():
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(
        subscriber_items=[
            _ddb_item("alice@example.com", unsubscribe_token="tok-a"),
            _ddb_item("bob@example.com", unsubscribe_token="tok-b"),
        ]
    )

    sent, failed, sub_sent, sub_failed, query_failed = _send_all(ses_client, ddb_client, mp3_bytes=b"fake-mp3-bytes")

    assert sent == 3  # owner + 2 subscribers
    assert failed == 0
    assert sub_sent == 2  # subscriber-only, owner excluded
    assert sub_failed == 0
    assert query_failed is False
    recipients = {entry["recipient"] for entry in ses_client.sent_to}
    assert recipients == {delivery_core.RECIP, "alice@example.com", "bob@example.com"}

    subscriber_sends = [e for e in ses_client.sent_to if e["recipient"] != delivery_core.RECIP]
    assert all(e["source"] == delivery_core.SUBSCRIBER_SENDER for e in subscriber_sends)
    alice_raw = next(e["raw"] for e in ses_client.sent_to if e["recipient"] == "alice@example.com")
    assert "tok-a" in _html_body_text(alice_raw)
    bob_raw = next(e["raw"] for e in ses_client.sent_to if e["recipient"] == "bob@example.com")
    assert "tok-b" in _html_body_text(bob_raw)


def test_one_bad_subscriber_does_not_block_others_or_the_owner():
    ses_client = FakeSesClient(failing_recipients={"broken@example.com"})
    ddb_client = FakeDynamoDBClient(
        subscriber_items=[
            _ddb_item("good1@example.com", unsubscribe_token="tok-1"),
            _ddb_item("broken@example.com", unsubscribe_token="tok-2"),
            _ddb_item("good2@example.com", unsubscribe_token="tok-3"),
        ]
    )

    sent, failed, sub_sent, sub_failed, query_failed = _send_all(ses_client, ddb_client, mp3_bytes=b"fake-mp3-bytes")

    assert sent == 3  # owner + good1 + good2
    assert failed == 1  # broken@example.com
    assert sub_sent == 2  # good1 + good2, owner excluded
    assert sub_failed == 1  # broken@example.com
    assert query_failed is False
    recipients = {entry["recipient"] for entry in ses_client.sent_to}
    assert recipients == {delivery_core.RECIP, "good1@example.com", "good2@example.com"}
    assert "broken@example.com" not in recipients


def test_owner_send_failure_does_not_block_subscriber_sends():
    ses_client = FakeSesClient(failing_recipients={delivery_core.RECIP})
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("carol@example.com")])

    sent, failed, sub_sent, sub_failed, query_failed = _send_all(ses_client, ddb_client)

    assert failed == 1  # owner's send failed
    assert sent == 1  # but the subscriber still got theirs
    assert sub_sent == 1  # carol
    assert sub_failed == 0
    assert query_failed is False
    recipients = {entry["recipient"] for entry in ses_client.sent_to}
    assert recipients == {"carol@example.com"}


def test_dynamodb_query_outage_still_lets_owner_send_succeed():
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(raise_on_query=True)

    sent, failed, sub_sent, sub_failed, query_failed = _send_all(ses_client, ddb_client)

    assert sent == 1
    assert failed == 0
    assert sub_sent == 0
    assert sub_failed == 0
    # A genuine query failure must be surfaced distinctly, not indistinguishable
    # from a real zero-subscriber day.
    assert query_failed is True
    assert ses_client.sent_to[0]["recipient"] == delivery_core.RECIP


def test_mp3_bytes_are_reused_verbatim_across_every_recipient():
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("dave@example.com"), _ddb_item("erin@example.com")])
    mp3_bytes = b"identical-mp3-bytes-for-everyone"

    _send_all(ses_client, ddb_client, mp3_bytes=mp3_bytes)

    assert len(ses_client.sent_to) == 3
    for entry in ses_client.sent_to:
        parsed = email.message_from_string(entry["raw"])
        attachment_parts = [part for part in parsed.walk() if part.get_content_disposition() == "attachment"]
        assert len(attachment_parts) == 1
        assert attachment_parts[0].get_payload(decode=True) == mp3_bytes


def test_signup_header_and_disclaimer_present_for_owner_and_subscribers():
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("grace@example.com", unsubscribe_token="tok-g")])

    _send_all(ses_client, ddb_client)

    assert len(ses_client.sent_to) == 2  # owner + grace
    for entry in ses_client.sent_to:
        body = _html_body_text(entry["raw"])
        assert delivery_core.SUBSCRIBE_SITE_URL in body
        assert "curated and written by an AI agent" in body
        assert "brief</p>" in body  # original brief content still present


def test_audio_failure_still_sends_text_only_email_to_everyone():
    """mp3_bytes=None must not attach a part, but must still send -- the same
    fail-safe synthesize_audio() preserves (its own tests cover the Polly-failure
    path directly; this proves send_all() degrades gracefully given that input)."""
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("frank@example.com")])

    sent, failed, sub_sent, sub_failed, query_failed = _send_all(ses_client, ddb_client, mp3_bytes=None)

    assert sent == 2
    assert failed == 0
    assert sub_sent == 1
    assert sub_failed == 0
    assert query_failed is False
    for entry in ses_client.sent_to:
        parsed = email.message_from_string(entry["raw"])
        attachment_parts = [part for part in parsed.walk() if part.get_content_disposition() == "attachment"]
        assert attachment_parts == []


def test_unsubscribe_footer_present_for_subscribers_not_owner():
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("henry@example.com", unsubscribe_token="tok-h")])

    _send_all(ses_client, ddb_client, subscribers_api_base_url="https://briefing.mschweier.com/api")

    owner_entry = next(e for e in ses_client.sent_to if e["recipient"] == delivery_core.RECIP)
    subscriber_entry = next(e for e in ses_client.sent_to if e["recipient"] == "henry@example.com")

    # Unsubscribe now lives in the top meta box (not a footer): present for subscribers,
    # absent for the owner (not a subscriber).
    owner_body = _html_body_text(owner_entry["raw"])
    assert "unsubscribe</a>" not in owner_body
    assert "tok-h" not in owner_body
    subscriber_body = _html_body_text(subscriber_entry["raw"])
    assert "unsubscribe</a>" in subscriber_body
    assert "tok-h" in subscriber_body


# ---------------------------------------------------------------------------
# Feedback link (docs/prd/reader-feedback.md FR-5, ADR-0011, ADR-0012 §B) -- ported
# from test_audio_email_fanout.py's feedback-link coverage, adapted to
# delivery_core's explicit-parameter shape (no module-level env reads).
# ---------------------------------------------------------------------------

FEEDBACK_SECRET_VALUE = "test-feedback-signing-secret"


def test_feedback_link_present_with_valid_config():
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("alice@example.com")])

    delivery_core.send_all(
        ses_client,
        ddb_client,
        FakeSecretsManagerClient(secret_value=FEEDBACK_SECRET_VALUE),
        "Subject",
        "<p>brief</p>",
        None,
        "brief.mp3",
        "brief-subscribers-test",
        "2026-07-06",
        feedback_base_url="https://feedback.mschweier.com",
        feedback_token_secret_arn="arn:aws:secretsmanager:us-east-1:740353583786:secret:feedback-test-xxxxx",
    )

    assert len(ses_client.sent_to) == 2  # owner + alice
    for entry in ses_client.sent_to:
        body = _html_body_text(entry["raw"])
        assert "feedback.mschweier.com/?t=" in body
        assert "Share feedback" in body


def test_feedback_link_gracefully_absent_when_config_missing_send_still_succeeds():
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("bob@example.com")])

    sent, failed, sub_sent, sub_failed, query_failed = _send_all(ses_client, ddb_client)

    assert sent == 2  # owner + bob -- send is unaffected
    assert failed == 0
    assert len(ses_client.sent_to) == 2
    for entry in ses_client.sent_to:
        body = _html_body_text(entry["raw"])
        assert "Share feedback" not in body
        assert "/?t=" not in body


def test_feedback_link_gracefully_absent_when_base_url_missing_but_secret_configured():
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[])

    sent, failed, _, _, _ = delivery_core.send_all(
        ses_client,
        ddb_client,
        FakeSecretsManagerClient(secret_value=FEEDBACK_SECRET_VALUE),
        "Subject",
        "<p>brief</p>",
        None,
        "brief.mp3",
        "brief-subscribers-test",
        "2026-07-06",
        feedback_token_secret_arn="arn:aws:secretsmanager:us-east-1:740353583786:secret:feedback-test-xxxxx",
        # feedback_base_url deliberately omitted -- both must be set.
    )

    assert sent == 1
    assert failed == 0
    body = _html_body_text(ses_client.sent_to[0]["raw"])
    assert "Share feedback" not in body


def test_feedback_link_uses_correct_per_recipient_identity():
    """The owner's link attributes to RECIP; each subscriber's link attributes to
    their own email -- proven by decoding each recipient's token payload and
    checking the embedded identity matches who actually got that email."""
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(
        subscriber_items=[
            _ddb_item("alice@example.com", unsubscribe_token="tok-a"),
            _ddb_item("bob@example.com", unsubscribe_token="tok-b"),
        ]
    )

    delivery_core.send_all(
        ses_client,
        ddb_client,
        FakeSecretsManagerClient(secret_value=FEEDBACK_SECRET_VALUE),
        "Subject",
        "<p>brief</p>",
        None,
        "brief.mp3",
        "brief-subscribers-test",
        "2026-07-06",
        feedback_base_url="https://feedback.mschweier.com",
        feedback_token_secret_arn="arn:aws:secretsmanager:us-east-1:740353583786:secret:feedback-test-xxxxx",
    )

    import feedback_token

    for entry in ses_client.sent_to:
        body = _html_body_text(entry["raw"])
        token = body.split("/?t=")[1].split('"')[0]
        result = feedback_token.validate(FEEDBACK_SECRET_VALUE, token)
        assert result.valid is True
        assert result.identity == entry["recipient"]


def test_feedback_link_generation_failure_never_blocks_send(monkeypatch):
    """Belt-and-braces: even if token generation itself raises unexpectedly, the
    send must proceed without the link, never raise."""
    import feedback_token

    monkeypatch.setattr(feedback_token, "generate", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[])

    sent, failed, _, _, _ = delivery_core.send_all(
        ses_client,
        ddb_client,
        FakeSecretsManagerClient(secret_value=FEEDBACK_SECRET_VALUE),
        "Subject",
        "<p>brief</p>",
        None,
        "brief.mp3",
        "brief-subscribers-test",
        "2026-07-06",
        feedback_base_url="https://feedback.mschweier.com",
        feedback_token_secret_arn="arn:aws:secretsmanager:us-east-1:740353583786:secret:feedback-test-xxxxx",
    )

    assert sent == 1
    assert failed == 0
    body = _html_body_text(ses_client.sent_to[0]["raw"])
    assert "Share feedback" not in body


# ---------------------------------------------------------------------------
# Post-send owner confirmation (docs/prd/send-confirmation-summary.md) -- ported
# verbatim-in-behavior from test_audio_email_fanout.py.
# ---------------------------------------------------------------------------


class RaisesOnSend:
    def send_raw_email(self, **kwargs):
        raise RuntimeError("simulated SES failure for confirmation send")


def test_confirmation_reports_subscriber_only_count_and_failures():
    subject, body = delivery_core._build_confirmation_email(
        "2026-07-03", 5, 1, skipped=False, subscriber_query_failed=False
    )
    assert "2026-07-03" in subject
    assert "2026-07-03" in body
    assert "Sent to 5 subscribers" in body
    assert "1 subscriber send failed" in body


def test_confirmation_zero_subscribers_no_failure_mention():
    subject, body = delivery_core._build_confirmation_email(
        "2026-07-03", 0, 0, skipped=False, subscriber_query_failed=False
    )
    assert "Sent to 0 subscribers" in body
    assert "failed" not in body.lower()


def test_confirmation_skip_mode_wording_does_not_imply_real_send():
    subject, body = delivery_core._build_confirmation_email(
        "2026-07-03", 0, 0, skipped=True, subscriber_query_failed=False
    )
    assert "skipped" in body.lower()
    assert "validation run" in body.lower()
    assert "Sent to" not in body


def test_confirmation_query_failure_disambiguated_from_genuine_zero():
    subject, body = delivery_core._build_confirmation_email(
        "2026-07-03", 0, 0, skipped=False, subscriber_query_failed=True
    )
    assert "lookup failed" in body.lower()
    assert "Sent to 0 subscribers" not in body


def test_send_confirmation_email_sends_to_owner_from_sender():
    ses_client = FakeSesClient()

    delivery_core.send_confirmation_email(ses_client, "2026-07-03", 3, 0, skipped=False, subscriber_query_failed=False)

    assert len(ses_client.sent_to) == 1
    sent = ses_client.sent_to[0]
    assert sent["recipient"] == delivery_core.RECIP
    assert sent["source"] == delivery_core.SENDER
    assert "3 subscribers" in _plain_body_text(sent["raw"])


def test_send_confirmation_email_failure_is_swallowed_not_raised():
    ses_client = RaisesOnSend()

    # Must not raise.
    delivery_core.send_confirmation_email(ses_client, "2026-07-03", 2, 0, skipped=False, subscriber_query_failed=False)
