"""Unit tests for the GET /confirm Lambda handler.

Covers PRD acceptance criteria: AC-2 (valid unexpired link confirms), AC-11 (expired link
fails gracefully and does not confirm), plus invalid-token and idempotent-already-confirmed
paths; and (docs/prd/instant-welcome-brief.md) AC-6 (no welcome-send invoke on an
already-confirmed re-click) and AC-8 (a welcome-send invoke failure never blocks
confirmation).
"""

import subscriber_common as common
from conftest import import_handler

_confirm_module = import_handler("confirm")
_handle = _confirm_module._handle


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


# --- instant-welcome-brief.md: welcome-send async invoke (ADR-0009) ---


class _RecordingLambdaClient:
    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


class _RaisingLambdaClient:
    def invoke(self, **kwargs):
        raise RuntimeError("simulated Lambda invoke failure")


def test_actual_transition_invokes_welcome_send_async(subscribers_table, monkeypatch):
    monkeypatch.setattr(_confirm_module, "WELCOME_FUNCTION_NAME", "arn:aws:lambda:us-east-1:123:function:welcome-send")
    email = "confirming@example.com"
    token = "valid-token"
    subscribers_table.put_item(
        Item={
            "email": email,
            "firstName": "Ada",
            "status": common.STATUS_PENDING,
            "confirmToken": token,
            "confirmTokenExpiresAt": common.now_epoch() + 1000,
        }
    )
    lambda_client = _RecordingLambdaClient()

    resp = _handle(_event(email, token), subscribers_table, lambda_client)

    assert resp["statusCode"] == 200
    assert len(lambda_client.invocations) == 1
    call = lambda_client.invocations[0]
    assert call["InvocationType"] == "Event"
    assert call["FunctionName"] == "arn:aws:lambda:us-east-1:123:function:welcome-send"
    import json

    payload = json.loads(call["Payload"])
    assert payload["email"] == email
    assert payload["firstName"] == "Ada"
    assert payload["unsubscribeToken"]  # a fresh token was generated and passed along


def test_already_confirmed_reclick_does_not_invoke_welcome_send(subscribers_table, monkeypatch):
    """AC-6: re-clicking an already-confirmed link is the idempotent no-op branch and
    must NEVER trigger a resend of the welcome email."""
    monkeypatch.setattr(_confirm_module, "WELCOME_FUNCTION_NAME", "arn:aws:lambda:us-east-1:123:function:welcome-send")
    email = "already@example.com"
    subscribers_table.put_item(
        Item={"email": email, "status": common.STATUS_CONFIRMED, "unsubscribeToken": "existing-unsub-token"}
    )
    lambda_client = _RecordingLambdaClient()

    resp = _handle(_event(email, "irrelevant-token"), subscribers_table, lambda_client)

    assert resp["statusCode"] == 200
    assert lambda_client.invocations == []


def test_invalid_or_expired_token_does_not_invoke_welcome_send(subscribers_table, monkeypatch):
    monkeypatch.setattr(_confirm_module, "WELCOME_FUNCTION_NAME", "arn:aws:lambda:us-east-1:123:function:welcome-send")
    email = "expired@example.com"
    token = "expired-token"
    subscribers_table.put_item(
        Item={
            "email": email,
            "status": common.STATUS_PENDING,
            "confirmToken": token,
            "confirmTokenExpiresAt": common.now_epoch() - 10,
        }
    )
    lambda_client = _RecordingLambdaClient()

    resp = _handle(_event(email, token), subscribers_table, lambda_client)

    assert resp["statusCode"] == 400
    assert lambda_client.invocations == []


def test_welcome_send_invoke_failure_does_not_block_confirmation(subscribers_table, monkeypatch):
    """AC-8: a welcome-send invoke failure (IAM denial, throttling, control-plane
    outage) must still leave the subscriber confirmed and the confirm page returning
    200 -- confirmation is never gated on the welcome send."""
    monkeypatch.setattr(_confirm_module, "WELCOME_FUNCTION_NAME", "arn:aws:lambda:us-east-1:123:function:welcome-send")
    email = "confirming2@example.com"
    token = "valid-token-2"
    subscribers_table.put_item(
        Item={
            "email": email,
            "status": common.STATUS_PENDING,
            "confirmToken": token,
            "confirmTokenExpiresAt": common.now_epoch() + 1000,
        }
    )

    resp = _handle(_event(email, token), subscribers_table, _RaisingLambdaClient())

    assert resp["statusCode"] == 200
    item = common.get_subscriber(subscribers_table, email)
    assert item["status"] == common.STATUS_CONFIRMED


def test_no_welcome_function_configured_is_a_silent_no_op(subscribers_table, monkeypatch):
    """With WELCOME_FUNCTION_NAME unset (e.g. a deploy context that hasn't wired it
    yet), the invoke is skipped -- logged, never raised -- and confirmation still
    succeeds normally."""
    monkeypatch.setattr(_confirm_module, "WELCOME_FUNCTION_NAME", "")
    email = "confirming3@example.com"
    token = "valid-token-3"
    subscribers_table.put_item(
        Item={
            "email": email,
            "status": common.STATUS_PENDING,
            "confirmToken": token,
            "confirmTokenExpiresAt": common.now_epoch() + 1000,
        }
    )
    lambda_client = _RecordingLambdaClient()

    resp = _handle(_event(email, token), subscribers_table, lambda_client)

    assert resp["statusCode"] == 200
    assert lambda_client.invocations == []
