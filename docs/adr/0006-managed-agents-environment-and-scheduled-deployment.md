# 0006. Self-hosted Managed Agents environment and scheduled-deployment definition

- Status: Accepted (amended 2026-07-03 — interim first-party Anthropic fallback, see bottom)
- Date: 2026-07-03 (updated 2026-07-03 — self-hosted Lambda MicroVM environment per ADR-0004)
- Deciders: architect (Claude), human (chose the self-hosted path in ADR-0004; chose the interim
  fallback in the 2026-07-03 amendment)

## Context

The migration replaces the local Claude Desktop scheduled task with a **native Managed Agents
scheduled deployment** (PRD FR-3/4/5, AC-2/AC-3/AC-4) that fires the full pipeline weekdays on
the same cadence and local time as today (6:07 AM local), with **no external trigger** (no
EventBridge/Lambda/Mac fires the *schedule* — the trigger is the platform's own `schedule.cron`).
The deployment definition must be **versioned in this repo** as source-of-truth (FR-16, AC-15).
Managed Agents is in beta and requires the `managed-agents-2026-04-01` header (§6).

Per **ADR-0004 (Accepted, Option B)** the runtime is a **self-hosted sandbox on AWS Lambda
MicroVMs**, not Anthropic's default `cloud` sandbox — chosen so AWS calls authenticate via the
microVM's IAM execution role (IMDSv2, no static key). That decision changes what this ADR's
"environment" is: instead of configuring an Anthropic-hosted sandbox's networking, we operate a
**self-hosted environment** (`config: {type: "self_hosted"}`) backed by our own AWS
infrastructure (launcher Lambda + webhook + microVM image). This ADR fixes that environment's
infra shape, the IaC choice, the networking posture, the schedule, and how it is versioned. It
does not restate the credential mechanism (ADR-0004), the persistence store (ADR-0005), or the
research-half port (ADR-0007).

## Decision

### Environment type: `self_hosted` on AWS Lambda MicroVMs

The Managed Agents **environment is `self_hosted`**. A scheduled session enters the environment's
work queue; Anthropic sends a `session.status_run_started` **webhook** to our AWS account; a
**launcher Lambda** verifies the signature and calls `RunMicroVM`; a **Firecracker microVM**
boots our container image (Anthropic `EnvironmentWorker` entrypoint + pipeline code), executes
the tool calls (where the pipeline and `audio_email.py` run), and terminates when the session
ends (full flow in ADR-0004). The AWS building blocks (mirroring the reference implementation
`aws-samples/sample-lambda-microvm-claude-managed-agents`):

- **API Gateway** webhook endpoint (receives `session.status_run_started`).
- **Launcher Lambda** (webhook-signature verification + `RunMicroVM`).
- **MicroVM container image** (Anthropic `EnvironmentWorker` + the ported pipeline code).
- **Secrets Manager**: the **environment key** (worker auth) and the **webhook signing secret**.
  These are the *only* secrets in the new path — **there is no AWS static-key secret anymore**
  (ADR-0004): AWS access is via the microVM execution role.
- **IAM roles**: (a) the **launcher Lambda role** — scoped to what it needs (invoke `RunMicroVM`,
  read the two Secrets Manager secrets, write its own logs), nothing more; and (b) the
  **microVM execution role** — scoped **verbatim to `deploy/iam-policy.json`** (Polly synth; S3
  rw on `cowork-polly-tts-740353583786/*` **plus** the `s3:ListBucket` prefix-scoped addition
  ADR-0005 requires; SES send gated by the `ses:FromAddress` condition; DynamoDB `Query` on the
  `status-index` GSI). Least privilege, no broader.
- An **S3 bucket** the integration needs for its own operation (per the reference stack) —
  distinct from the pipeline's `cowork-polly-tts-740353583786` artifact bucket.

### IaC: AWS CDK (Python), adapting the reference architecture

**We will build the self-hosted stack with AWS CDK (Python)**, adapting AWS's reference
implementation, rather than deploying the reference CloudFormation template as-is. Rationale:
this repo's stated IaC convention is **CDK Python** and `deploy/subscribers/` is already a CDK
Python app — using CDK keeps one IaC toolchain, one review style, and lets the microVM execution
role reuse the exact least-privilege policy shape already established in this repo. Tradeoff:
porting the reference CloudFormation to CDK is upfront effort and must track the reference as it
evolves during the beta; deploying the CloudFormation as-is would be faster to stand up but
introduces a **second IaC tool** (raw CloudFormation) alongside CDK, fragmenting the deploy
story and making the security-critical IAM roles harder to keep consistent with `deploy/`'s
conventions. Consistency and reviewability win at this scale. (If the reference stack proves
large or fast-moving enough that porting is a genuine drag, revisit — using it as-is is a
reversible fallback, noted below.)

### Networking: microVM default public egress (no Managed Agents `allowed_hosts` allowlist)

With a self-hosted sandbox, egress is governed by **the microVM's own networking**, not by an
Anthropic-side environment `allowed_hosts` list. **Lambda MicroVMs have public internet access
by default** — reaching `api.anthropic.com` (worker↔platform), the AWS API endpoints
(Polly/S3/SES/DynamoDB in `us-east-1`), and the public news sources the research step fetches,
**with no extra networking configuration** (FR-6). A **VPC egress connector is only needed to
reach *private* resources** — not applicable here; everything this pipeline touches is a public
AWS or internet endpoint. So **no VPC, no NAT, no `allowed_hosts` allowlist** is required, and
the built-in research fetching and the AWS calls both work out of the box.

Least-privilege posture is enforced where it actually matters for this design — the **microVM
IAM execution role** (what AWS the code *can* call), not an egress allowlist. Tightening network
egress further (e.g. running the microVM in a VPC with an endpoint allowlist) is possible but
**not adopted now**: it adds VPC/NAT/endpoint machinery and cost for a marginal gain over an
already tightly-scoped IAM role, and the research step legitimately needs broad public web
access anyway. Noted as an available future hardening lever.

### Schedule: native `schedule.cron` in Europe/Berlin (unchanged by the self-hosted choice)

The Deployments API `schedule.cron` targets the `self_hosted` environment id exactly as it would
a `cloud` environment — the session just enters our work queue instead of an Anthropic-managed
container. The timing is unchanged from the original design:

- **Cron:** `schedule.cron = "7 6 * * 1-5"` fired at **06:07 local, Monday–Friday**, with
  **`timezone: "Europe/Berlin"`** (confirmed with the owner 2026-07-03 — this ADR's original
  `America/Los_Angeles` was an unverified placeholder, corrected before the deployment was
  created), matching today's weekday 6:07 AM run. Using the platform's timezone field (not a
  UTC-baked cron) means DST shifts are handled by the platform, so the brief keeps arriving at
  6:07 **local** year-round.
- The deployment is **pausable/unpausable** and exposes **per-run history** natively (AC-4); the
  owner monitors success/failure there and/or via the run webhook (AC-17). No custom monitoring
  is built.
- **Confirm the owner's actual local timezone before deploy** — a one-line config value; if the
  Mac is not on Pacific time, set `timezone` to the owner's real zone.

### Versioning in the repo: `deploy/managed-agent/`

Committed under a **new `deploy/managed-agent/` directory**, following existing `deploy/`
conventions (source-of-truth, human-applied, not auto-deployed):

- `deploy/managed-agent/deployment.json` (or `.yaml`) — the Deployments-API payload: `agent`
  definition reference (ADR-0007), `environment` reference (the `self_hosted` environment id),
  `schedule` (cron + timezone), and the **beta API version pinned** (`managed-agents-2026-04-01`)
  in a metadata field/comment.
- `deploy/managed-agent/cdk/` — the **CDK Python app** for the self-hosted stack (launcher
  Lambda, API Gateway webhook, microVM image build, Secrets Manager secrets, both IAM roles, the
  integration S3 bucket). **Secret values are referenced by name only** (Secrets Manager ARNs/
  names), never inlined (FR-15, AC-14; consistent with how `deploy/subscribers` handles secrets).
- `deploy/managed-agent/microvm/` — the microVM container image (Dockerfile + `EnvironmentWorker`
  entrypoint) and the pipeline code it runs (see ADR-0007 for the ported research/writing skill).
- `deploy/managed-agent/README.md` — the setup/runbook: standing up "Claude Platform on AWS"
  (Marketplace + IAM-federated console, AC-1), deploying the CDK stack, **registering the webhook
  and storing the environment key + signing secret in Secrets Manager out-of-band**, creating/
  updating the deployment, pausing/unpausing, and reading run history.
- The **local Desktop `SKILL.md` inline copy of STEP 6 remains the lockstep counterpart** while
  the local task is still the fallback (PRD FR-17); this ADR does not change that convention.

## Alternatives considered

- **Deploy AWS's reference CloudFormation template as-is** (skip CDK). Rejected as the default:
  faster to stand up, but introduces a second IaC tool alongside this repo's CDK Python
  convention (used in `deploy/subscribers/`), fragmenting the deploy/review story and making the
  security-critical IAM roles harder to keep consistent with `deploy/iam-policy.json`. Retained
  as a **reversible fallback** if CDK-porting the reference proves a genuine drag during the beta.
- **`cloud` environment with a static AWS key** (the original ADR-0006 assumption). Rejected via
  ADR-0004: it requires a standing static credential in a cloud config (security regression). The
  self-hosted path removes the static key, which is why this ADR now describes self-hosted infra.
- **`limited` Managed Agents `allowed_hosts` allowlist** (the original ADR-0006 networking
  choice). No longer applicable: with a self-hosted sandbox, egress is the microVM's own default
  public networking, not an Anthropic-side allowlist. Least privilege is enforced via the microVM
  IAM role instead. (A VPC + endpoint allowlist is the analogous future hardening lever, not
  adopted now — marginal gain over the scoped role, plus VPC/NAT cost, and the research step
  needs broad public web access regardless.)
- **UTC-baked cron** instead of a timezone field. Rejected: drifts an hour across DST; the
  platform `timezone` field is correct.
- **External trigger (EventBridge/Lambda/Mac) firing the schedule.** Rejected: the PRD mandates
  the native `schedule.cron` (FR-4, AC-3). (Note the launcher Lambda is *not* such a trigger — it
  is invoked *by Anthropic's webhook in response to* the native schedule, not a scheduler itself.)
- **Committing the definition under `deploy/subscribers/` or the repo root.** Rejected: distinct
  concern; a dedicated `deploy/managed-agent/` keeps `deploy/` legible.

## Consequences

Positive:
- Networking "just works" — default public egress reaches Anthropic, AWS APIs, and news sources
  with no VPC/NAT/allowlist to maintain (FR-6); least privilege is enforced by the microVM IAM
  role (ADR-0004), the axis that actually gates AWS access.
- Correct local-time delivery year-round via the platform timezone field; native pause/unpause
  and run history satisfy monitoring (AC-4/AC-17) with no custom infra.
- One IaC toolchain (CDK Python), consistent with `deploy/subscribers/`, keeping the deploy and
  security-review story unified; the microVM execution role reuses the repo's least-privilege
  policy shape.
- The deployment + self-hosted stack are source-of-truth in git, secret-free (Secrets Manager
  by reference), satisfying AC-15.

Negative / follow-ups:
- **Real infrastructure to operate** (launcher Lambda, API Gateway webhook, microVM image,
  Secrets Manager, two IAM roles, an integration S3 bucket) — a genuine build/operate increase
  over a one-env-var `cloud` setup (named plainly in ADR-0004). AWS's reference implementation is
  the starting point.
- **CDK-porting the reference stack is upfront effort** and must track the reference as it
  changes during the beta; the as-is CloudFormation fallback is documented if porting drags.
- The **beta API surface may change** (`managed-agents-2026-04-01`); the definition records the
  version built against, and the run must **fail loudly** (not silently skip) if the platform
  contract changes — the migration's whole purpose is to stop silent skips.
- The **webhook endpoint is a new operational/security surface** (signature verification is
  mandatory; its signing secret rotates per that system's guidance).
- The owner's **actual timezone must be confirmed** before deploy (one-line config).
- Standing up "Claude Platform on AWS" (Marketplace + IAM federation, AC-1) is a prerequisite
  runbook step, not automated by this definition.

## Verification note

The self-hosted environment type, the Lambda MicroVM integration flow, default public egress,
and the reference CloudFormation stack are documented (Anthropic
`docs/en/managed-agents/self-hosted-sandboxes`; AWS `microvms-integrations-claude-managed-agents`;
`aws-samples/sample-lambda-microvm-claude-managed-agents`) and confirmed with the human this
session. AWS endpoint hostnames and CDK constructs should be confirmed at build time. The
Developer should validate on the first real run that the microVM reaches all AWS endpoints and
news sources (PRD §7 open networking question) and that `schedule.cron` fires a self-hosted
session end-to-end with the Mac off (AC-2/AC-3).

## Amendment (2026-07-03) — interim first-party Anthropic fallback while AWS registration is blocked

**Status of this amendment: Accepted, explicitly temporary.** "Claude Platform on AWS" remains
the target/primary platform (unchanged); this amendment adds a **parallel, interim fallback** so
build/validation work is not fully blocked on an external support case, with an explicit intent
to **migrate back** once unblocked.

**Trigger.** Signing up for "Claude Platform on AWS" in account `740353583786` fails at the AWS
Console's own sign-up step with `Setup failed: AWS account registration is incomplete or revoked`,
reproducible across multiple attempts including fresh sessions. Diagnosis ruled out: payment
method (active), prior sign-up attempt (none), AWS Organizations-level Marketplace restriction
(account is standalone, confirmed via `aws organizations describe-organization`), and an orphaned
Marketplace agreement (confirmed via `aws marketplace-agreement search-agreements`: zero
agreements exist for this account). Root cause unconfirmed; an AWS Support case is open (24h SLA).

**Decision: stand up the same self-hosted stack against a standard first-party Anthropic account**
(a normal sign-up at `console.anthropic.com`/`platform.claude.com`, billed directly to Anthropic,
no AWS Marketplace involvement) as an interim environment, in parallel with — not instead of —
continuing to pursue Claude Platform on AWS via the support case.

**Why this needs no branch fork and (almost) no code fork.** Checked directly against the code
already built and deployed under this ADR: `ANTHROPIC_BASE_URL` is already a first-class optional
override, threaded from the launcher's own env (`launcher.py` → `LauncherConfig.base_url`)
through the run-hook payload (`shared/payload.py`) into each session's environment, which
`microvm/worker/worker.mjs` reads when constructing its Anthropic client. **Nothing sets this
today, so the stack already defaults to the standard first-party endpoint
(`api.anthropic.com`)** — meaning the first-party fallback is not a different codebase, it is the
exact code already reviewed and deployed, pointed at a different Anthropic account. Practically:

- Same CDK stack, same launcher Lambda, same webhook endpoint, same microVM image, same
  `deploy/iam-policy.json`-scoped execution role — all unchanged and already deployed.
- **Different only**: which Anthropic account creates the `self_hosted` environment/agent/
  deployment (via `console.anthropic.com` instead of the AWS Console/gateway), the values
  populated into the two already-provisioned Secrets Manager secrets (environment key, webhook
  signing secret — now sourced from the first-party account), the webhook registration target
  (still the same already-live URL, just registered against the first-party account's webhook
  settings), and the `anthropicEnvironmentId` CDK context value on redeploy.
- **One known future code delta, not yet built and not needed for this fallback**: Claude
  Platform on AWS requires the `AnthropicAWS` SDK client (SigV4 or AWS-issued-API-key auth, plus
  a required `anthropic-workspace-id` header) rather than the plain `Anthropic` client
  `worker.mjs` uses today. This is a small, additive, backward-compatible change to make when
  migrating back — not a rewrite, and does not need to exist for the first-party path to work now.

**Migration-back plan (once Claude Platform on AWS unblocks).** Recreate the environment/agent/
deployment against the AWS gateway, repopulate the same two Secrets Manager secrets with the
Claude-Platform-on-AWS environment key + signing secret, update the `anthropicEnvironmentId`
context value and redeploy, add the `AnthropicAWS` client conditional to `worker.mjs`, and
decommission the first-party environment/agent/deployment. `deploy/managed-agent/README.md`
documents both provider setups side by side plus this migration runbook so the two paths never
need reconciling — there is no divergent branch to merge back.

**Consequences of this amendment**, additive to the ADR's original ones:
- Positive: unblocks build/validation work today without waiting on the AWS Support case; the
  interim path exercises the identical infra and pipeline code that will run once migrated, so it
  is a genuine dry run, not throwaway work.
  - Note: this is also a strict superset of first-party Claude Managed Agents capability for this
    use case — first-party has *no* autonomous-session reauthentication cap, versus Claude
    Platform on AWS's 6-hour limit — so nothing about this pipeline's behavior is constrained by
    the interim path.
- Negative: billing for the interim period is a separate Anthropic invoice, outside the AWS
  account `740353583786` billing consolidation this project otherwise maintains — accepted as a
  deliberate, temporary tradeoff, not a silent architecture change. The parallel-run validation
  window (PRD §8) should note which platform produced which run's data if both are ever live at
  once during the transition.
