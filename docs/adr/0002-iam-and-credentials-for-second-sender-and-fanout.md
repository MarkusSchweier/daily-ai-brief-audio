# 0002. IAM and credentials for the second sender and the fan-out subscriber read

- Status: Accepted
- Date: 2026-07-02
- Deciders: architect (Claude), human (settled constraints relayed via orchestrator)

## Context

This is the decision the PRD explicitly flagged for the Architect (PRD §6/§7, "Second SES
sender / IAM scope"). Two new capabilities are needed, and they attach to two different actors:

1. **The three public Lambdas** (subscribe / confirm / unsubscribe) need DynamoDB access and,
   for two of them, SES send from `aibriefing@mschweier.com`.
2. **The daily fan-out** — the existing `deploy/audio_email.py`, run on the Mac as the IAM user
   `cowork-polly-tts` — needs to (a) **read** confirmed subscribers from DynamoDB and (b) **send**
   the subscriber copy of the brief from `aibriefing@mschweier.com`, in addition to the owner's
   copy from `mail@mschweier.com`.

The live least-privilege policy (`deploy/iam-policy.json`) currently pins SES to a single From
via `"Condition": { "StringEquals": { "ses:FromAddress": "mail@mschweier.com" } }` on the
`mschweier.com` domain identity. The hard constraint is that the **owner's from/to path must
keep working completely unchanged**, and least privilege must be preserved.

Key finding: `aibriefing@mschweier.com` needs **no new SES identity**. `mschweier.com` is
already a DKIM-verified **domain** identity, so any sub-address is sendable at the identity
level. In the SES sandbox the *recipient* must be verified, not the sender sub-address. So the
only real gate on the second sender is the IAM `ses:FromAddress` condition — not SES config.

## Decision

**We will use two clearly separated permission surfaces, keeping the owner's live path
untouched:**

### A. Public Lambdas — fresh, function-scoped roles created by CDK (per-function least priv)

Each Lambda gets its own execution role. No role gets table-wide `Scan`, and none reuse the
`cowork-polly-tts` user.

- **subscribe**: `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem` on the table ARN
  only; `ses:SendEmail`/`ses:SendRawEmail` on the `mschweier.com` identity **with
  `ses:FromAddress` condition = `aibriefing@mschweier.com`** (to send the confirmation email).
- **confirm**: `dynamodb:GetItem`, `dynamodb:UpdateItem` on the table ARN only. No SES (the
  confirmation landing page is served by the Lambda response; no email required). If a
  "welcome/confirmed" email is later desired, add the same `aibriefing@` SES condition.
- **unsubscribe**: `dynamodb:GetItem`, `dynamodb:UpdateItem` on the table ARN only. No SES.

All three are scoped to the single table ARN (and, for subscribe, the SES identity ARN). None
get GSI or `Scan` permissions — the public paths are all single-item lookups keyed by `email`
(+ token). Plus the AWS-managed basic Lambda logging permissions.

### B. Fan-out (Mac / `cowork-polly-tts`) — extend the existing policy, additively, with two tightly-scoped Sids

We will **extend the existing `cowork-polly-tts` inline policy** rather than create a second
IAM identity, by adding two new statements and broadening the existing SES From condition to a
two-element list:

1. **New Sid `DynamoDBSubscribersQuery`**: `dynamodb:Query` scoped to **the `status-index` GSI
   ARN only** (`.../table/brief-subscribers/index/status-index`) — not the base table, no
   `Scan`, no writes. The fan-out only ever reads confirmed rows via the GSI.
2. **Broaden `SesSendFromMschweier`**: change the condition to
   `"ses:FromAddress": ["mail@mschweier.com", "aibriefing@mschweier.com"]` (StringEquals with an
   array is OR semantics). Same identity ARN, same actions. This lets the one credential send
   both the owner copy and the subscriber copies.

### Credentials

The fan-out keeps using the **same `cowork-polly-tts` static access key** it uses today — no new
credential is introduced on the Mac. The public Lambdas use **IAM roles** (no static keys at
all). This means the only long-lived secret remains the single existing one (already flagged for
rotation in the README security item — unchanged by this epic, but worth doing before go-live).

## Alternatives considered

- **A second dedicated IAM identity (user or role) for the fan-out's new capabilities**, leaving
  `cowork-polly-tts` pinned to `mail@mschweier.com` only. Considered seriously because it gives
  the tightest theoretical blast-radius separation (a leak of the fan-out's subscriber-sending
  credential could not touch the owner path, and vice versa). **Rejected** because: (a) both
  capabilities run in the *same* process on the *same* Mac in a *single* script invocation, so a
  compromise of that process already has both — a second credential adds operational complexity
  (a second key to store, rotate, and inject via the credential chain) without a real isolation
  gain against the actual threat; (b) the PRD's fail-safe requires the owner and subscriber sends
  to share the already-read MP3 bytes and run in one loop, so splitting identities would mean the
  script juggling two boto3 sessions for no security benefit; (c) the owner path is protected by
  behavior (its own try/except, its own From) far more than by credential separation here. The
  blast-radius delta is not worth the operational cost at this scale.
- **Give the fan-out `dynamodb:Query` on the base table** (not just the GSI). Rejected: the
  fan-out only needs confirmed rows via the status GSI; scoping to the index ARN is strictly
  tighter and still sufficient. `Scan` was never on the table for anyone.
- **A separate `aibriefing@mschweier.com` SES email identity** (verify the address on its own).
  Rejected as unnecessary: the domain identity already covers it; a separate identity adds
  verification/DKIM surface for zero functional gain and would fragment the SES config.
- **Broaden the From to the whole domain** (drop the `ses:FromAddress` condition, or allow
  `*@mschweier.com`). Rejected: that is a real loosening — it would let the credential send as
  *any* address on the domain (including impersonating the owner from other sub-addresses). The
  explicit two-value allow-list is the least-privilege choice.
- **Let the public Lambdas share the `cowork-polly-tts` credential.** Rejected outright: static
  keys in Lambda are an anti-pattern; roles are free and tighter. Lambdas get their own roles.

## Consequences

Positive:
- Owner path unchanged: `mail@mschweier.com` from/to still passes the (now two-element) SES
  condition; the DynamoDB Query Sid is additive and cannot affect existing Polly/S3/SES Sids.
- Least privilege preserved end to end: no `Scan` anywhere; fan-out read is GSI-only; second
  sender is an explicit allow-listed From, not a domain-wide grant; Lambdas are role-scoped and
  keyless.
- Only one long-lived credential remains on the Mac (the existing one) — no new secret to manage.
- The change to `deploy/iam-policy.json` is a small, reviewable, additive diff (two statements +
  one condition value), keeping it in sync with what's actually attached to the user.

Negative / follow-ups:
- The fan-out credential can now send as `aibriefing@` too; a compromise of that Mac process can
  send from both addresses. Accepted given both already run in one process (see Alternatives).
  Mitigation lever if this ever matters: split to a second identity later (reversible).
- `deploy/iam-policy.json` must be applied to the live user by the human/devops as a deploy step
  (it is source-of-truth, not auto-applied); the runbook must call this out, and the GSI ARN in
  the new Sid must match the table CDK actually creates.
- The pre-existing "rotate the exposed access key" security item now also gates the second-sender
  capability — rotate before relying on `aibriefing@` in any real send. Flag to security-engineer.
  **Resolved 2026-07-02**: `cowork-polly-tts` access key rotated and verified (old key
  deactivated); GitHub PAT rotated and verified. See `deploy/validation-handoff.md`.
- SES sandbox still requires each subscriber (recipient) address to be individually verified for
  testing; unchanged by this decision, noted in the PRD.

## Verification note

`aws-docs` MCP was not reachable this session. The mechanics relied on here — `StringEquals`
with an array as OR, `dynamodb:Query` scoping to a GSI ARN, and domain-identity sub-address
sending — are stable, documented IAM/SES/DynamoDB semantics. Developer should confirm the exact
GSI ARN format and re-validate the policy with an SES send test from both From addresses (owner
self-send unchanged; `aibriefing@` to a verified test recipient).
