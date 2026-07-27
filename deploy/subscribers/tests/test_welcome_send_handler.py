"""Unit tests for the welcome-send Lambda handler (docs/adr/0009's async invoke target).

Covers PRD instant-welcome-brief.md acceptance criteria: AC-4 (sent on first confirm,
with audio), AC-5 (graceful no-audio: pointer absent AND pointer-exists-but-object-gone),
AC-7 (cold start), plus the retry-on-SES-failure contract this handler is designed
around (ADR-0009).
"""

from __future__ import annotations

import email as email_lib
import importlib.util
import json
import os
import sys

import pytest

import feedback_token
from conftest import FUNCTIONS_DIR, import_handler

welcome_send = import_handler("welcome-send")


def _load_welcome_send_with_feedback_config(feedback_base_url="", feedback_secret_arn=""):
    """Load a FRESH instance of welcome-send/handler.py with its own
    FEEDBACK_BASE_URL / FEEDBACK_TOKEN_SECRET_ARN env vars (these are read as
    module-level constants at import time, so the shared `welcome_send` module above,
    imported once with no feedback config, can't be reused for these tests)."""
    old_base_url = os.environ.get("FEEDBACK_BASE_URL")
    old_secret_arn = os.environ.get("FEEDBACK_TOKEN_SECRET_ARN")
    os.environ["FEEDBACK_BASE_URL"] = feedback_base_url
    os.environ["FEEDBACK_TOKEN_SECRET_ARN"] = feedback_secret_arn
    module_name = f"welcome_send_feedback_test_under_test_{id(feedback_base_url)}_{id(feedback_secret_arn)}"
    try:
        handler_path = FUNCTIONS_DIR / "welcome-send" / "handler.py"
        spec = importlib.util.spec_from_file_location(module_name, handler_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if old_base_url is None:
            os.environ.pop("FEEDBACK_BASE_URL", None)
        else:
            os.environ["FEEDBACK_BASE_URL"] = old_base_url
        if old_secret_arn is None:
            os.environ.pop("FEEDBACK_TOKEN_SECRET_ARN", None)
        else:
            os.environ["FEEDBACK_TOKEN_SECRET_ARN"] = old_secret_arn


class FakeSesClient:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.sent = []

    def send_raw_email(self, Source, Destinations, RawMessage):
        if self.should_fail:
            raise RuntimeError("simulated SES outage")
        self.sent.append({"source": Source, "recipient": Destinations[0], "raw": RawMessage["Data"]})
        return {"MessageId": "fake-message-id"}


def _html_body_text(raw_mime: str) -> str:
    parsed = email_lib.message_from_string(raw_mime)
    for part in parsed.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode("utf-8")
    return ""


def _attachment_parts(raw_mime: str):
    parsed = email_lib.message_from_string(raw_mime)
    return [part for part in parsed.walk() if part.get_content_disposition() == "attachment"]


def _put_brief(s3_client, date, html, audio_key=None):
    s3_client.put_object(Bucket=welcome_send.latest_brief.BUCKET, Key=f"briefs/{date}/brief.html", Body=html.encode("utf-8"))
    if audio_key is not None:
        s3_client.put_object(
            Bucket=welcome_send.latest_brief.BUCKET,
            Key=f"briefs/{date}/{welcome_send.latest_brief.AUDIO_POINTER_FILENAME}",
            Body=json.dumps({"audio_key": audio_key}).encode("utf-8"),
        )


def test_welcome_email_with_audio_link(briefs_bucket):
    """PRD docs/prd/audio-link-delivery.md AC-15: the welcome email carries the
    audio LINK (never an attachment), as the meta-box line directly after the
    welcome intro (ADR-0017 D11 amendment)."""
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>", audio_key="audio/.today.mp3")
    briefs_bucket.put_object(Bucket=welcome_send.latest_brief.BUCKET, Key="audio/.today.mp3", Body=b"fake-mp3-bytes")

    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "New.Sub@Example.com", "firstName": "New", "unsubscribeToken": "unsub-tok"},
        ses_client,
        briefs_bucket,
    )

    assert resp["sent"] is True
    assert len(ses_client.sent) == 1
    sent = ses_client.sent[0]
    assert sent["recipient"] == "new.sub@example.com"
    assert sent["source"] == welcome_send.SENDER
    body = _html_body_text(sent["raw"])
    assert "Welcome to the Daily AI Brief!" in body
    assert "every weekday in the morning" in body  # owner copy (no specific time)
    assert "Today's brief" in body
    # The welcome mail carries the WELCOME variant of the daily chrome
    # (owner-specified format, 2026-07-08; amended 2026-07-27 by ADR-0017 D11):
    # intro -> audio -> unsubscribe -> feedback -> disclaimer -- deliberately NO
    # forward/subscribe line (the recipient just subscribed; that line belongs to
    # the regular daily brief only).
    assert "subscribe here" not in body
    assert "curated and written by an AI agent" in body
    assert "unsub-tok" in body  # unsubscribe link present
    assert "Listen to this brief" in body
    assert f"{welcome_send.AUDIO_BASE_URL}/audio/.today.mp3" in body
    # Ordering: greeting first, audio directly after (D11), then unsubscribe, then disclaimer.
    assert (
        body.index("Welcome to the Daily AI Brief!")
        < body.index("Listen to this brief")
        < body.index("unsub-tok")
        < body.index("curated and written by an AI agent")
    )
    # No MIME attachment -- ever (ADR-0017 D8: the attachment machinery is deleted).
    assert _attachment_parts(sent["raw"]) == []


def test_welcome_email_audio_link_generic_note_when_expiration_unreadable(briefs_bucket):
    """AC-12 (applied to the welcome path): the object is confirmed present via
    HeadObject, but no parseable Expiration header comes back (moto's HeadObject
    does not simulate the x-amz-expiration header even with a configured
    lifecycle rule) -- the link is still shown, with the generic availability
    note rather than a date."""
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>", audio_key="audio/.today.mp3")
    briefs_bucket.put_object(Bucket=welcome_send.latest_brief.BUCKET, Key="audio/.today.mp3", Body=b"fake-mp3-bytes")

    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "sub@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    body = _html_body_text(ses_client.sent[0]["raw"])
    assert "Listen to this brief" in body
    assert "link available for about 7 days" in body


def test_welcome_email_shows_the_audio_objects_real_remaining_expiry_not_confirmation_plus_seven(
    briefs_bucket, monkeypatch
):
    """AC-15: the welcome email sends the LATEST archived edition, whose audio may
    be up to seven days into ITS OWN lifecycle -- the displayed expiry must be
    that specific object's real remaining expiry, never "confirmation date + 7".
    Exercised here against an object whose real `Expiration` header (injected via
    `_resolve_audio_metadata`, since moto cannot simulate S3's real
    x-amz-expiration header -- see the delivery unit's equivalent tests for the
    same limitation) reflects only 2 days of remaining life, as if it had been
    created 5 days ago."""
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>", audio_key="audio/.5-days-old.mp3")
    briefs_bucket.put_object(
        Bucket=welcome_send.latest_brief.BUCKET, Key="audio/.5-days-old.mp3", Body=b"fake-mp3-bytes"
    )
    monkeypatch.setattr(
        welcome_send,
        "_resolve_audio_metadata",
        lambda s3_client, audio_key: (
            True,
            'expiry-date="Wed, 29 Jul 2026 00:00:00 GMT", rule-id="expire-audio-7-days"',
        ),
    )

    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "sub@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    body = _html_body_text(ses_client.sent[0]["raw"])
    assert "link expires 29 July 2026" in body


def test_large_audio_object_is_still_linked_no_size_cap(briefs_bucket):
    """FR-11's principle applied to the welcome path too: there is no
    message-size risk in a link, so a real, present audio object of any size is
    linked -- there is no attachment-size cap left to trigger (ADR-0017 D8 deleted
    the old `MAX_AUDIO_ATTACHMENT_BYTES` machinery entirely; HeadObject-based
    resolution never even reads the object's bytes, only its metadata)."""
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>", audio_key="audio/.big.mp3")
    briefs_bucket.put_object(Bucket=welcome_send.latest_brief.BUCKET, Key="audio/.big.mp3", Body=b"x" * (1024 * 1024))

    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "sub@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    body = _html_body_text(ses_client.sent[0]["raw"])
    assert "Listen to this brief" in body
    assert _attachment_parts(ses_client.sent[0]["raw"]) == []


def test_graceful_no_audio_when_pointer_absent(briefs_bucket):
    """AC-14 (no pointer at all): the no-audio line is shown -- no attachment,
    no broken link."""
    import audio_link

    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>")  # no pointer at all

    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "sub@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    sent = ses_client.sent[0]
    body = _html_body_text(sent["raw"])
    assert _attachment_parts(sent["raw"]) == []
    assert "Today's brief" in body  # written body still sent
    assert audio_link.NO_AUDIO_LINE_HTML in body
    assert "Listen to this brief" not in body


def test_graceful_no_audio_when_pointer_resolves_to_a_gone_object(briefs_bucket):
    """AC-16: pointer exists but the MP3 it names was never written (expired
    under the 7-day audio/ lifecycle, or otherwise gone) -- the welcome email is
    still sent, with the no-audio line and no broken link."""
    import audio_link

    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>", audio_key="audio/.expired.mp3")

    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "sub@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    sent = ses_client.sent[0]
    body = _html_body_text(sent["raw"])
    assert _attachment_parts(sent["raw"]) == []
    assert "Today's brief" in body
    assert audio_link.NO_AUDIO_LINE_HTML in body
    assert "Listen to this brief" not in body


def test_cold_start_sends_welcome_only_no_brief_no_audio(briefs_bucket):
    """AC-17: the cold-start case shows NO audio line at all -- not the link,
    and not even the no-audio fallback line -- exactly as today."""
    import audio_link

    # Empty store -- no brief has ever been archived.
    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "brandnew@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    sent = ses_client.sent[0]
    body = _html_body_text(sent["raw"])
    assert "Welcome to the Daily AI Brief!" in body
    assert "every weekday in the morning" in body  # owner copy (no specific time)
    assert "haven't published an edition yet" in body
    assert _attachment_parts(sent["raw"]) == []
    assert "Listen to this brief" not in body
    assert audio_link.NO_AUDIO_LINE_HTML not in body


def test_missing_email_or_token_is_a_permanent_no_op_not_an_exception(briefs_bucket):
    ses_client = FakeSesClient()

    resp = welcome_send._handle({"unsubscribeToken": "tok"}, ses_client, briefs_bucket)
    assert resp["sent"] is False
    assert ses_client.sent == []

    resp = welcome_send._handle({"email": "sub@example.com"}, ses_client, briefs_bucket)
    assert resp["sent"] is False
    assert ses_client.sent == []


def test_ses_send_failure_is_logged_and_reraised_for_async_invoke_retry(briefs_bucket, caplog):
    """ADR-0009: this Lambda is invoked InvocationType='Event', so a raised exception
    here is what gives a transient SES failure Lambda's automatic retries and an
    on-failure destination -- swallowing it here would silently defeat that design."""
    ses_client = FakeSesClient(should_fail=True)

    with caplog.at_level("ERROR", logger=welcome_send.logger.name):
        with pytest.raises(RuntimeError, match="simulated SES outage"):
            welcome_send._handle({"email": "sub@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket)

    assert "WELCOME_SEND_FAILED" in caplog.text


# ---------------------------------------------------------------------------
# Feedback link (docs/prd/reader-feedback.md FR-5/AC-6, ADR-0011, ADR-0012 §B): a
# fresh module instance per test (see _load_welcome_send_with_feedback_config above)
# so each test can set its own FEEDBACK_BASE_URL / FEEDBACK_TOKEN_SECRET_ARN.
# ---------------------------------------------------------------------------

FEEDBACK_SIGNING_SECRET = "test-feedback-signing-secret"


@pytest.fixture
def feedback_secret(mocked_aws):
    """A mocked Secrets Manager secret; yields its real (moto-assigned) ARN."""
    import boto3

    client = boto3.client("secretsmanager", region_name="us-east-1")
    client.create_secret(Name="feedback-test-secret", SecretString=FEEDBACK_SIGNING_SECRET)
    described = client.describe_secret(SecretId="feedback-test-secret")
    yield described["ARN"]


def test_feedback_link_present_when_config_valid(briefs_bucket, feedback_secret):
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>")
    welcome_send_with_feedback = _load_welcome_send_with_feedback_config(
        feedback_base_url="https://feedback.mschweier.com", feedback_secret_arn=feedback_secret
    )

    ses_client = FakeSesClient()
    resp = welcome_send_with_feedback._handle(
        {"email": "reader@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    body = _html_body_text(ses_client.sent[0]["raw"])
    assert "feedback.mschweier.com/?t=" in body
    assert "Share feedback" in body

    secret = welcome_send_with_feedback._get_feedback_signing_secret()
    token = body.split("/?t=")[1].split('"')[0]
    result = feedback_token.validate(secret, token)
    assert result.valid is True
    assert result.identity == "reader@example.com"
    assert result.brief_date == "2026-07-03"


def test_feedback_link_gracefully_absent_when_config_missing_send_still_succeeds(briefs_bucket):
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>")
    welcome_send_no_feedback = _load_welcome_send_with_feedback_config(
        feedback_base_url="", feedback_secret_arn=""
    )

    ses_client = FakeSesClient()
    resp = welcome_send_no_feedback._handle(
        {"email": "reader@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    body = _html_body_text(ses_client.sent[0]["raw"])
    assert "Share feedback" not in body
    assert "/?t=" not in body


def test_feedback_link_absent_on_cold_start_no_edition_to_attribute(briefs_bucket, feedback_secret):
    # Empty store -- cold start, no brief date exists to attribute feedback to.
    welcome_send_with_feedback = _load_welcome_send_with_feedback_config(
        feedback_base_url="https://feedback.mschweier.com", feedback_secret_arn=feedback_secret
    )

    ses_client = FakeSesClient()
    resp = welcome_send_with_feedback._handle(
        {"email": "reader@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    body = _html_body_text(ses_client.sent[0]["raw"])
    assert "Share feedback" not in body


def test_feedback_link_uses_correct_recipient_identity(briefs_bucket, feedback_secret):
    _put_brief(briefs_bucket, "2026-07-02", "<h1>Yesterday's brief</h1>")
    welcome_send_with_feedback = _load_welcome_send_with_feedback_config(
        feedback_base_url="https://feedback.mschweier.com", feedback_secret_arn=feedback_secret
    )

    ses_client = FakeSesClient()
    welcome_send_with_feedback._handle(
        {"email": "New.Sub@Example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    secret = welcome_send_with_feedback._get_feedback_signing_secret()
    body = _html_body_text(ses_client.sent[0]["raw"])
    token = body.split("/?t=")[1].split('"')[0]
    result = feedback_token.validate(secret, token)
    assert result.valid is True
    # normalize_email() lowercases -- the identity in the token must match the
    # normalized recipient, not the raw event payload casing.
    assert result.identity == "new.sub@example.com"


def test_feedback_link_generation_failure_never_blocks_send(briefs_bucket, feedback_secret, monkeypatch):
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>")
    welcome_send_with_feedback = _load_welcome_send_with_feedback_config(
        feedback_base_url="https://feedback.mschweier.com", feedback_secret_arn=feedback_secret
    )
    monkeypatch.setattr(
        welcome_send_with_feedback.feedback_token,
        "generate",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    ses_client = FakeSesClient()
    resp = welcome_send_with_feedback._handle(
        {"email": "reader@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    body = _html_body_text(ses_client.sent[0]["raw"])
    assert "Share feedback" not in body
