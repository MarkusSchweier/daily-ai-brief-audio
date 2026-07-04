# Active PRD

The current active PRD for this project:

@reader-feedback.md

---

Status: **Deployed and live-validated (2026-07-04); ready for PR.** New epic, built and shipped
fully autonomously overnight per the owner's explicit instruction: a public feedback web form,
reachable via a personalized per-recipient/per-edition link embedded in the daily brief email
(fan-out + instant-welcome-brief), with an anonymous opt-out. Standalone new `deploy/feedback/`
CDK app (own CloudFront, DynamoDB, IAM, signed HMAC token per ADR-0011/0012); collection + storage
only, no analysis/action on the data in scope. Reviewer + security-engineer both passed (one
shared finding — an overscoped, unused DynamoDB grant — fixed before deploy). Real infrastructure
is live: `FeedbackStack` deployed, secret populated, `managed-agent`/`subscribers` stacks
redeployed with the secret wired in, microVM image rebuilt (v7.0), and the live scheduled
deployment updated (deployments turned out to be immutable — documented the actual
create-new/archive-old mechanism in `deploy/managed-agent/README.md`). Live-validated against the
real deployed secret/API/table, including the exact production link-generation function, not a
reimplementation. The **only** remaining step is DNS for `feedback.mschweier.com` (2 CNAME
records, documented in `deploy/feedback/README.md` §3) — gated to the human, since this sandbox
has no DNS API access (same `briefing.mschweier.com` precedent). The feedback link is fully
functional today via the CloudFront default domain in the meantime; no scheduled send occurs
before the DNS window closes (next one: Monday 06:07 Europe/Berlin).

Previous PRD — `send-confirmation-summary.md` (**Shipped, merged PR #21**). Small, additive
change: after each daily Managed Agents run completes, a short confirmation email to
`mail@mschweier.com` states the brief went out and to how many subscribers. Deployed and
live-validated. During this validation, an M1 webhook-replay/idempotency risk (flagged
non-blocking on PR #18) was observed live in practice — see the webhook-idempotency fix below.

Previous fix — **Webhook idempotency restored** (ADR-0010, **merged PR #22**). A DynamoDB-backed
idempotency guard (via `aws-lambda-powertools`) on the launcher Lambda now prevents duplicate
`RunMicrovm` launches from duplicate/replayed webhook deliveries. A reviewer-found fail-open gap
(silent no-dedup if the table env var was unset) was fixed to fail closed before deploy. Deployed
via real `cdk deploy` and live-validated against a genuinely reproduced concurrent-duplicate
webhook delivery (two real signed requests, one microVM, three independent sources of proof).

Previous PRD — `instant-welcome-brief.md` (**Shipped, merged PR #20**). New sign-ups now receive the latest edition of the brief the
moment they confirm their email, with a short welcome header stating the weekday send time
(06:07 Europe/Berlin, unchanged), centralized into one canonical source the email prose and the
deployment schedule agree on. Cross-subsystem: an audio-key pointer was added to the Managed
Agents `briefs/` archival (single place — the local Desktop task doesn't archive), and a scoped
welcome-send Lambda plus least-privilege SES/S3 IAM (ADR-0009: async-decoupled — the confirm
Lambda async-invokes the welcome Lambda, which holds the SES/S3 grants; the confirm Lambda itself
never holds SES/S3 rights). Implemented, independently reviewed, and security-cleared: the
reviewer caught, and a follow-up fix resolved, a duplicate-confirm-request race that could
double-send the welcome email; the security-engineer found no Critical/High issues. Ready for PR.
**Independent of `neutral-briefing-pipeline.md`** below — a separate, not-yet-merged PR this
feature neither depends on nor touches.

Previous PRD — `neutral-briefing-pipeline.md` (merged, PR #19): removed the Anthropic lens and
selection skew from the daily brief's **content logic** (AC-1..AC-8, ADR-0008).

Earlier PRD — `managed-agents-migration.md` (merged): migrated the daily brief pipeline to
self-hosted Claude Managed Agents (ADRs 0004–0007 Accepted); the local Desktop task stays running
as a monitored fallback during the parallel-run window.

Earlier PRD — `public-subscriptions.md` (Complete): all three ADRs (0001–0003) Accepted; feature
fully built, reviewed, and security-cleared on branch `feat/public-subscriptions`, deployed and
validated per `deploy/subscribers/README.md`.

Start a new one with: `/feature "<describe the feature>"`
