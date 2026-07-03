"""Unit tests for the welcome-send Lambda handler (docs/adr/0009's async invoke target).

Covers PRD instant-welcome-brief.md acceptance criteria: AC-4 (sent on first confirm,
with audio), AC-5 (graceful no-audio: pointer absent AND pointer-exists-but-object-gone),
AC-7 (cold start), plus the retry-on-SES-failure contract this handler is designed
around (ADR-0009).
"""

from __future__ import annotations

import email as email_lib
import json

import pytest

from conftest import import_handler

welcome_send = import_handler("welcome-send")


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


def test_welcome_email_with_audio_attached(briefs_bucket):
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
    assert "06:07 (Europe/Berlin)" in body
    assert "Today's brief" in body
    assert "curated and written by an AI agent" in body
    assert "unsub-tok" in body  # unsubscribe link present
    attachments = _attachment_parts(sent["raw"])
    assert len(attachments) == 1
    assert attachments[0].get_payload(decode=True) == b"fake-mp3-bytes"


def test_graceful_no_audio_when_pointer_absent(briefs_bucket):
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>")  # no pointer at all

    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "sub@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    sent = ses_client.sent[0]
    assert _attachment_parts(sent["raw"]) == []
    assert "Today's brief" in _html_body_text(sent["raw"])  # written body still sent


def test_graceful_no_audio_when_pointer_resolves_to_a_gone_object(briefs_bucket):
    # Pointer exists but the MP3 it names was never written (expired under the 7-day
    # audio/ lifecycle, or otherwise gone) -- AC-5's second no-audio case.
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>", audio_key="audio/.expired.mp3")

    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "sub@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    sent = ses_client.sent[0]
    assert _attachment_parts(sent["raw"]) == []
    assert "Today's brief" in _html_body_text(sent["raw"])


def test_oversized_audio_is_dropped_not_attached(briefs_bucket):
    # A pointer that resolves to a real, present object -- just one over the size cap
    # -- is treated the same as "no usable audio": written body still sent, no
    # attachment, no error (mirrors AC-5's other no-audio cases).
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today's brief</h1>", audio_key="audio/.huge.mp3")
    oversized = b"x" * (welcome_send.MAX_AUDIO_ATTACHMENT_BYTES + 1)
    briefs_bucket.put_object(Bucket=welcome_send.latest_brief.BUCKET, Key="audio/.huge.mp3", Body=oversized)

    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "sub@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    sent = ses_client.sent[0]
    assert _attachment_parts(sent["raw"]) == []
    assert "Today's brief" in _html_body_text(sent["raw"])


def test_cold_start_sends_welcome_only_no_brief_no_audio(briefs_bucket):
    # Empty store -- no brief has ever been archived.
    ses_client = FakeSesClient()
    resp = welcome_send._handle(
        {"email": "brandnew@example.com", "unsubscribeToken": "tok"}, ses_client, briefs_bucket
    )

    assert resp["sent"] is True
    sent = ses_client.sent[0]
    body = _html_body_text(sent["raw"])
    assert "Welcome to the Daily AI Brief!" in body
    assert "06:07 (Europe/Berlin)" in body
    assert "haven't published an edition yet" in body
    assert _attachment_parts(sent["raw"]) == []


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
