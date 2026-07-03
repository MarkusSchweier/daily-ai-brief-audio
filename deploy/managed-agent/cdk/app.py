#!/usr/bin/env python3
"""CDK app entry point for the self-hosted Claude Managed Agents sandbox.

Deploys a single stack, `ManagedAgentSandboxStack` (see docs/adr/0004, docs/adr/0006),
that provisions the AWS side of Anthropic's self-hosted Lambda MicroVM integration:
the webhook API Gateway + launcher Lambda, the launcher's least-privilege role, the
microVM's own least-privilege IAM execution role (scoped verbatim to
deploy/iam-policy.json's four Sids plus the ADR-0005 s3:ListBucket addition), the two
Secrets Manager secrets (environment key, webhook signing secret — created empty; see
deploy/managed-agent/README.md for out-of-band population), and the S3 bucket the
image-build step needs for its own artifacts (distinct from the pipeline's existing
`cowork-polly-tts-740353583786` bucket, which this stack does not touch).

This adapts AWS's reference implementation
(github.com/aws-samples/sample-lambda-microvm-claude-managed-agents, SAM/CloudFormation)
into AWS CDK (Python), per ADR-0006's IaC decision, so it matches this repo's existing
`deploy/subscribers/` convention (one CDK Python app per deploy surface).

Context parameters (pass via `-c key=value` or set in cdk.json `context`):
  - `anthropicEnvironmentId` (e.g. "env_...") — the Claude self_hosted environment id
    created via the Claude Console (see README). Required for a real deploy; a bare
    `cdk synth` uses a placeholder if unset so the stack still synthesizes for review.
  - `microvmImageIdentifier` (optional) — name of the built microVM image the launcher
    invokes. Defaults to "claude-daily-brief-worker". The image itself is built and
    pushed out-of-band (see deploy/managed-agent/microvm/ and the README) — this stack
    does not build or push the image.
  - `projectName` (optional) — lower-case resource-name prefix. Defaults to
    "daily-brief-managed-agent".
"""

import os

import aws_cdk as cdk

from managed_agent.stack import ManagedAgentSandboxStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

ManagedAgentSandboxStack(
    app,
    "ManagedAgentSandboxStack",
    env=env,
    description=(
        "Self-hosted Claude Managed Agents sandbox (Lambda MicroVMs) for the daily "
        "AI brief pipeline: webhook + launcher Lambda + microVM execution role."
    ),
)

app.synth()
