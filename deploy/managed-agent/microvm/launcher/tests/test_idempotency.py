"""Regression tests for the restored webhook idempotency guard
(docs/adr/0010-restore-webhook-idempotency.md).

Backed by a real (moto-mocked) DynamoDB table, matching this repo's convention
for testing AWS-backed logic (deploy/managed-agent/tests/conftest.py's
``mock_aws`` fixtures). ``test_launcher.py`` covers the non-idempotency-aware
paths (signature verification, malformed events, RunMicrovm failure) with a
plain ``Launcher(config, client)`` — this file is scoped to the dedup behavior
specifically, per this repo's test-file-per-concern convention.

Run with: pip install -r ../requirements-dev.txt && pytest (from
deploy/managed-agent/microvm/launcher/).
"""

import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.microvm_client import LaunchedMicroVm, LaunchMicroVmError
from shared.types import LauncherConfig, WebhookEvent

import launcher as launcher_module
from launcher import Launcher, _build_idempotent_executor, handler

_IDEMPOTENCY_TABLE_NAME = "daily-brief-agent-idempotency"


class _FakeMicroVmClient:
    """Same shape as test_launcher.py's fake, tracked at instance level so each
    test gets an independent call count."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    def launch_microvm(self, image_identifier, **kwargs):
        self.calls.append({"image_identifier": image_identifier, **kwargs})
        if self.fail:
            raise LaunchMicroVmError("boom")
        return LaunchedMicroVm(microvm_id="mvm-123", endpoint="https://example.invalid")


def _config() -> LauncherConfig:
    return LauncherConfig(
        environment_id="env_test",
        image_identifier="arn:aws:lambda:us-east-1:740353583786:microvm-image:test",
        environment_key_secret_id="arn:aws:secretsmanager:us-east-1:740353583786:secret:test-env-key",
        execution_role_arn="arn:aws:iam::740353583786:role/test-microvm-role",
        aws_region="us-east-1",
    )


@pytest.fixture
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def idempotency_table(aws_credentials):
    """A real (moto-mocked) DynamoDB table matching the CDK stack's
    _build_idempotency_table() shape: partition key ``id``, TTL attribute
    ``expiration`` (Powertools' default)."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=_IDEMPOTENCY_TABLE_NAME,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield _IDEMPOTENCY_TABLE_NAME


def _idempotent_launcher(client, table_name: str) -> Launcher:
    launcher = Launcher(_config(), client)
    launcher._launch_executor = _build_idempotent_executor(launcher, table_name)
    return launcher


# ---------------------------------------------------------------------------
# The key regression test: a second delivery of the same event_id must be
# deduped -- exactly one RunMicrovm call across two handle() invocations.
# ---------------------------------------------------------------------------


def test_duplicate_event_id_launches_microvm_exactly_once(idempotency_table):
    client = _FakeMicroVmClient()
    launcher = _idempotent_launcher(client, idempotency_table)
    event = WebhookEvent(event_id="evt_dup", data_type="session.status_run_started", session_id="sesn_abc")

    first = launcher.handle(event)
    second = launcher.handle(event)

    assert first["statusCode"] == 200
    assert second["statusCode"] == 200
    assert len(client.calls) == 1


def test_distinct_event_id_for_same_session_is_not_deduped(idempotency_table):
    """Sessions could in principle receive more than one distinct start event
    over their lifetime -- a different event_id for the same session_id must
    launch normally, not be suppressed as a duplicate."""
    client = _FakeMicroVmClient()
    launcher = _idempotent_launcher(client, idempotency_table)
    first_event = WebhookEvent(event_id="evt_1", data_type="session.status_run_started", session_id="sesn_abc")
    second_event = WebhookEvent(event_id="evt_2", data_type="session.status_run_started", session_id="sesn_abc")

    first = launcher.handle(first_event)
    second = launcher.handle(second_event)

    assert first["statusCode"] == 200
    assert second["statusCode"] == 200
    assert len(client.calls) == 2


def test_duplicate_delivery_does_not_surface_launch_failure_of_a_completed_call(idempotency_table):
    """Once the first call has completed successfully, a duplicate delivery
    replays the cached (successful) result rather than re-invoking RunMicrovm
    -- confirming the guard sits around the side effect, not just the retry."""
    client = _FakeMicroVmClient()
    launcher = _idempotent_launcher(client, idempotency_table)
    event = WebhookEvent(event_id="evt_replay", data_type="session.status_run_started", session_id="sesn_abc")

    launcher.handle(event)
    launcher.handle(event)
    launcher.handle(event)

    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# handler()-level wiring: IDEMPOTENCY_TABLE env var installs the wrapped
# executor end-to-end.
# ---------------------------------------------------------------------------


class _FakeBoto3MicroVmClient:
    instances: list["_FakeBoto3MicroVmClient"] = []

    def __init__(self, *, region_name):
        self.region_name = region_name
        self.calls = []
        _FakeBoto3MicroVmClient.instances.append(self)

    def launch_microvm(self, image_identifier, **kwargs):
        self.calls.append({"image_identifier": image_identifier, **kwargs})
        return LaunchedMicroVm(microvm_id="mvm-999", endpoint="https://example.invalid")


def _required_env(monkeypatch, *, signing_secret_arn="arn:aws:secretsmanager:us-east-1:740353583786:secret:signing"):
    monkeypatch.setenv("ANTHROPIC_ENVIRONMENT_ID", "env_test")
    monkeypatch.setenv("MICROVM_IMAGE_IDENTIFIER", "arn:aws:lambda:us-east-1:740353583786:microvm-image:test")
    monkeypatch.setenv("ENVIRONMENT_KEY_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:740353583786:secret:env-key")
    monkeypatch.setenv("MICROVM_EXECUTION_ROLE_ARN", "arn:aws:iam::740353583786:role/test-microvm-role")
    if signing_secret_arn is None:
        monkeypatch.delenv("SIGNING_SECRET_ARN", raising=False)
    else:
        monkeypatch.setenv("SIGNING_SECRET_ARN", signing_secret_arn)


def _api_gateway_event(body: str = "{}") -> dict:
    return {
        "body": body,
        "headers": {"webhook-signature": "sig", "webhook-timestamp": "123", "webhook-id": "evt_1"},
    }


def test_handler_dedupes_duplicate_delivery_via_idempotency_table(monkeypatch, idempotency_table):
    _required_env(monkeypatch)
    monkeypatch.setenv("IDEMPOTENCY_TABLE", idempotency_table)
    monkeypatch.setattr(launcher_module, "_get_secret", lambda arn: "whsec_test")
    monkeypatch.setattr(launcher_module, "Boto3MicroVmClient", _FakeBoto3MicroVmClient)
    _FakeBoto3MicroVmClient.instances.clear()

    body = json.dumps({"id": "evt_handler_dup", "data": {"type": "session.status_run_started", "id": "sesn_abc"}})

    first = handler(_api_gateway_event(body=body), verifier=lambda body, headers, secret: True)
    second = handler(_api_gateway_event(body=body), verifier=lambda body, headers, secret: True)

    assert first["statusCode"] == 200
    assert second["statusCode"] == 200
    # Two handler() invocations each construct their own Boto3MicroVmClient
    # (matching production, where each Lambda invocation calls handler()
    # fresh), but the underlying launch_microvm side effect must fire only once
    # total across both instances -- durable dedup, not per-instance dedup.
    total_launch_calls = sum(len(c.calls) for c in _FakeBoto3MicroVmClient.instances)
    assert total_launch_calls == 1


def test_handler_still_denies_and_never_verifies_when_signing_secret_unset(monkeypatch, idempotency_table):
    """The existing fail-closed behavior must be unaffected by the idempotency
    wiring -- verification (and therefore the idempotent executor) is never
    reached when the signing secret is unset."""
    verifier_calls = []

    def verifier(*args):
        verifier_calls.append(args)
        return True

    result = handler(_api_gateway_event(), verifier=verifier, config=_config())

    assert result["statusCode"] == 500
    assert verifier_calls == []


def test_handler_still_denies_invalid_signature_before_launching(monkeypatch, idempotency_table):
    _required_env(monkeypatch)
    monkeypatch.setenv("IDEMPOTENCY_TABLE", idempotency_table)
    monkeypatch.setattr(launcher_module, "_get_secret", lambda arn: "whsec_test")
    monkeypatch.setattr(launcher_module, "Boto3MicroVmClient", _FakeBoto3MicroVmClient)
    _FakeBoto3MicroVmClient.instances.clear()

    result = handler(_api_gateway_event(), verifier=lambda body, headers, secret: False)

    assert result["statusCode"] == 401
    assert _FakeBoto3MicroVmClient.instances == []
