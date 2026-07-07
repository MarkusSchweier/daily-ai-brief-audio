"""Tests for the `deliveryDecoupled` context flag on MicroVmExecutionRole (ADR-0015 D1/D6).

Flag OFF (default) = today's behavior: full in-VM delivery grants
(Polly/S3/SES/DynamoDB) PLUS the two new ARN-scoped Secrets Manager reads the decoupled
`delivery_client.py` needs. Flag ON = the D1 strip: delivery capability removed, leaving
only env-key + logs + the two auth-token reads (FR-1 posture).

Run from `deploy/managed-agent/cdk/` with its own venv (`.venv/bin/python -m pytest`)."""

from __future__ import annotations

import aws_cdk as cdk

from managed_agent.stack import ManagedAgentSandboxStack

_LEGACY_DELIVERY_SIDS = {
    "PollySynthesis",
    "S3AudioReadWrite",
    "S3ListBriefsPrefix",
    "SesSendFromMschweier",
    "DynamoDBSubscribersQuery",
}
_NEW_SECRET_READ_SIDS = {"ReadDeliveryBearerSecret", "ReadRecentBriefsSigningSecret"}


def _execution_role_sids(*, delivery_decoupled: bool) -> set[str]:
    app = cdk.App(context={"deliveryDecoupled": True} if delivery_decoupled else None)
    stack = ManagedAgentSandboxStack(
        app, "ManagedAgentSandboxStack", env=cdk.Environment(account="740353583786", region="us-east-1")
    )
    template = app.synth().get_stack_by_name(stack.stack_name).template
    sids: set[str] = set()
    for logical_id, resource in template["Resources"].items():
        if resource["Type"] == "AWS::IAM::Policy" and "MicroVmExecutionRole" in logical_id:
            for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
                if statement.get("Sid"):
                    sids.add(statement["Sid"])
    return sids


def test_flag_off_keeps_full_delivery_grants_plus_the_two_new_secret_reads():
    sids = _execution_role_sids(delivery_decoupled=False)
    # Today's in-VM delivery capability is intact...
    assert _LEGACY_DELIVERY_SIDS <= sids
    # ...and the two new auth-token reads are additively present (harmless to audio_email.py).
    assert _NEW_SECRET_READ_SIDS <= sids
    assert "ReadEnvironmentKey" in sids


def test_flag_on_strips_delivery_capability_leaving_only_auth_reads_and_logs():
    sids = _execution_role_sids(delivery_decoupled=True)
    # The D1 strip: NO Polly/S3/SES/DynamoDB delivery capability remains (FR-1).
    assert not (_LEGACY_DELIVERY_SIDS & sids), f"delivery grants not stripped: {_LEGACY_DELIVERY_SIDS & sids}"
    assert "ReadFeedbackTokenSecret" not in sids
    # What remains: env-key read, logs, and the two ARN-scoped delivery-auth reads.
    assert _NEW_SECRET_READ_SIDS <= sids
    assert {"ReadEnvironmentKey", "RuntimeLogs"} <= sids


def test_new_secret_reads_are_arn_scoped_not_wildcard():
    """The two new grants must be scoped to exactly the two delivery secret names (with a
    `-*` for Secrets Manager's random ARN suffix), never a bare `*`."""
    app = cdk.App(context={"deliveryDecoupled": True})
    stack = ManagedAgentSandboxStack(
        app, "ManagedAgentSandboxStack", env=cdk.Environment(account="740353583786", region="us-east-1")
    )
    template = app.synth().get_stack_by_name(stack.stack_name).template
    resources = "".join(
        str(r["Properties"]["PolicyDocument"]["Statement"])
        for lid, r in template["Resources"].items()
        if r["Type"] == "AWS::IAM::Policy" and "MicroVmExecutionRole" in lid
    )
    assert "daily-ai-brief/delivery-bearer-secret-*" in resources
    assert "daily-ai-brief/recent-briefs-read-bearer-secret-*" in resources
