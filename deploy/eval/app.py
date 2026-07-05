#!/usr/bin/env python3
"""CDK app entry point for the evaluation harness (PRD docs/prd/eval-harness.md,
ADR-0013 Option A).

Deploys a single stack, `BriefEvalStack`, that provisions the eval-records DynamoDB
table, the reviewer bearer secret + this stack's own Anthropic API key secret, the
trigger/poll/submit-review/read Lambdas + their least-privilege roles, the HTTP API
front door, and the static review site behind CloudFront.

Context parameters (pass via `-c key=value` or set in cdk.json `context`):
  - `evalDomainName` (e.g. "eval.mschweier.com") -- the review site's own origin, used
    to lock down CORS on the HTTP API. Optional; if unset, CORS defaults to the
    temporary CloudFront domain.
  - `certificateArn` (optional) -- an existing ACM cert (us-east-1) for the custom
    domain. If unset, the stack still deploys at the CloudFront default domain.
  - `productionAgentId` / `productionEnvironmentId` -- the SAME agent/environment id
    deploy/managed-agent/deployment.json's live scheduled deployment targets (PRD
    FR-1: evaluation runs must use the established replay mechanism, not a second,
    parallel pipeline). Placeholders are fine for `cdk synth`; a real deploy needs the
    real ids.
  - `feedbackTableArn` / `feedbackTableName` (optional, backward-compatible) -- the
    deploy/feedback/ stack's `FeedbackTableArn`/`FeedbackTableName` outputs, once that
    stack is deployed (PRD FR-15's read-only calibration join). Absent by default so
    this stack synthesizes/deploys cleanly before that stack exists.
"""

import os

import aws_cdk as cdk

from brief_eval.stack import BriefEvalStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

BriefEvalStack(
    app,
    "BriefEvalStack",
    env=env,
    description="Evaluation harness for the daily AI brief pipeline (measurement infrastructure only).",
)

app.synth()
