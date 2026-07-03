# Active PRD

The current active PRD for this project:

@send-confirmation-summary.md

---

Status: **Ready for Developer — no ADR.** Small, additive, no-new-infrastructure change: after each
daily Managed Agents run completes (owner copy + subscriber fan-out), send a short **confirmation
email to `mail@mschweier.com`** from the existing `aibriefing@mschweier.com` sender, stating the
brief went out and to how many **subscribers** (owner excluded from the count). Touches **only**
`deploy/managed-agent/pipeline/audio_email.py` (the local Desktop task is deactivated — not a
lockstep target). No new AWS resource, IAM permission, or secret (verified against
`deploy/iam-policy.json`: SES send is gated on `ses:FromAddress` only, and `mail@mschweier.com` is
already the owner's live recipient). The confirmation send is failure-isolated — a glitch in it
never fails the run. The §7 open question is resolved (FR-8/AC-7: distinguish "0 subscribers because
nobody confirmed" from "0 subscribers because the DynamoDB query silently failed"). **Architect
reviewed 2026-07-03 and confirmed no design ADR is warranted** — nothing here is significant,
irreversible, or cross-cutting (contrast ADR-0009's new-Lambda/IAM fork and ADR-0008's cross-cutting
lockstep procedure); the query-failure signal is a contained tweak to `send_all()`'s return
contract in one file. Developer implements against FR-1..FR-8.

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

Previous PRD — `neutral-briefing-pipeline.md` (separate branch, not-yet-merged): removed the
Anthropic lens and selection skew from the daily brief's **content logic** (AC-1..AC-8, ADR-0008).
Content/prompt-only; no overlap with this feature's delivery/confirm-flow scope, and not a
dependency of it.

Earlier PRD — `managed-agents-migration.md` (merged): migrated the daily brief pipeline to
self-hosted Claude Managed Agents (ADRs 0004–0007 Accepted); the local Desktop task stays running
as a monitored fallback during the parallel-run window.

Earlier PRD — `public-subscriptions.md` (Complete): all three ADRs (0001–0003) Accepted; feature
fully built, reviewed, and security-cleared on branch `feat/public-subscriptions`, deployed and
validated per `deploy/subscribers/README.md`.

Start a new one with: `/feature "<describe the feature>"`
