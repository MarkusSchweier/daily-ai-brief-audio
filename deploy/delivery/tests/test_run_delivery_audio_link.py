"""Integration-level tests for the audio-link wiring INSIDE `_run_delivery()`
(PRD docs/prd/audio-link-delivery.md, ADR-0017): proves `synthesize_audio()` ->
`resolve_audio_metadata()` -> `audio_link.build_audio_link_result()` ->
`send_all()` are actually chained correctly at the handler level, not just
correct in isolation (each already has its own direct unit tests --
test_synthesize_audio.py, test_audio_link.py, test_delivery_core_send_all.py).

Covers AC-8 (the sent email's audio line contains the real audio URL), AC-11
(a Polly failure degrades to the no-audio line, never blocks the send), and
AC-19/FR-17 (a single structured log line records whether a link was embedded).
"""

from __future__ import annotations

import email
import logging

import boto3
import pytest

import handler as handler_module


@pytest.fixture
def delivery_table(mocked_aws):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="brief-deliveries-audio-link-test",
        KeySchema=[{"AttributeName": "deliveryId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "deliveryId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    yield table


class _RecordingSesClient:
    def __init__(self):
        self.sent_to = []

    def send_raw_email(self, Source, Destinations, RawMessage):
        self.sent_to.append({"recipient": Destinations[0], "raw": RawMessage["Data"]})
        return {"MessageId": f"fake-{len(self.sent_to)}"}


class _EmptyDynamoDBClient:
    def get_paginator(self, name):
        class _Paginator:
            def paginate(self, **kwargs):
                return iter([{"Items": []}])

        return _Paginator()


class _NoOpSecretsManagerClient:
    def get_secret_value(self, SecretId):
        raise RuntimeError("no secret configured")


class _NoOpS3ClientForArchival:
    """put_object always raises (archival is best-effort and not the concern of
    these tests); head_object is monkeypatched per-test onto delivery_core
    directly instead of this client, since resolve_audio_metadata() takes
    whatever s3_client _run_delivery() passes it -- see each test below for how
    HeadObject behavior is injected."""

    def __init__(self, head_object_response=None, head_object_exc=None):
        self._head_object_response = head_object_response
        self._head_object_exc = head_object_exc
        self.head_object_calls = []

    def put_object(self, **kwargs):
        raise RuntimeError("no real S3 in this test")

    def head_object(self, Bucket, Key):
        self.head_object_calls.append((Bucket, Key))
        if self._head_object_exc is not None:
            raise self._head_object_exc
        return self._head_object_response


class _SucceedingPollyClient:
    def __init__(self, audio_key: str):
        self._audio_key = audio_key

    def start_speech_synthesis_task(self, **kwargs):
        return {"SynthesisTask": {"TaskId": "task-real"}}

    def get_speech_synthesis_task(self, TaskId):
        return {
            "SynthesisTask": {
                "TaskStatus": "completed",
                "OutputUri": f"https://s3.amazonaws.com/cowork-polly-tts-740353583786/{self._audio_key}",
            }
        }


class _FailingPollyClient:
    def start_speech_synthesis_task(self, **kwargs):
        raise RuntimeError("simulated Polly outage")


def _worker_event(delivery_id: str) -> dict:
    return {
        "_delivery_worker": True,
        "deliveryId": delivery_id,
        "body": {
            "contractVersion": 2,
            "brief_markdown": "# Test Brief\n\nSome content.",
            "listening_script": "This is the listening script.",
            "candidates": '{"considered": []}',
            "source_usage": '{"featured": []}',
            "metadata": {"email_subject": "Test Brief", "enable_subscriber_fanout": False, "brief_date": "2026-07-06"},
        },
    }


def _html_body_text(raw_mime: str) -> str:
    parsed = email.message_from_string(raw_mime)
    for part in parsed.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode("utf-8")
    return ""


def _brief_email_body(ses_client: _RecordingSesClient) -> str:
    """`_run_delivery()` sends TWO owner emails per successful run -- the brief
    itself (multipart/alternative, has an HTML part) and the separate short
    post-send confirmation (plain text only, `send_confirmation_email()`).
    Returns the HTML body of the FIRST one that has one -- the brief."""
    for entry in ses_client.sent_to:
        body = _html_body_text(entry["raw"])
        if body:
            return body
    raise AssertionError(f"no HTML-bearing email found among {ses_client.sent_to!r}")


def test_successful_audio_resolution_puts_the_real_link_in_the_sent_email(delivery_table, caplog):
    """AC-8: the sent email's audio line contains the real audio URL, built from
    the actual OutputUri-derived key -- and AC-19: a single structured log line
    records the link was embedded, with the expiry date used."""
    delivery_id = "delivery-audio-ok"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})
    ses_client = _RecordingSesClient()
    audio_key = "audio/.real-task-id.mp3"
    s3_client = _NoOpS3ClientForArchival(
        head_object_response={
            "Expiration": 'expiry-date="Tue, 04 Aug 2026 00:00:00 GMT", rule-id="expire-audio-7-days"'
        }
    )

    with caplog.at_level(logging.INFO):
        result = handler_module._handle_worker_invocation(
            _worker_event(delivery_id),
            table=delivery_table,
            polly_client=_SucceedingPollyClient(audio_key),
            s3_client=s3_client,
            ses_client=ses_client,
            dynamodb_client=_EmptyDynamoDBClient(),
            secretsmanager_client=_NoOpSecretsManagerClient(),
        )

    assert result["status"] == "succeeded"
    # Owner's brief + the separate post-send confirmation email (fanout disabled).
    assert len(ses_client.sent_to) == 2
    body = _brief_email_body(ses_client)
    assert f"https://briefing.mschweier.com/{audio_key}" in body
    assert "4 August 2026" in body
    assert "Listen to this brief" in body

    row = delivery_table.get_item(Key={"deliveryId": delivery_id})["Item"]
    import json

    summary = json.loads(row["summary"])
    assert summary["audio_link_included"] is True
    assert summary["audio_link_expiry"] == "4 August 2026"

    assert any("AUDIO_LINK_INCLUDED" in r.message for r in caplog.records)


def test_polly_failure_degrades_to_no_audio_line_and_still_sends(delivery_table, caplog):
    """AC-11: a Polly synthesis failure never blocks the send -- the email still
    goes out, with the no-audio line and no <a> tag."""
    import audio_link

    delivery_id = "delivery-audio-failed"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})
    ses_client = _RecordingSesClient()
    s3_client = _NoOpS3ClientForArchival()

    with caplog.at_level(logging.INFO):
        result = handler_module._handle_worker_invocation(
            _worker_event(delivery_id),
            table=delivery_table,
            polly_client=_FailingPollyClient(),
            s3_client=s3_client,
            ses_client=ses_client,
            dynamodb_client=_EmptyDynamoDBClient(),
            secretsmanager_client=_NoOpSecretsManagerClient(),
        )

    assert result["status"] == "succeeded"
    assert len(ses_client.sent_to) == 2
    body = _brief_email_body(ses_client)
    assert audio_link.NO_AUDIO_LINE_HTML in body
    assert "Listen to this brief" not in body
    # HeadObject must never even be attempted -- there is no key to look up.
    assert s3_client.head_object_calls == []

    import json

    summary = json.loads(delivery_table.get_item(Key={"deliveryId": delivery_id})["Item"]["summary"])
    assert summary["audio_ok"] is False
    assert summary["audio_link_included"] is False
    assert summary["audio_link_expiry"] is None

    assert any("AUDIO_LINK_OMITTED" in r.message for r in caplog.records)


def test_head_object_404_degrades_to_no_audio_line_even_though_synthesis_succeeded(delivery_table):
    """D1's table row 3: synthesis succeeded (audio_ok=True) but the object is
    somehow not reachable via HeadObject (e.g. a race, or an already-expired
    object) -- still degrades to the no-audio line, never a known-broken link."""
    import audio_link

    delivery_id = "delivery-audio-headobject-404"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})
    ses_client = _RecordingSesClient()
    s3_client = _NoOpS3ClientForArchival(head_object_exc=RuntimeError("simulated NoSuchKey"))

    result = handler_module._handle_worker_invocation(
        _worker_event(delivery_id),
        table=delivery_table,
        polly_client=_SucceedingPollyClient("audio/.gone.mp3"),
        s3_client=s3_client,
        ses_client=ses_client,
        dynamodb_client=_EmptyDynamoDBClient(),
        secretsmanager_client=_NoOpSecretsManagerClient(),
    )

    assert result["status"] == "succeeded"
    body = _brief_email_body(ses_client)
    assert audio_link.NO_AUDIO_LINE_HTML in body


def test_expiration_header_missing_uses_the_generic_availability_note(delivery_table):
    """AC-12: object confirmed present (HeadObject 200) but no parseable
    Expiration header -- link shown, generic note instead of a date."""
    delivery_id = "delivery-audio-no-expiration"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})
    ses_client = _RecordingSesClient()
    audio_key = "audio/.no-expiry.mp3"
    s3_client = _NoOpS3ClientForArchival(head_object_response={})

    result = handler_module._handle_worker_invocation(
        _worker_event(delivery_id),
        table=delivery_table,
        polly_client=_SucceedingPollyClient(audio_key),
        s3_client=s3_client,
        ses_client=ses_client,
        dynamodb_client=_EmptyDynamoDBClient(),
        secretsmanager_client=_NoOpSecretsManagerClient(),
    )

    assert result["status"] == "succeeded"
    body = _brief_email_body(ses_client)
    assert f"https://briefing.mschweier.com/{audio_key}" in body
    assert "link available for about 7 days" in body


def test_oversized_mp3_is_still_linked_no_size_cap_in_the_daily_email(delivery_table):
    """AC-13, at the DAILY-email layer (the welcome-side equivalent is
    test_welcome_send_handler.py::test_large_audio_object_is_still_linked_no_size_cap
    -- this is its missing daily-path counterpart, review gap). ADR-0017 D8 deleted
    MAX_AUDIO_ATTACHMENT_BYTES and the whole attachment/size-cutoff branch entirely;
    resolve_audio_metadata() only ever HeadObjects for Expiration, never gates on
    ContentLength. Simulates a HeadObject response for an MP3 far past the old 15 MB
    attachment cutoff, and proves the link is shown regardless -- the old
    silent-drop-past-15MB failure mode is structurally gone, not just untriggered."""
    delivery_id = "delivery-audio-oversized"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})
    ses_client = _RecordingSesClient()
    audio_key = "audio/.oversized.mp3"
    oversized_bytes = 20 * 1024 * 1024  # 20 MB, comfortably past the old 15 MB cutoff
    s3_client = _NoOpS3ClientForArchival(
        head_object_response={
            "ContentLength": oversized_bytes,
            "Expiration": 'expiry-date="Tue, 04 Aug 2026 00:00:00 GMT", rule-id="expire-audio-7-days"',
        }
    )

    result = handler_module._handle_worker_invocation(
        _worker_event(delivery_id),
        table=delivery_table,
        polly_client=_SucceedingPollyClient(audio_key),
        s3_client=s3_client,
        ses_client=ses_client,
        dynamodb_client=_EmptyDynamoDBClient(),
        secretsmanager_client=_NoOpSecretsManagerClient(),
    )

    assert result["status"] == "succeeded"
    body = _brief_email_body(ses_client)
    assert f"https://briefing.mschweier.com/{audio_key}" in body
    assert "Listen to this brief" in body
    assert "4 August 2026" in body
    # No attachment part regardless of the (irrelevant) object size.
    import email as email_module

    for entry in ses_client.sent_to:
        parsed = email_module.message_from_string(entry["raw"])
        assert [p for p in parsed.walk() if p.get_content_disposition() == "attachment"] == []
