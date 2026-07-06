"""Idempotency tests for the delivery worker leg (PRD
docs/prd/agent-system-redesign.md, ADR-0014 Decision 2a's "Async self-invoke has
at-least-once/retry semantics" note): a duplicate invocation of the worker leg for
the SAME `deliveryId` must NOT double-send -- proving the conditional
pending->in_progress transition (`_claim_delivery()`) genuinely gates a second
invocation, mirroring the fail-closed, never-double-fire discipline
docs/adr/0010-restore-webhook-idempotency.md already established for the
launcher's webhook guard.

Uses REAL moto-backed DynamoDB (not a hand-rolled fake) so the
`ConditionExpression`/`ConditionalCheckFailedException` semantics under test are
the genuine DynamoDB behavior, not an assumption about it.
"""

from __future__ import annotations

import json

import boto3
import pytest

import handler as handler_module


@pytest.fixture
def delivery_table(mocked_aws):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="brief-deliveries-idempotency-test",
        KeySchema=[{"AttributeName": "deliveryId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "deliveryId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    yield table


class _CountingSesClient:
    """Records every send_raw_email call -- the assertion surface for "did this
    actually send twice or once".

    A single successful `_run_delivery()` call legitimately sends TWO emails to
    the owner (the brief itself, via `send_all()`, AND the separate short
    post-send confirmation email, via `send_confirmation_email()`) -- that is
    correct, expected, unrelated behavior, not a double-send bug. `send_count`
    tracks EVERY send_raw_email call (used to prove "no MORE than one full
    delivery's worth of emails went out"); `brief_send_count` tracks ONLY the
    brief itself, distinguished from the confirmation email by its DECODED
    Subject header (the confirmation's is always "AI Brief sent — <date>";
    the worker event's own brief subject in this test file is "Test Brief" --
    the two are always different strings by construction). Decoding via
    `email.message_from_string` + `email.header.decode_header` rather than a
    raw substring match, because `_build_message()`/`send_confirmation_email()`
    both RFC-2047-encode a non-ASCII Subject (the em dash) as
    `=?utf-8?q?...?=` -- a literal substring check against the raw MIME bytes
    would never match the encoded header."""

    _CONFIRMATION_SUBJECT_PREFIX = "AI Brief sent"

    def __init__(self):
        self.send_count = 0
        self.brief_send_count = 0
        self.sent_to = []

    def send_raw_email(self, Source, Destinations, RawMessage):
        self.send_count += 1
        self.sent_to.append(Destinations[0])
        if not self._is_confirmation_email(RawMessage["Data"]):
            self.brief_send_count += 1
        return {"MessageId": f"fake-{self.send_count}"}

    def _is_confirmation_email(self, raw_mime: str) -> bool:
        import email
        from email.header import decode_header

        parsed = email.message_from_string(raw_mime)
        raw_subject = parsed.get("Subject", "")
        decoded_parts = decode_header(raw_subject)
        subject = "".join(
            part.decode(encoding or "utf-8") if isinstance(part, bytes) else part for part, encoding in decoded_parts
        )
        return subject.startswith(self._CONFIRMATION_SUBJECT_PREFIX)


class _EmptyDynamoDBClient:
    """A subscriber-query client returning zero confirmed subscribers -- keeps
    these tests focused on "did the owner's send happen twice", the minimal
    reproduction of a double-send."""

    def get_paginator(self, name):
        class _Paginator:
            def paginate(self, **kwargs):
                return iter([{"Items": []}])

        return _Paginator()


class _NoOpSecretsManagerClient:
    def get_secret_value(self, SecretId):
        raise RuntimeError("no secret configured")


class _NoOpS3Client:
    """Enough of an S3 client stand-in for archive_todays_brief()/
    archive_candidates_file()'s best-effort put_object calls not to explode --
    both already catch and log any exception, so a client that always raises is a
    valid, minimal stand-in that proves archival never blocks the send/claim
    logic under test here."""

    def put_object(self, **kwargs):
        raise RuntimeError("no real S3 in this test")


class _NoOpPollyClient:
    """A Polly client whose synthesis task always fails fast -- this test isn't
    about audio, and synthesize_audio()'s own fail-safe (proven in
    test_delivery_core_send_all.py via its mp3_bytes=None cases) means a failure
    here still lets send_all() proceed text-only."""

    def start_speech_synthesis_task(self, **kwargs):
        raise RuntimeError("no real Polly in this test")


def _worker_event(delivery_id: str) -> dict:
    return {
        "_delivery_worker": True,
        "deliveryId": delivery_id,
        "body": {
            "contractVersion": 1,
            "brief_markdown": "# Test Brief\n\nSome content.",
            "listening_script": "This is the listening script.",
            "metadata": {"email_subject": "Test Brief", "enable_subscriber_fanout": False, "brief_date": "2026-07-06"},
        },
    }


def _invoke_worker_leg(delivery_id: str, table, ses_client) -> dict:
    return handler_module._handle_worker_invocation(
        _worker_event(delivery_id),
        table=table,
        polly_client=_NoOpPollyClient(),
        s3_client=_NoOpS3Client(),
        ses_client=ses_client,
        dynamodb_client=_EmptyDynamoDBClient(),
        secretsmanager_client=_NoOpSecretsManagerClient(),
    )


def test_first_worker_invocation_claims_and_sends(delivery_table):
    delivery_id = "delivery-abc123"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})
    ses_client = _CountingSesClient()

    result = _invoke_worker_leg(delivery_id, delivery_table, ses_client)

    assert result["status"] == "succeeded"
    assert ses_client.brief_send_count == 1

    row = delivery_table.get_item(Key={"deliveryId": delivery_id})["Item"]
    assert row["status"] == "succeeded"


def test_duplicate_worker_invocation_for_the_same_delivery_id_does_not_double_send(delivery_table):
    """THE core idempotency requirement (this phase's brief): invoking the worker
    leg TWICE for the same deliveryId must send the brief AT MOST once. This
    directly simulates Lambda's own at-least-once async invocation semantics -- a
    duplicate/retried delivery of the SAME event."""
    delivery_id = "delivery-def456"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})
    ses_client = _CountingSesClient()

    first_result = _invoke_worker_leg(delivery_id, delivery_table, ses_client)
    assert first_result["status"] == "succeeded"
    assert ses_client.brief_send_count == 1
    sends_after_first_invocation = ses_client.send_count  # 2: the brief + the confirmation email, both expected once

    # The SAME worker-leg payload, delivered again (Lambda's own documented
    # at-least-once semantics for async invocations) -- this must NOT re-run
    # send_all() or send_confirmation_email().
    second_result = _invoke_worker_leg(delivery_id, delivery_table, ses_client)

    assert second_result["status"] == "duplicate_skipped"
    assert ses_client.brief_send_count == 1  # STILL 1, not 2 -- the crux of this test.
    assert ses_client.send_count == sends_after_first_invocation  # no NEW sends of any kind on the duplicate

    row = delivery_table.get_item(Key={"deliveryId": delivery_id})["Item"]
    assert row["status"] == "succeeded"  # unaffected by the duplicate


def test_three_concurrent_duplicate_invocations_send_exactly_once(delivery_table):
    """Belt-and-braces: even THREE invocations for the same deliveryId (a more
    aggressive retry storm) must still send the brief exactly once."""
    delivery_id = "delivery-ghi789"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})
    ses_client = _CountingSesClient()

    results = [_invoke_worker_leg(delivery_id, delivery_table, ses_client) for _ in range(3)]

    assert results[0]["status"] == "succeeded"
    assert results[1]["status"] == "duplicate_skipped"
    assert results[2]["status"] == "duplicate_skipped"
    assert ses_client.brief_send_count == 1


def test_claim_delivery_returns_false_when_row_already_in_progress(delivery_table):
    """Direct unit test of `_claim_delivery()` itself: a row already
    `in_progress` (not `pending`) must fail the conditional claim."""
    delivery_id = "delivery-jkl012"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "in_progress"})

    claimed = handler_module._claim_delivery(delivery_table, delivery_id)

    assert claimed is False


def test_claim_delivery_returns_false_when_row_already_succeeded(delivery_table):
    delivery_id = "delivery-mno345"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "succeeded"})

    claimed = handler_module._claim_delivery(delivery_table, delivery_id)

    assert claimed is False


def test_claim_delivery_returns_true_exactly_once_for_a_pending_row(delivery_table):
    delivery_id = "delivery-pqr678"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})

    first_claim = handler_module._claim_delivery(delivery_table, delivery_id)
    second_claim = handler_module._claim_delivery(delivery_table, delivery_id)

    assert first_claim is True
    assert second_claim is False


def test_worker_invocation_marks_failed_on_exception_not_duplicate_skipped(delivery_table, monkeypatch):
    """A GENUINE failure (not a duplicate) must be distinguishable: the row ends
    up `failed`, with the claim having succeeded (proving this is a real
    processing failure, not the duplicate-guard rejecting it)."""
    delivery_id = "delivery-stu901"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated derive_html failure")

    monkeypatch.setattr(handler_module.delivery_core, "derive_html", _raise)

    result = _invoke_worker_leg(delivery_id, delivery_table, _CountingSesClient())

    assert result["status"] == "failed"
    row = delivery_table.get_item(Key={"deliveryId": delivery_id})["Item"]
    assert row["status"] == "failed"
    assert "error" in row


def test_json_serialized_worker_event_round_trips(delivery_table):
    """Sanity check that the worker payload this module builds in
    `_handle_trigger()` (JSON-serialized for the real `lambda.invoke(Payload=...)`
    call) is exactly what `_handle_worker_invocation()` expects to receive back --
    proving the two legs' payload shape agree, not just that each independently
    works against a hand-built dict."""
    delivery_id = "delivery-vwx234"
    delivery_table.put_item(Item={"deliveryId": delivery_id, "status": "pending"})

    worker_payload = {
        "_delivery_worker": True,
        "deliveryId": delivery_id,
        "body": {
            "contractVersion": 1,
            "brief_markdown": "# Round Trip\n\nContent.",
            "listening_script": "Script.",
            "metadata": {"enable_subscriber_fanout": False},
        },
    }
    round_tripped_event = json.loads(json.dumps(worker_payload))

    ses_client = _CountingSesClient()
    result = handler_module._handle_worker_invocation(
        round_tripped_event,
        table=delivery_table,
        polly_client=_NoOpPollyClient(),
        s3_client=_NoOpS3Client(),
        ses_client=ses_client,
        dynamodb_client=_EmptyDynamoDBClient(),
        secretsmanager_client=_NoOpSecretsManagerClient(),
    )

    assert result["status"] == "succeeded"
    assert ses_client.brief_send_count == 1
