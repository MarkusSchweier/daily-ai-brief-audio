#!/usr/bin/env python3
"""CDK app entry point for the standalone reader-feedback surface.

Deploys a single stack, `FeedbackStack` (see docs/prd/reader-feedback.md and
docs/adr/0012-feedback-standalone-stack-and-token-helper-packaging.md §B), that
provisions the `brief-feedback` DynamoDB table, the token-signing secret, the submit
Lambda + its least-privilege role, the HTTP API front door, and the static feedback
site behind its own CloudFront distribution. This is a genuinely standalone deploy
lifecycle — it shares no resource or IAM role with `deploy/subscribers/` or
`deploy/managed-agent/` (ADR-0012 §B).

Context parameters (pass via `-c key=value` or set in cdk.json `context`):
  - `feedbackDomainName` (e.g. "feedback.mschweier.com") — the feedback site's own
    origin, used to lock down CORS on the HTTP API and (if `certificateArn` is also
    set) as the CloudFront alias. Optional; defaults to
    `DEFAULT_FEEDBACK_DOMAIN = "feedback.mschweier.com"` for CORS purposes even before
    DNS/cert exist.
  - `certificateArn` (optional) — an existing ACM cert (us-east-1) for the custom
    domain. If unset, the stack still deploys; the site is reachable at the CloudFront
    default domain and the custom-domain/ACM wiring is a documented manual follow-up
    (see deploy/feedback/README.md), matching deploy/subscribers/'s DNS deferral.
"""

import os

import aws_cdk as cdk

from brief_feedback.stack import FeedbackStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

FeedbackStack(
    app,
    "FeedbackStack",
    env=env,
    description="Standalone public reader-feedback form + submit API for the daily AI brief.",
)

app.synth()
