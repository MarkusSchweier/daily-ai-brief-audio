#!/usr/bin/env python3
"""CDK app entry point for the decoupled delivery boundary (PRD
docs/prd/agent-system-redesign.md, ADR-0014 Decision 2a).

Deploys a single stack, `BriefDeliveryStack`, that provisions the
`brief-deliveries` DynamoDB tracking table, the empty delivery bearer secret, the
single deliver Lambda (handling both the sync trigger/poll HTTP legs and the async
self-invoke worker leg) + its least-privilege role, and the HTTP API front door.

Context parameters:
  - `subscribersTableName` (optional, default "brief-subscribers") -- the
    deploy/subscribers/ stack's table name, for the subscriber-fanout query.
  - `subscribersApiBaseUrl` / `feedbackTokenSecretArn` / `feedbackBaseUrl` -- the
    email-chrome config (unsubscribe + feedback links). COMMITTED DEFAULTS live in
    this directory's cdk.json since the 2026-07-08 incident (a -c-less deploy
    silently reset them to "" and the first decoupled production send lost its
    feedback links and shipped broken unsubscribe links); a `-c` flag still
    overrides. The stack FAILS LOUD at synth if any resolves empty --
    `-c allowEmptyChromeConfig=true` is the tests/bootstrap-only escape hatch.
"""

import os

import aws_cdk as cdk

from brief_delivery.stack import BriefDeliveryStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

BriefDeliveryStack(
    app,
    "BriefDeliveryStack",
    env=env,
    description="Decoupled AWS delivery boundary for the daily AI brief pipeline (PRD agent-system-redesign.md, ADR-0014).",
)

app.synth()
