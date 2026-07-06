#!/usr/bin/env python3
"""CDK app entry point for the decoupled delivery boundary (PRD
docs/prd/agent-system-redesign.md, ADR-0014 Decision 2a).

Deploys a single stack, `BriefDeliveryStack`, that provisions the
`brief-deliveries` DynamoDB tracking table, the empty delivery bearer secret, the
single deliver Lambda (handling both the sync trigger/poll HTTP legs and the async
self-invoke worker leg) + its least-privilege role, and the HTTP API front door.

Context parameters (pass via `-c key=value` or set in cdk.json `context`):
  - `subscribersTableName` (optional, default "brief-subscribers") -- the
    deploy/subscribers/ stack's table name, for the subscriber-fanout query.
  - `subscribersApiBaseUrl` (optional) -- the deploy/subscribers/ stack's API base
    URL, for building per-subscriber unsubscribe links.
  - `feedbackTokenSecretArn` / `feedbackBaseUrl` (optional, backward-compatible) --
    the deploy/feedback/ stack's signing-secret ARN and public base URL, once that
    stack is deployed. Absent by default so this stack synthesizes/deploys cleanly
    before that stack exists (same backward-compatible pattern
    deploy/managed-agent/cdk/managed_agent/stack.py already established).
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
