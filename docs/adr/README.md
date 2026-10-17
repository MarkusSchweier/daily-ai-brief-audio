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
| [0008](0008-skill-content-change-lockstep-and-live-version-push.md) | Skill-content lockstep (reconciled to two-way by ADR-0014) + live Skills-API version push | Accepted (amended 2026-07-06) |
| [0009](0009-async-welcome-send-decoupling.md) | Decouple the welcome send from the confirm request path via async Lambda invoke | Accepted |
| [0010](0010-restore-webhook-idempotency.md) | Restore durable webhook idempotency in the launcher Lambda | Accepted |
| [0011](0011-feedback-link-signed-token-scheme.md) | Signed, self-attesting feedback-link token scheme | Accepted |
| [0012](0012-feedback-standalone-stack-and-token-helper-packaging.md) | Standalone `deploy/feedback/` CDK app and token-helper packaging | Accepted |
| [0013](0013-eval-harness-backbone-build-vs-adopt.md) | Evaluation-harness backbone: custom AWS-native build vs. adopting a self-hostable eval tool | Accepted |
| [0014](0014-agent-system-redesign-topology.md) | Agent-system redesign: environment topology (cloud vs. self-hosted), decoupled delivery boundary, and git-native candidate versioning | Accepted — Decision 1 (hybrid) + 2d signed-token ratified by the human 2026-07-06 |
| [0015](0015-production-delivery-decoupling.md) | Production delivery decoupling: route the self-hosted MicroVM's daily send through the delivery boundary (full decouple, four-artifact contract) | Accepted 2026-07-06 — amends ADR-0014 Decision 1 (Phase-7 no-op reversed for delivery); cut-over owner-gated |
| [0016](0016-eval-harness-reintegration.md) | Eval-harness re-integration: a local-first, git-native harness driving the decoupled candidate mechanism (cost-optimization epic, step A) | Accepted 2026-07-07 (Gate 0); amended 2026-07-07 (judge methodology v2) and 2026-10-11 (judges on Opus 5.5, per-request rate-card pricing, effort in candidate declarations, brief-capture guard); supersedes ADR-0013 |
| [0017](0017-audio-link-delivery.md) | Audio-as-link delivery: serve `audio/*` through the existing subscriber CloudFront distribution with prefix-scoped OAC, and replace the MP3 attachment with an expiry-dated link | Accepted 2026-07-27 — owner-ratified at Gate 2; D2 flipped to a CDK-managed bucket policy against the architect's recommendation, D11 amended (welcome greeting stays first) |
| [0018](0018-source-urls-in-initial-prompt.md) | Restore `web_fetch` by generating the curated `sources.md` URLs into the first user message (production `initial_prompt` + one new eval candidate), check-only generator plus a drift test | Accepted 2026-10-17 — owner approved and authorized deployment, deployed the same day; amended 2026-10-17 post-evaluation (production on agent v4 with the `web_fetch` content cap; two added block rules; refusal risk). No image, skill, IAM or delivery change |
