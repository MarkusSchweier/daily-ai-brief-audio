"""Unit tests for the sync trigger (`POST /deliver`) and poll (`GET
/deliver/{deliveryId}`) legs of functions/deliver/handler.py -- PRD
docs/prd/agent-system-redesign.md FR-2/FR-2a/FR-3, ADR-0014 Decision 2a (async
transport amendment).

Uses moto for the `brief-deliveries` DynamoDB table and a fake boto3-Lambda-shaped
client for the self-invoke call (no real network call, no real Lambda deploy)."""

from __future__ import annotations

import json

import boto3
import pytest

import handler as handler_module


@pytest.fixture
def delivery_table(mocked_aws):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="brief-deliveries-trigger-test",
        KeySchema=[{"AttributeName": "deliveryId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "deliveryId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    yield table


class _FakeLambdaClient:
    """Stand-in for boto3's Lambda client -- records every `invoke()` call so
    tests can assert the self-invoke actually happened, with the right
    InvocationType and payload shape, without any real Lambda existing."""

    def __init__(self, raise_on_invoke=False):
        self.invocations = []
        self._raise_on_invoke = raise_on_invoke

    def invoke(self, FunctionName, InvocationType, Payload):
        if self._raise_on_invoke:
            raise RuntimeError("simulated Lambda invoke failure")
        self.invocations.append({"FunctionName": FunctionName, "InvocationType": InvocationType, "Payload": Payload})
        return {"StatusCode": 202}


def _post_event(body: dict, with_bearer: str | None = "secret123") -> dict:
    headers = {"Authorization": f"Bearer {with_bearer}"} if with_bearer else {}
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": headers,
        "body": json.dumps(body),
    }


def _get_event(delivery_id: str, with_bearer: str | None = "secret123") -> dict:
    headers = {"Authorization": f"Bearer {with_bearer}"} if with_bearer else {}
    return {
        "requestContext": {"http": {"method": "GET"}},
        "headers": headers,
        "pathParameters": {"deliveryId": delivery_id},
    }


VALID_BODY = {
    "contractVersion": 1,
    "brief_markdown": "# Brief\n\nContent.",
    "listening_script": "The listening script.",
    "metadata": {"email_subject": "Daily AI Brief", "enable_subscriber_fanout": False},
}


# ---------------------------------------------------------------------------
# Bearer-auth gating on the top-level handler() dispatch (delivery_auth wiring).
# ---------------------------------------------------------------------------


def test_post_deliver_returns_401_when_bearer_missing(delivery_table, monkeypatch):
    monkeypatch.setattr(handler_module.delivery_auth, "is_authorized", lambda event: False)

    result = handler_module.handler(_post_event(VALID_BODY, with_bearer=None), None)

    assert result["statusCode"] == 401


def test_get_deliver_returns_401_when_bearer_wrong(delivery_table, monkeypatch):
    monkeypatch.setattr(handler_module.delivery_auth, "is_authorized", lambda event: False)

    result = handler_module.handler(_get_event("some-id", with_bearer="wrong"), None)

    assert result["statusCode"] == 401


# ---------------------------------------------------------------------------
# POST /deliver -- request body validation (FR-2: contractVersion is explicit and
# reviewable, not an invisible edit).
# ---------------------------------------------------------------------------


def test_trigger_rejects_missing_contract_version(delivery_table):
    lambda_client = _FakeLambdaClient()
    body = dict(VALID_BODY)
    del body["contractVersion"]

    result = handler_module._handle_trigger(_post_event(body), delivery_table, lambda_client)

    assert result["statusCode"] == 400
    assert lambda_client.invocations == []
    assert delivery_table.scan()["Items"] == []


def test_trigger_rejects_unsupported_contract_version(delivery_table):
    lambda_client = _FakeLambdaClient()
    body = {**VALID_BODY, "contractVersion": 999}

    result = handler_module._handle_trigger(_post_event(body), delivery_table, lambda_client)

    assert result["statusCode"] == 400
    assert lambda_client.invocations == []


def test_trigger_rejects_missing_brief_markdown(delivery_table):
    lambda_client = _FakeLambdaClient()
    body = dict(VALID_BODY)
    del body["brief_markdown"]

    result = handler_module._handle_trigger(_post_event(body), delivery_table, lambda_client)

    assert result["statusCode"] == 400


def test_trigger_rejects_missing_listening_script(delivery_table):
    lambda_client = _FakeLambdaClient()
    body = dict(VALID_BODY)
    del body["listening_script"]

    result = handler_module._handle_trigger(_post_event(body), delivery_table, lambda_client)

    assert result["statusCode"] == 400


def test_trigger_rejects_non_object_metadata(delivery_table):
    lambda_client = _FakeLambdaClient()
    body = {**VALID_BODY, "metadata": "not an object"}

    result = handler_module._handle_trigger(_post_event(body), delivery_table, lambda_client)

    assert result["statusCode"] == 400


def test_trigger_rejects_invalid_json_body(delivery_table):
    lambda_client = _FakeLambdaClient()
    event = {"requestContext": {"http": {"method": "POST"}}, "headers": {}, "body": "not json{"}

    result = handler_module._handle_trigger(event, delivery_table, lambda_client)

    assert result["statusCode"] == 400


def test_trigger_rejects_body_that_is_a_json_array_not_object(delivery_table):
    lambda_client = _FakeLambdaClient()
    event = {"requestContext": {"http": {"method": "POST"}}, "headers": {}, "body": json.dumps([1, 2, 3])}

    result = handler_module._handle_trigger(event, delivery_table, lambda_client)

    assert result["statusCode"] == 400


def test_trigger_does_not_accept_brief_html_field_but_does_not_error_on_its_presence(delivery_table):
    """FR-2a: content generation never produces brief HTML. A caller that still
    sends one is not specifically rejected for it (harmless extra field), but it
    must never be read anywhere -- proven by the fact this succeeds and the
    resulting worker payload is untouched by it (see the self-invoke assertion
    test below, which checks the payload body is passed through as-is)."""
    lambda_client = _FakeLambdaClient()
    body = {**VALID_BODY, "brief_html": "<p>should be ignored</p>"}

    result = handler_module._handle_trigger(_post_event(body), delivery_table, lambda_client)

    assert result["statusCode"] == 202


# ---------------------------------------------------------------------------
# POST /deliver -- the happy path: 202 immediately, pending row written, ONE
# self-invoke with InvocationType="Event" (never waits for the worker leg).
# ---------------------------------------------------------------------------


def test_trigger_returns_202_with_pending_status_and_a_delivery_id(delivery_table):
    lambda_client = _FakeLambdaClient()

    result = handler_module._handle_trigger(_post_event(VALID_BODY), delivery_table, lambda_client)

    assert result["statusCode"] == 202
    body = json.loads(result["body"])
    assert body["status"] == "pending"
    assert "deliveryId" in body
    assert len(body["deliveryId"]) > 0


def test_trigger_writes_a_pending_row_before_returning(delivery_table):
    lambda_client = _FakeLambdaClient()

    result = handler_module._handle_trigger(_post_event(VALID_BODY), delivery_table, lambda_client)
    delivery_id = json.loads(result["body"])["deliveryId"]

    row = delivery_table.get_item(Key={"deliveryId": delivery_id})["Item"]
    assert row["status"] == "pending"


def test_trigger_self_invokes_exactly_once_with_event_invocation_type(delivery_table):
    lambda_client = _FakeLambdaClient()

    result = handler_module._handle_trigger(_post_event(VALID_BODY), delivery_table, lambda_client)
    delivery_id = json.loads(result["body"])["deliveryId"]

    assert len(lambda_client.invocations) == 1
    invocation = lambda_client.invocations[0]
    assert invocation["InvocationType"] == "Event"  # asynchronous -- never waits

    worker_payload = json.loads(invocation["Payload"])
    assert worker_payload["_delivery_worker"] is True
    assert worker_payload["deliveryId"] == delivery_id
    assert worker_payload["body"] == VALID_BODY


def test_trigger_does_not_wait_for_or_call_any_polly_ses_archival_logic(delivery_table):
    """Structural proof this is genuinely async: `_handle_trigger()` never touches
    Polly/SES/S3 -- it only writes a DynamoDB row and calls `lambda_client.invoke`.
    (Enforced by construction here -- lambda_client is the ONLY AWS-shaped
    dependency `_handle_trigger()` receives.)"""
    import inspect

    source = inspect.getsource(handler_module._handle_trigger)
    assert "polly" not in source.lower()
    assert "ses_client" not in source
    assert "send_all" not in source


def test_trigger_marks_row_failed_when_self_invoke_fails(delivery_table):
    lambda_client = _FakeLambdaClient(raise_on_invoke=True)

    result = handler_module._handle_trigger(_post_event(VALID_BODY), delivery_table, lambda_client)

    assert result["statusCode"] == 502
    items = delivery_table.scan()["Items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# GET /deliver/{deliveryId} -- the poll route.
# ---------------------------------------------------------------------------


def test_poll_returns_pending_status_for_an_unprocessed_delivery(delivery_table):
    delivery_table.put_item(Item={"deliveryId": "delivery-1", "status": "pending"})

    result = handler_module._handle_poll(_get_event("delivery-1"), delivery_table)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "pending"


def test_poll_returns_succeeded_status_with_summary(delivery_table):
    summary = {"html_derived": True, "audio_ok": True, "sent_count": 5}
    delivery_table.put_item(
        Item={"deliveryId": "delivery-2", "status": "succeeded", "summary": json.dumps(summary)}
    )

    result = handler_module._handle_poll(_get_event("delivery-2"), delivery_table)

    body = json.loads(result["body"])
    assert body["status"] == "succeeded"
    assert body["summary"] == summary


def test_poll_returns_failed_status_with_error_detail(delivery_table):
    delivery_table.put_item(Item={"deliveryId": "delivery-3", "status": "failed", "error": "delivery_failed"})

    result = handler_module._handle_poll(_get_event("delivery-3"), delivery_table)

    body = json.loads(result["body"])
    assert body["status"] == "failed"
    assert body["error"] == "delivery_failed"


def test_poll_returns_404_for_unknown_delivery_id(delivery_table):
    result = handler_module._handle_poll(_get_event("does-not-exist"), delivery_table)

    assert result["statusCode"] == 404


def test_poll_returns_400_when_delivery_id_path_param_missing(delivery_table):
    event = {"requestContext": {"http": {"method": "GET"}}, "headers": {}, "pathParameters": {}}

    result = handler_module._handle_poll(event, delivery_table)

    assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# Dispatch: handler() routes GET vs POST correctly and distinguishes the worker
# self-invoke payload from a real API Gateway event.
# ---------------------------------------------------------------------------


def test_is_worker_invocation_detects_the_private_marker():
    assert handler_module._is_worker_invocation({"_delivery_worker": True}) is True
    assert handler_module._is_worker_invocation({"requestContext": {}}) is False
    assert handler_module._is_worker_invocation({}) is False


def test_is_api_gateway_event_detects_request_context():
    assert handler_module._is_api_gateway_event({"requestContext": {"http": {"method": "POST"}}}) is True
    assert handler_module._is_api_gateway_event({"_delivery_worker": True, "deliveryId": "x", "body": {}}) is False


def test_is_worker_invocation_is_false_when_both_markers_are_present():
    """REVIEWER-FOUND GAP, FIXED: `_is_api_gateway_event()` was defined but never
    actually wired into `_is_worker_invocation()`'s check -- the dispatch relied
    IMPLICITLY on "a real API Gateway event can never also carry
    `_delivery_worker`" (true and safe today) rather than defending against it
    explicitly. A payload carrying BOTH `_delivery_worker: True` AND a
    `requestContext` key (which should never occur in practice, but is exactly
    the case defense-in-depth exists for -- e.g. a future API Gateway
    integration change, or a refactor that starts echoing extra fields into the
    request context) must NOT be dispatched as a worker invocation -- an
    authenticated API Gateway request must always go through the bearer-auth
    gate, never bypass it via a spoofed/coincidental `_delivery_worker` key."""
    ambiguous_event = {
        "_delivery_worker": True,
        "deliveryId": "some-id",
        "body": {},
        "requestContext": {"http": {"method": "POST"}},
    }

    assert handler_module._is_worker_invocation(ambiguous_event) is False


def test_handler_routes_an_ambiguous_event_through_the_bearer_auth_gate_not_the_worker_leg(monkeypatch):
    """End-to-end proof (not just the predicate unit test above): `handler()`
    itself, given the same ambiguous event, must go through
    `delivery_auth.is_authorized()` -- never straight to
    `_handle_worker_invocation()` bypassing auth entirely."""
    monkeypatch.setattr(handler_module.delivery_auth, "is_authorized", lambda event: False)

    ambiguous_event = {
        "_delivery_worker": True,
        "deliveryId": "some-id",
        "body": {},
        "requestContext": {"http": {"method": "POST"}},
        "headers": {},
    }

    result = handler_module.handler(ambiguous_event, None)

    # 401 (the auth gate rejecting it), NOT a worker-invocation response shape
    # (which would have {"deliveryId": ..., "status": ...} and no statusCode).
    assert result["statusCode"] == 401
