#!/usr/bin/env python3
"""CDK app entry point for the public subscriber surface.

Deploys a single stack, `BriefSubscribersStack` (see docs/adr/0001), that provisions the
DynamoDB subscriber table, the three subscribe/confirm/unsubscribe Lambdas + their
least-privilege roles, the HTTP API front door, and (later stage) the static subscribe
site behind CloudFront.

Context parameters (pass via `-c key=value` or set in cdk.json `context`):
  - `subscribeDomainName`  (e.g. "briefing.mschweier.com") — the subscribe site's own
    origin, used to lock down CORS on the HTTP API. Optional; if unset, CORS defaults to
    the temporary CloudFront domain only (set once known, or override at deploy time).
  - `certificateArn` (optional) — an existing ACM cert (us-east-1) for the custom domain.
    If unset, the stack still deploys; the site is reachable at the CloudFront default
    domain and the custom-domain/ACM wiring is a documented manual follow-up
    (see deploy/subscribers/README.md) since DNS access is not assumed in every sandbox.
"""

import os

import aws_cdk as cdk

from brief_subscribers.stack import BriefSubscribersStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

BriefSubscribersStack(
    app,
    "BriefSubscribersStack",
    env=env,
    description="Public self-service subscribe/confirm/unsubscribe surface for the daily AI brief.",
)

app.synth()
