# Active PRD

The current active PRD for this project:

@send-confirmation-summary.md

---

Status: **Shipped (2026-07-03).** Small, additive, no-new-infrastructure change: after each daily
Managed Agents run completes (owner copy + subscriber fan-out), a short **confirmation email to
`mail@mschweier.com`** is sent from the existing `aibriefing@mschweier.com` sender, stating the
brief went out and to how many **subscribers** (owner excluded from the count), with the
DynamoDB-query-failure-vs-genuine-zero disambiguation from FR-8/AC-7. Touches **only**
`deploy/managed-agent/pipeline/audio_email.py` (the local Desktop task is deactivated — not a
lockstep target); no new AWS resource, IAM permission, or secret. Architect confirmed no ADR
needed. Implemented (`a0d1450`, `445f8c1`), independently reviewed (approved) and
security-cleared (no findings). **Deployed**: microVM image rebuilt to version `6.0` and
**live-validated** — a real end-to-end session confirmed via raw log output (not just the agent's
own summary) that the confirmation genuinely sent with a real SES `MessageId` and correct
skip-mode wording. One incidental finding worth a human look: the validation run's single
start-event triggered multiple redundant microVM launches (webhook retry) that mostly hit an
account-level memory quota — harmless (exactly one instance won and completed the work, no
duplicate email), but a live instance of the webhook-replay/no-idempotency risk (M1) already
flagged as non-blocking on PR #18; worth reconsidering priority now that it's observed in
practice. Ready for PR.

Previous PRD — `instant-welcome-brief.md` (**Shipped 2026-07-03**). New sign-ups now receive the latest edition of the brief the
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
