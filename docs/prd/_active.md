# Active PRD

The current active PRD for this project:

@eval-harness.md

---

Status: **Approved, build starting (2026-07-04).** New two-epic effort, epic 1 of 2: a real
transcript-mined cost analysis found the daily pipeline costs ~$2.60–2.65/run (Sonnet 5, dominated
by cache-read tokens — the post-research writing/delivery phase costs *more* than research). Before
optimizing that cost (epic 2, separate, not yet started), this epic builds the **measurement
infrastructure**: an eval harness scoring brief-production runs, calibrated against real
`brief-feedback` reader data, with an easy human-review web UI and a structured machine-readable
output for a future optimization agent. ADR-0013 presented custom-AWS-native vs. adopt-Langfuse/
Phoenix; the owner approved **build custom** (new `deploy/eval/`, sibling of `deploy/subscribers/`/
`deploy/feedback/`). The owner then trimmed the full nine-criterion candidate set down to a v1
subset: content selection, factual accuracy (LLM-judge only), length/format compliance,
day-over-day dedup (LLM-judge only), and cost with a phase-level breakdown. Neutrality/tone drift,
listening-script quality, and latency are deferred (not deleted); source-tier diversity is replaced
by a different idea (per-brief source-usage tracking to prune unused sources — issue #28, not this
epic). Working on branch `feat/eval-harness` (created off latest `main`, since the previous
`feat/reader-feedback` branch was already merged and had these new-epic files sitting on it
uncommitted). Docs done; build not yet started.

Previous PRD — `reader-feedback.md` (**Shipped, merged PR #24 + follow-up PR #25**). A public
feedback web form, reachable via a personalized per-recipient/per-edition link embedded in the
daily brief email, with an anonymous opt-out. Standalone `deploy/feedback/` CDK app; live at
`https://feedback.mschweier.com`. Shipped, DNS cut over, fully validated.

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
