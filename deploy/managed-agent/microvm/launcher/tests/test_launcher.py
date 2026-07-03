"""Unit tests for the launcher's pure logic (no AWS calls).

Run with: pip install -r ../requirements.txt -r requirements-dev.txt && pytest
(from deploy/managed-agent/microvm/launcher/).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.microvm_client import LaunchedMicroVm, LaunchMicroVmError, MicroVmClient
from shared.types import LauncherConfig, WebhookEvent

from launcher import Launcher


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
