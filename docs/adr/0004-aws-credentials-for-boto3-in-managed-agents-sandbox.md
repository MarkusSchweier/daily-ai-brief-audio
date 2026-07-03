# 0004. AWS credential/identity mechanism for boto3 in the Managed Agents sandbox

- Status: Accepted
- Date: 2026-07-03 (updated 2026-07-03 — human decided Option B, self-hosted Lambda MicroVM)
- Deciders: architect (Claude), human (resolved the escalated security-posture tradeoff)

## Context

The PRD `docs/prd/managed-agents-migration.md` moves the daily brief pipeline off the local
Claude Desktop scheduled task and onto **Claude Managed Agents via "Claude Platform on AWS"**
(decided; not relitigated). Inside that runtime the pipeline must make **boto3 / SigV4-signed**
calls to Polly, S3, SES and DynamoDB (FR-14, AC-13/AC-14). The central open question the PRD
handed to the Architect (§7) was: **how does a real AWS credential reach the sandbox so those
signed calls succeed, without regressing security posture or committing a secret to git?**

Ground truth (settled; do not re-derive):

- **The Managed Agents credential vault is unusable for boto3.** The vault
  (`environment_variable` credential type) substitutes the real secret only at network
  **egress** and hands the sandbox an **opaque placeholder**. boto3 computes the SigV4 request
  signature **client-side, from the real secret, before the request leaves the sandbox** — so it
  signs over the placeholder, and Anthropic's own docs warn such clients "produce an invalid
  signature." Confirmed unsuitable for Polly/S3/SES/DynamoDB.
- **The default `cloud` environment offered no confirmed role-assumption path.** Anthropic's
  "Claude Platform on AWS" page documents IAM-federated **console access for humans**
  (`AssumeConsole`) and SigV4/API-key auth for **calling Claude's own Messages API** — nothing
  that lets an Anthropic-hosted sandbox session assume an AWS IAM role to call *other* services.

**New, confirmed research (done with the human; treat as ground truth):** Managed Agents
support a **self-hosted sandbox** environment type (`config: {type: "self_hosted"}`), documented
at Anthropic's `docs/en/managed-agents/self-hosted-sandboxes` and integrated with AWS via
**Lambda MicroVMs** (AWS's `microvms-integrations-claude-managed-agents` doc + reference
implementation `github.com/aws-samples/sample-lambda-microvm-claude-managed-agents`). In this
model **tool execution runs on infrastructure we control**, not Anthropic's sandbox (Claude's
orchestration/reasoning still runs on Anthropic's side; only tool execution moves):

- A scheduled session enters the self-hosted environment's work queue → Anthropic sends a
  `session.status_run_started` **webhook** to an endpoint in our AWS account → a **launcher
  Lambda** (ours) verifies the webhook signature and calls **`RunMicroVM`** → a **Firecracker
  microVM** boots (snapshot-based, sub-second, up to 8h) running a **container image we build**
  (the Anthropic SDK's `EnvironmentWorker` entrypoint + our pipeline code) → the worker claims
  the session's work item (auth'd with an **environment key**, a lesser-privileged worker-auth
  secret, *not* our org API key), executes each tool call Claude directs (bash/file ops — this
  is where `audio_email.py` and the pipeline run), posts results back → the microVM terminates
  when the session ends.
- **Crucially, each microVM has its own IAM execution role, delivered via IMDSv2** — the same
  mechanism as a normal EC2 instance or Lambda function. boto3 inside the microVM picks up
  **short-lived, auto-rotating temporary credentials** from the standard AWS credential provider
  chain automatically, and signs requests **locally with real, valid credentials**. There is
  **no static key anywhere** and **no vault/placeholder SigV4 problem** — the signing happens on
  infrastructure that holds real credentials.

This resolves the escalated tradeoff: the human chose to build the self-hosted path
specifically to **avoid any standing static credential**.

## Decision

**We will run the pipeline in a self-hosted Managed Agents sandbox on AWS Lambda MicroVMs, and
authenticate AWS calls via the microVM's own IAM execution role (temporary credentials via
IMDSv2). No static AWS access key is created, injected, or stored anywhere.** (Option B.)

Concrete:

- The Managed Agents **environment is `self_hosted`**, backed by the Lambda MicroVMs integration
  (launcher Lambda + webhook + microVM image), built in AWS account `740353583786`
  (see ADR-0006 for the environment/deployment/IaC shape).
- The **microVM's IAM execution role** carries permissions **identical to
  `deploy/iam-policy.json`** — verbatim least privilege: Polly synth; S3 rw on
  `cowork-polly-tts-740353583786/*` (plus the `s3:ListBucket` addition ADR-0005 requires); SES
  `SendEmail`/`SendRawEmail` gated by the `ses:FromAddress` condition; DynamoDB `Query` on the
  `status-index` GSI. Nothing broader. boto3 in the microVM assumes this role automatically via
  IMDSv2; the pipeline code sets no credentials explicitly.
- **No `cowork-polly-tts` static key is used by the new path**, and no new static key is minted.
  The local Desktop fallback keeps using its own existing key locally, untouched (PRD non-goal:
  do not retire the fallback).
- The only secrets in the new path are the **environment key** and the **webhook signing
  secret**, held in **AWS Secrets Manager** (per the reference architecture) — worker-auth /
  webhook-verification secrets, *not* AWS resource-access credentials. They are referenced by
  name in versioned IaC, never committed (FR-15, AC-14).

### The escalated item — resolved

> The escalated tradeoff was: a cloud-hosted static AWS key (Option A) would be a security
> regression vs. today's local-only credential. **The human chose Option B to avoid the static
> key entirely.** The cost is real build effort (a launcher Lambda, a microVM container image, a
> webhook endpoint, and an IaC stack to provision it), accepted in exchange for
> **no standing secret and auto-rotating credentials** — a security posture *better* than
> today's local static key, not merely non-regressed.

## Alternatives considered

- **Option A — plain non-vault environment variable holding a real static AWS key for a
  dedicated least-privilege IAM user, in the default `cloud` environment.** This was the
  *escalated fallback* in the original (Proposed) version of this ADR: confirmed to work, but it
  places a **standing static AWS credential in a cloud-hosted Managed Agents config**, whereas
  today the credential exists only in a local file on the owner's Mac and never leaves it — a
  genuine security-posture regression. **Rejected by the human** in favor of Option B, which
  eliminates the static key altogether. Option A remains the correct *fallback* only if the
  self-hosted integration were ever unavailable; it is not the chosen path.
- **Managed Agents credential vault (`environment_variable` credential type).** Rejected on
  ground truth: egress-time substitution breaks client-side SigV4 signing (invalid signature).
  This is *why* the question existed.
- **Reuse the `cowork-polly-tts` static key in the cloud config.** Rejected: entangles the local
  fallback's live credential with a cloud runtime, prevents independent rotation/revocation, and
  (like Option A) leaves a standing static secret. Option B needs no static key at all.
- **Self-hosted sandbox on our own always-on infrastructure (EC2/ECS/EKS poller)** instead of
  Lambda MicroVMs. Rejected for this once-a-day job: an always-on poller carries idle cost and
  patching burden. The webhook-triggered Lambda MicroVM pattern is serverless, boots on demand,
  and matches this repo's existing serverless bias — no idle cost.

## Consequences

Positive:
- **No standing AWS secret anywhere** — the microVM assumes an IAM execution role and receives
  **short-lived, auto-rotating** credentials via IMDSv2, signed locally and validly (AC-13). This
  is a **posture improvement** over today's local static key, and fully resolves the escalated
  security concern.
- Least privilege preserved (AC-14): the execution role is scoped verbatim to
  `deploy/iam-policy.json` (+ the ADR-0005 `s3:ListBucket` addition).
- No secret in git: the only secrets (environment key, webhook signing secret) live in Secrets
  Manager, referenced by name in versioned IaC.
- Tool execution runs on **our** infrastructure, giving full control over the runtime image and
  the AWS calls it makes.

Negative / follow-ups (name plainly — this is a genuine build-effort increase over Option A):
- **Real infrastructure to build and operate:** a **launcher Lambda** (webhook-signature
  verification + `RunMicroVM`), a **microVM container image** (Anthropic `EnvironmentWorker`
  entrypoint + the pipeline code), an **API Gateway webhook endpoint**, **Secrets Manager**
  secrets (environment key + webhook signing secret), an **S3 bucket** for the integration, and
  the **IAM roles** (launcher role, microVM execution role). AWS publishes a working reference
  implementation (`aws-samples/sample-lambda-microvm-claude-managed-agents`) — this is the
  starting point, not build-from-scratch, but it is materially more than setting one env var.
- **A webhook must be registered** with Anthropic and its signing secret rotated per that
  system's guidance; the webhook endpoint becomes an operational surface to monitor.
- **Beta churn:** the self-hosted sandbox + Lambda MicroVM integration are new/beta
  (`managed-agents-2026-04-01`); the repo records the version built against, and the run must
  **fail loudly** (not silently skip) if the platform contract changes.
- The **environment-key / webhook-secret** are new secrets to manage (in Secrets Manager) — but
  they are worker-auth/verification secrets, not AWS resource credentials, so a leak does not
  grant AWS access; the microVM role is still the only thing that can call Polly/S3/SES/DynamoDB.

## Verification note

The self-hosted sandbox feature, the Lambda MicroVM integration, the IMDSv2-delivered execution
role, default public egress, and the reference CloudFormation stack are all documented (Anthropic
`docs/en/managed-agents/self-hosted-sandboxes`; AWS `microvms-integrations-claude-managed-agents`;
`github.com/aws-samples/sample-lambda-microvm-claude-managed-agents`) and were confirmed with the
human this session. The Developer should build against the reference implementation, confirm the
microVM execution role resolves via IMDSv2 with a live boto3 call, and pin the beta header
`managed-agents-2026-04-01`.
