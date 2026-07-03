"""Launcher Lambda: launch one ephemeral MicroVM per started session.

Adapted from AWS's reference implementation
(github.com/aws-samples/sample-lambda-microvm-claude-managed-agents,
src/functions/launcher.py) per docs/adr/0006. On a
``session.status_run_started`` webhook event, verifies the Anthropic webhook
signature in-process, then launches one MicroVM via ``RunMicrovm`` with the
session dispatch delivered through ``runHookPayload``.

Security model (ADR-0004):
- The launcher passes only a *reference* to the environment-key secret into the
  MicroVM. The environment key itself is fetched by the VM's own execution role
  (IMDSv2) — the launcher never reads or forwards it.
- The organization API key never reaches AWS compute.

Behavior:
- Rejects deliveries that fail signature verification (401).
- Ignores non-``session.status_run_started`` events (200) — matches the launcher
  being reachable, per ADR-0006, only by that single event type.
- Enforces the RunMicrovm 5 TPS rate limit.
- On RunMicrovm failure, returns non-2xx so Anthropic retries.

Adaptation note vs. the reference implementation: this port DROPS the reference's
DynamoDB-backed idempotency table (Powertools Idempotency, deduping concurrent/
retried webhook deliveries by event id). Rationale: this pipeline fires a single
scheduled session per weekday (PRD FR-4), so webhook volume is negligible and a
double-launch (the failure mode idempotency guards against) would at worst run
the daily brief pipeline twice — annoying (duplicate emails) but not unsafe, and
directly observable via Managed Agents run history (PRD AC-4/AC-17). Revisit and
add the idempotency table (mirroring the reference's IdempotencyTable resource)
as a hardening follow-up if double-launches are ever actually observed; the
reference implementation is the template to restore it from if so.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

# Module-scope import so the heavier anthropic (+ pydantic/httpx) cost is paid
# once during Lambda init, not on every invocation.
try:
    import anthropic
except ImportError:  # pragma: no cover - exercised via mocked verifier in tests
    anthropic = None  # type: ignore[assignment]

from shared.constants import (
    DEFAULT_IDLE_POLICY,
    DEFAULT_LOGGING_CONFIG,
    SESSION_RUN_STARTED,
    all_ingress_arn,
    internet_egress_arn,
)
from shared.microvm_client import Boto3MicroVmClient, LaunchMicroVmError, MicroVmClient
from shared.payload import build_run_hook_payload
from shared.rate_limiter import TokenBucket
from shared.types import LauncherConfig, WebhookEvent

_SESSION_ID_PREFIX = "sesn_"
_SESSION_ID_MAX_LEN = 128


def _log(level: str, message: str, **fields: Any) -> None:
    """Minimal structured logging (no aws-lambda-powertools dependency in this
    port — see the microvm/launcher/requirements.txt note on why). CloudWatch
    Logs Insights can still query these as JSON lines."""
    record = {"level": level, "message": message, **fields}
    print(json.dumps(record, default=str))


def verify_signature(body: str, headers: dict[str, str], signing_secret: str) -> bool:
    """Verify a webhook delivery using the Anthropic SDK.

    Returns True if the signature is valid and the payload is fresh, else False.
    """
    if anthropic is None:  # pragma: no cover - import-error path
        _log("error", "anthropic SDK not available; cannot verify webhook signature")
        return False
    try:
        client = anthropic.Anthropic(api_key="unused-for-webhook-verification")
        client.beta.webhooks.unwrap(body, headers=headers, key=signing_secret)
        return True
    except Exception as exc:  # noqa: BLE001
        _log("warning", "webhook signature verification failed", error=str(exc))
        return False


class Launcher:
    """Pure launch logic, testable without AWS."""

    def __init__(
        self,
        config: LauncherConfig,
        client: MicroVmClient,
        *,
        rate_limiter: Optional[TokenBucket] = None,
    ) -> None:
        self._config = config
        self._client = client
        self._rate_limiter = rate_limiter or TokenBucket(config.launch_tps_limit)

    def _launch_and_dispatch(self, event: WebhookEvent) -> dict[str, Any]:
        """Launch one MicroVM with the run hook payload. Raises on failure."""
        run_hook_payload = build_run_hook_payload(event, self._config)
        self._rate_limiter.acquire()
        launched = self._client.launch_microvm(
            image_identifier=self._config.image_identifier,
            run_hook_payload=run_hook_payload,
            max_lifetime_seconds=self._config.max_lifetime_seconds,
            execution_role_arn=self._config.execution_role_arn,
            idle_policy=DEFAULT_IDLE_POLICY,
            logging_config=DEFAULT_LOGGING_CONFIG,
            ingress_network_connectors=[all_ingress_arn(self._config.aws_region)],
            egress_network_connectors=[internet_egress_arn(self._config.aws_region)],
        )

        _log(
            "info",
            "launched microvm",
            microvm_id=launched.microvm_id,
            session_id=event.session_id,
        )
        return {"microvm_id": launched.microvm_id}

    def handle(self, event: WebhookEvent) -> dict[str, Any]:
        """Handle one parsed, verified webhook event. Returns an API Gateway response."""
        if event.data_type != SESSION_RUN_STARTED:
            _log("info", "ignoring non-start event", data_type=event.data_type)
            return {"statusCode": 200, "body": "ignored"}

        # Return 200 (not 4xx) for malformed events so Anthropic doesn't retry a
        # payload that will never become valid.
        if not event.event_id:
            _log("warning", "ignoring event: missing event_id")
            return {"statusCode": 200, "body": "ignored"}

        if not event.session_id:
            _log("warning", "ignoring event: missing session_id", event_id=event.event_id)
            return {"statusCode": 200, "body": "ignored"}

        # Loose shape sanity — log-only, never rejects.
        if not (
            event.session_id.startswith(_SESSION_ID_PREFIX)
            and len(event.session_id) <= _SESSION_ID_MAX_LEN
        ):
            _log(
                "warning",
                "event has an implausible session_id shape (proceeding)",
                event_id=event.event_id,
                session_id=event.session_id,
            )

        try:
            result = self._launch_and_dispatch(event)
        except LaunchMicroVmError as exc:
            _log("error", "RunMicrovm failed", session_id=event.session_id, error=str(exc))
            return {
                "statusCode": 502,
                "body": json.dumps({"error": "run_microvm_failed", "session_id": event.session_id}),
            }

        return {
            "statusCode": 200,
            "body": json.dumps({"microvm_id": result.get("microvm_id"), "session_id": event.session_id}),
        }


def _load_config() -> LauncherConfig:
    region = os.environ.get("AWS_REGION", "us-east-1")
    return LauncherConfig(
        environment_id=os.environ["ANTHROPIC_ENVIRONMENT_ID"],
        image_identifier=os.environ["MICROVM_IMAGE_IDENTIFIER"],
        environment_key_secret_id=os.environ["ENVIRONMENT_KEY_SECRET_ARN"],
        execution_role_arn=os.environ["MICROVM_EXECUTION_ROLE_ARN"],
        aws_region=region,
        signing_secret_arn=os.environ.get("SIGNING_SECRET_ARN"),
        base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
    )


def _get_secret(secret_arn: str) -> str:
    import boto3  # type: ignore[import-not-found]

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    return response["SecretString"]


def handler(
    event: dict[str, Any],
    context: Any = None,
    *,
    verifier: Callable[[str, dict[str, str], str], bool] = verify_signature,
) -> dict[str, Any]:
    """Lambda entry point (API Gateway proxy integration)."""
    config = _load_config()

    body = event.get("body")
    raw_body = body if isinstance(body, str) else json.dumps(body or {})
    headers = event.get("headers") or {}

    if config.signing_secret_arn:
        signing_secret = _get_secret(config.signing_secret_arn)
        if not verifier(raw_body, headers, signing_secret):
            _log("info", "denying webhook: signature verification failed")
            return {"statusCode": 401, "body": "signature verification failed"}

    client = Boto3MicroVmClient(region_name=config.aws_region)
    launcher = Launcher(config, client)

    payload = json.loads(raw_body) if raw_body else {}
    return launcher.handle(WebhookEvent.from_payload(payload))
