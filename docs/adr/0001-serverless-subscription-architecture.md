# 0001. Serverless architecture for public self-service subscriptions

- Status: Accepted
- Date: 2026-07-02
- Deciders: architect (Claude), human (settled constraints relayed via orchestrator)

## Context

The PRD `docs/prd/public-subscriptions.md` adds a public subscribe / double-opt-in-confirm /
unsubscribe surface and a daily fan-out to confirmed subscribers, extending the existing live
audio+mail system (`deploy/audio_email.py`, Polly → S3 → SES) without regressing the owner's
own daily delivery.

Settled constraints from the human (do not relitigate): fully serverless AWS, no standalone
server; a subdomain of `mschweier.com`; IaC is AWS CDK (Python); SES stays in sandbox for this
build; subscriber-facing sender is `aibriefing@mschweier.com` while the owner's
`mail@mschweier.com` from/to path stays byte-for-byte unchanged; double opt-in with a ~48h
confirm-link expiry; unsubscribe reachable from both the site and every subscriber email footer.

A Plan agent produced a recommended architecture against the real repo. This ADR validates and
locks it. Note the daily fan-out is **not** a new AWS trigger — it extends the existing
Mac-based scheduled task (steps 5–8), which runs `deploy/audio_email.py` as the IAM user
`cowork-polly-tts`. The existing Polly/S3/SES resources were created imperatively and have **no
IaC today**; this epic is purely additive and does not retrofit them into CDK.

## Decision

**We will build the public surface as a serverless stack: S3 + CloudFront (static subscribe
page) → API Gateway HTTP API → three single-purpose Lambdas (subscribe / confirm / unsubscribe)
→ DynamoDB → SES.** The daily fan-out is an additive change to the existing
`deploy/audio_email.py` script, not a new Lambda.

Concrete choices:

- **Static site**: S3 (private, all public access blocked) fronted by CloudFront with Origin
  Access Control (OAC). CloudFront serves `https://briefing.mschweier.com` with an ACM
  certificate (us-east-1, required for CloudFront). No client-side framework; a plain HTML form
  posting to the API.
- **API**: API Gateway **HTTP API** (not REST API) — cheaper, lower latency, native JWT/CORS
  support we do not even need here, and sufficient for three unauthenticated POST/GET routes.
  Routes: `POST /subscribe`, `GET /confirm`, `GET /unsubscribe` (GET on the token links so they
  work from an email click; the confirm/unsubscribe handlers are idempotent — see ADR-0003).
- **Compute**: three Lambdas (Python, matching the repo's language), each with its own
  function-scoped IAM role (least privilege — see ADR-0002). Runtime: Python 3.13 on arm64.
- **Data**: one DynamoDB table `brief-subscribers`, on-demand (pay-per-request) billing,
  PK `email` (normalized lowercase), a `status-index` GSI for the fan-out `Query`, and TTL on
  `confirmTokenExpiresAt` to auto-purge never-confirmed rows (schema in ADR-0003).
- **Email**: SES via the already-DKIM-verified `mschweier.com` domain identity. No new SES
  identity is created — `aibriefing@mschweier.com` is a sub-address of the existing domain
  identity, so it is already sendable at the identity level; the only gate is the IAM
  `ses:FromAddress` condition (ADR-0002). Confirmation and unsubscribe-confirmation emails are
  sent by the Lambdas; the daily subscriber copy is sent by the fan-out script.

**CDK stack boundaries — one app, one stack.** A new CDK app lives at `deploy/subscribers/`
with a **single stack** (`BriefSubscribersStack`) that provisions DynamoDB, the three Lambdas +
their roles, the HTTP API, the S3 site bucket, CloudFront + OAC, and the ACM cert. Rationale:
the resources share one lifecycle, are small, and there is no independent-deploy or
blast-radius reason to split site/API/data into separate stacks at this size. The DNS record
(`briefing` CNAME/alias) and the pre-existing SES/DKIM setup are managed **outside** this stack
(DNS is a manual step in the runbook, as with the existing DKIM CNAMEs), keeping the stack free
of cross-account/hosted-zone assumptions. The existing Polly/S3/SES resources are explicitly
**not** imported into CDK.

## Alternatives considered

- **A new fan-out Lambda + EventBridge schedule** instead of extending the Mac script. Rejected:
  the brief text and MP3 are produced on the Mac by the scheduled task; moving fan-out to AWS
  would require shipping the generated MP3/HTML into AWS and re-plumbing the whole step 5–8
  pipeline. That contradicts the PRD ("extends the existing `deploy/audio_email.py`, same
  Mac-based trigger, not a new Lambda") and enlarges scope and regression risk to the owner's
  delivery. The scheduled task already holds AWS credentials and runs the send today.
- **API Gateway REST API** instead of HTTP API. Rejected: REST API is more expensive and
  feature-heavy (usage plans, API keys, request validators) than three anonymous endpoints
  need. HTTP API's built-in throttling and CORS cover the requirements (PRD FR-5, hardening).
- **Lambda Function URLs** (skip API Gateway entirely). Rejected: Function URLs give weaker
  centralized throttling/CORS controls and would spread config across three functions; a single
  HTTP API is a cleaner front door and the cost delta is negligible at this volume.
- **Split into three CDK stacks (data / api / site).** Rejected at this scale: adds
  cross-stack references and deploy ordering for no operational benefit; one stack is simpler to
  reason about and tear down. Revisit only if the surface grows materially.
- **Retrofit existing Polly/S3/SES into CDK for a unified IaC story.** Rejected: out of scope
  and risky. The live resources work and are documented imperatively; importing them risks a
  drift/replace on the owner's critical path. Additive-only is the safer boundary.
- **RDS/Aurora Serverless or S3 as the subscriber store.** Rejected: DynamoDB is the boring,
  cheap, serverless-native fit for a key-by-email single-table access pattern with TTL and a
  status GSI; a relational store is overkill and adds VPC/connection concerns to the Lambdas.

## Consequences

Positive:
- Matches the existing serverless posture; no servers to patch; near-zero idle cost.
- Owner's delivery path is untouched at the infrastructure level — the fan-out change is a
  localized, fail-safe edit to one script (see ADR-0002 for how it reads subscribers).
- One stack is trivial to `cdk deploy` / `cdk destroy` and to review.
- CloudFront + OAC keeps the S3 bucket private and gives HTTPS on the subdomain via ACM.

Negative / follow-ups:
- Two IaC worlds now coexist: CDK for the new surface, imperative runbooks for the live
  resources. This is intentional; documented in the `deploy/subscribers/README.md` runbook.
- DNS for `briefing.mschweier.com` and any SES production-access request remain manual, out-of-
  stack steps (production access is explicitly a later epic per the PRD).
- The Mac scheduled task now depends on DynamoDB read access at send time (ADR-0002); if the
  Mac is offline the whole delivery is skipped as today — no change to that failure mode.
- CloudFront distributions take ~minutes to deploy/change; account for that in the runbook.

## Verification note

The `aws-docs` MCP was not reachable in this session. The AWS mechanics relied on here
(HTTP API throttling/CORS, CloudFront OAC with a private S3 origin, ACM-in-us-east-1 for
CloudFront, DynamoDB on-demand + TTL + GSI, SES domain-identity sub-address sending, and the
`ses:FromAddress` IAM condition key) are long-stable, well-documented behaviors. The developer
should still confirm current service defaults (e.g. Python runtime version, HTTP API default
throttle limits) against live AWS docs at build time.
