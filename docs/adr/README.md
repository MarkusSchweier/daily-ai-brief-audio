# Architecture Decision Records

Significant, cross-cutting, or hard-to-reverse technical decisions are recorded here by the
Architect agent, numbered sequentially (`0001-...md`). See the global `adr-writing` skill for
the template and when an ADR is required.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-serverless-subscription-architecture.md) | Serverless architecture for public self-service subscriptions | Accepted |
| [0002](0002-iam-and-credentials-for-second-sender-and-fanout.md) | IAM and credentials for the second sender and the fan-out subscriber read | Accepted |
| [0003](0003-subscriber-data-model-and-tokens.md) | Subscriber data model, tokens, and expiry | Accepted |
| [0004](0004-aws-credentials-for-boto3-in-managed-agents-sandbox.md) | AWS credential/identity for boto3 in the sandbox (self-hosted Lambda MicroVM, IAM execution role) | Accepted |
| [0005](0005-cross-run-persistence-store-for-brief-history.md) | External cross-run persistence store for brief history | Accepted |
| [0006](0006-managed-agents-environment-and-scheduled-deployment.md) | Self-hosted Managed Agents environment and scheduled-deployment definition | Accepted |
| [0007](0007-porting-the-research-writing-half-into-the-managed-agent.md) | Faithfully porting the research/writing half into the Managed Agent | Accepted |
| [0008](0008-skill-content-change-lockstep-and-live-version-push.md) | Three-way lockstep + live Skills-API version push for skill-content changes | Accepted |
| [0009](0009-async-welcome-send-decoupling.md) | Decouple the welcome send from the confirm request path via async Lambda invoke | Accepted |
