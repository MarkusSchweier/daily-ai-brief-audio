"""Unit tests for the launcher's pure logic (no AWS calls).

Run with: pip install -r ../requirements.txt -r requirements-dev.txt && pytest
(from deploy/managed-agent/microvm/launcher/).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.microvm_client import LaunchedMicroVm, LaunchMicroVmError, MicroVmClient
from shared.types import LauncherConfig, WebhookEvent

import launcher as launcher_module
from launcher import Launcher, handler, verify_signature


class _FakeMicroVmClient:
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


def test_ignores_non_start_events():
    client = _FakeMicroVmClient()
    launcher = Launcher(_config(), client)
    event = WebhookEvent(event_id="evt_1", data_type="session.status_run_completed", session_id="sesn_abc")

    result = launcher.handle(event)

    assert result == {"statusCode": 200, "body": "ignored"}
    assert client.calls == []


def test_ignores_missing_event_id():
    client = _FakeMicroVmClient()
    launcher = Launcher(_config(), client)
    event = WebhookEvent(event_id="", data_type="session.status_run_started", session_id="sesn_abc")

    result = launcher.handle(event)

    assert result == {"statusCode": 200, "body": "ignored"}
    assert client.calls == []


def test_launches_microvm_on_valid_start_event():
    client = _FakeMicroVmClient()
    launcher = Launcher(_config(), client)
    event = WebhookEvent(event_id="evt_1", data_type="session.status_run_started", session_id="sesn_abc")

    result = launcher.handle(event)

    assert result["statusCode"] == 200
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["execution_role_arn"] == "arn:aws:iam::740353583786:role/test-microvm-role"
    assert call["ingress_network_connectors"] == [
        "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS"
    ]
    assert call["egress_network_connectors"] == [
        "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
    ]


def test_returns_502_on_launch_failure():
    client = _FakeMicroVmClient(fail=True)
    launcher = Launcher(_config(), client)
    event = WebhookEvent(event_id="evt_1", data_type="session.status_run_started", session_id="sesn_abc")

    result = launcher.handle(event)

    assert result["statusCode"] == 502


def test_run_hook_payload_never_leaks_secrets():
    from shared.payload import build_run_hook_payload

    event = WebhookEvent(event_id="evt_1", data_type="session.status_run_started", session_id="sesn_abc")
    payload = build_run_hook_payload(event, _config())

    assert "ANTHROPIC_API_KEY" not in payload
    assert "ANTHROPIC_ENVIRONMENT_KEY" not in payload
    assert "sesn_abc" in payload


# ---------------------------------------------------------------------------
# handler() / verify_signature() — the unauthenticated-transport surface
# (API Gateway AuthorizationType.NONE per stack.py; the HMAC check in these
# two functions is the *only* thing that authenticates a delivery). Covers
# the fail-open regression: an unset SIGNING_SECRET_ARN must deny, not skip
# verification and proceed to RunMicrovm.
# ---------------------------------------------------------------------------


class _FakeBoto3MicroVmClient:
    """Stand-in for Boto3MicroVmClient — asserts no real boto3/AWS call is
    reachable from these tests. Tracked at class level so a test can assert
    whether a client was ever constructed (i.e. whether verification passed)."""

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


def test_handler_denies_and_never_verifies_when_signing_secret_unset():
    """The fail-open bug this test exists to prevent regressing: a config
    without a signing secret must be treated as 'cannot verify' (deny),
    never as 'verification not required' (proceed). Injects a LauncherConfig
    directly (via handler()'s config= parameter) rather than deleting the env
    var, since _load_config() now separately fails even harder (KeyError at
    cold start) — this test is about handler()'s own defense-in-depth check,
    which matters independently of how a config without the secret arrives."""
    verifier_calls = []

    def verifier(*args):
        verifier_calls.append(args)
        return True  # would incorrectly authorize the request if ever reached

    result = handler(_api_gateway_event(), verifier=verifier, config=_config())

    assert result["statusCode"] == 500
    assert verifier_calls == []  # verification must not even be attempted


def test_load_config_raises_at_cold_start_when_signing_secret_unset(monkeypatch):
    """The stronger, cold-start-time half of the fix: a Lambda missing
    SIGNING_SECRET_ARN entirely must fail loudly on the very first
    invocation, not silently construct a config that reaches handler()'s
    fail-closed check on every subsequent request."""
    _required_env(monkeypatch, signing_secret_arn=None)

    with pytest.raises(KeyError):
        launcher_module._load_config()


def test_handler_denies_invalid_signature_before_launching(monkeypatch):
    _required_env(monkeypatch)
    monkeypatch.setattr(launcher_module, "_get_secret", lambda arn: "whsec_test")
    monkeypatch.setattr(launcher_module, "Boto3MicroVmClient", _FakeBoto3MicroVmClient)
    _FakeBoto3MicroVmClient.instances.clear()

    result = handler(_api_gateway_event(), verifier=lambda body, headers, secret: False)

    assert result["statusCode"] == 401
    assert _FakeBoto3MicroVmClient.instances == []  # RunMicrovm client never constructed


def test_handler_launches_on_valid_signature(monkeypatch):
    _required_env(monkeypatch)
    monkeypatch.setattr(launcher_module, "_get_secret", lambda arn: "whsec_test")
    monkeypatch.setattr(launcher_module, "Boto3MicroVmClient", _FakeBoto3MicroVmClient)
    _FakeBoto3MicroVmClient.instances.clear()

    body = json.dumps({"id": "evt_1", "data": {"type": "session.status_run_started", "id": "sesn_abc"}})
    result = handler(_api_gateway_event(body=body), verifier=lambda body, headers, secret: True)

    assert result["statusCode"] == 200
    assert len(_FakeBoto3MicroVmClient.instances) == 1
    assert len(_FakeBoto3MicroVmClient.instances[0].calls) == 1


class _FakeWebhooksResource:
    def __init__(self, *, should_raise: bool):
        self.should_raise = should_raise
        self.calls = []

    def unwrap(self, body, *, headers, key):
        self.calls.append({"body": body, "headers": headers, "key": key})
        if self.should_raise:
            raise ValueError("invalid or expired webhook signature")
        return {"type": "session.status_run_started"}


class _FakeAnthropicClient:
    def __init__(self, *, api_key, webhooks: _FakeWebhooksResource):
        self.api_key = api_key
        self.beta = type("_Beta", (), {"webhooks": webhooks})()


class _FakeAnthropicModule:
    """Replaces the whole `anthropic` name in launcher.py's namespace, so
    these tests don't depend on the real SDK's exact webhook-verification
    behavior — only on verify_signature() calling it correctly."""

    def __init__(self, webhooks: _FakeWebhooksResource):
        self._webhooks = webhooks

    def Anthropic(self, *, api_key):
        return _FakeAnthropicClient(api_key=api_key, webhooks=self._webhooks)


def test_verify_signature_true_for_a_valid_delivery(monkeypatch):
    fake_webhooks = _FakeWebhooksResource(should_raise=False)
    monkeypatch.setattr(launcher_module, "anthropic", _FakeAnthropicModule(fake_webhooks))

    result = verify_signature("raw-body", {"webhook-signature": "sig"}, "whsec_test")

    assert result is True
    assert fake_webhooks.calls == [{"body": "raw-body", "headers": {"webhook-signature": "sig"}, "key": "whsec_test"}]


def test_verify_signature_false_for_an_invalid_or_expired_delivery(monkeypatch):
    fake_webhooks = _FakeWebhooksResource(should_raise=True)
    monkeypatch.setattr(launcher_module, "anthropic", _FakeAnthropicModule(fake_webhooks))

    result = verify_signature("raw-body", {"webhook-signature": "bad"}, "whsec_test")

    assert result is False


def test_verify_signature_false_when_anthropic_sdk_unavailable(monkeypatch):
    monkeypatch.setattr(launcher_module, "anthropic", None)

    assert verify_signature("raw-body", {}, "whsec_test") is False
