# Active PRD

The current active PRD for this project:

@agent-system-redesign.md

---

Status: **PRD rev. 2 + revised ADR-0014, both awaiting human sign-off before any build
(2026-07-06).** The agent-system-redesign PRD is the active planning doc. It fully decouples content
generation (Claude Platform) from AWS delivery (TTS/email/archival/DynamoDB) so a candidate agent
system can be deployed via a **pure API call with no container build** and triggered/retrieved with
**zero AWS infrastructure** and zero risk to the live send — with git-tracked, declarative,
independently-diffable, multi-agent-capable candidate definitions. **The PRD was revised (rev. 2) on
2026-07-06** to incorporate six owner-feedback points given after reviewing rev. 1: (1) **eval-harness
re-integration is de-scoped** to a later, separate epic — this epic keeps only the standalone property
that a candidate is triggerable and its artifacts retrievable via Claude-Platform-only APIs; (2) the
**local Desktop fallback is declared dead**; (3) **Markdown→HTML conversion moves to the delivery
side, deterministically (no LLM)** — content generation's output narrows to brief markdown +
listening-script text; (4) **git-native versioning replaces a bespoke `registry.json`**; (5) restates
(1); (6) a **new per-brief source-usage output** seeds GitHub issue #28's later source-consolidation
effort. **`docs/adr/0014-agent-system-redesign-topology.md` has now been revised to match (also
2026-07-06)**, status **"Proposed — pending human sign-off."** Its recommendation: full
**cloud-for-everything** (retire `deploy/managed-agent/cdk/` + `microvm/`, staged behind validation),
with a conservative hybrid (cloud eval, self-hosted production) offered as an explicit fallback; a new
standalone `deploy/delivery/` stack (bearer-token auth) that now also derives brief HTML
deterministically from markdown (flagged as a regression risk needing byte-for-byte verification
against a real production brief); and candidate versioning via **an annotated git tag per sync event**
(`candidate/<slug>/sync-<n>`, recording live Platform IDs in the tag message) instead of a
`registry.json` — directly answering the owner's "retrieve a previous prompt version without rolling
back the repo" question via `git show <ref>:<path>`. The ADR-0008 lockstep is reconciled to two-way
(in-repo ↔ live Skills-API), unconditionally, per the dead-Desktop-fallback decision. The one PM-level
ambiguity — "narrated version" = pre-narration listening-script text, not synthesized audio (§7/FR-8)
— was **confirmed by the owner (2026-07-06)**: "The listening script is the output. No actual TTS for
evals." **No implementation starts until the owner reviews both documents and signs off** — open items
include the full-cloud-vs-hybrid topology call, the delivery/candidate-layout shapes, and the
ADR-0008/source-usage reconciliations. Next step: owner reviews `docs/prd/agent-system-redesign.md`
(rev. 2) and `docs/adr/0014-agent-system-redesign-topology.md` (revised) and decides.

Previous PRD — `eval-harness.md` (**Shipped, merged 2026-07-05**). `deploy/eval/` deployed and
live-validated end to end (real evaluation runs, real cost breakdown, real candidates.json-driven
content-selection judging) — see that PRD/ADR-0013 for the full build history, including several
real bugs found and fixed only by actually triggering live runs (a DynamoDB reserved-keyword bug,
two Deployments-API shape gaps, a cost-miner endpoint/field bug, the microVM-image skill-content
lockstep gap, and an eval-prompt fidelity gap caught by an independent review). Merged to `main`.
The agent-system-redesign epic (now active, above) does **not** re-integrate this harness — as of the
PRD's rev. 2 (owner feedback), re-integrating `deploy/eval/` against the new candidate-deployment
mechanism is **deferred to a later, separate epic** (the harness will be adapted to whatever the
redesign produces, not the other way around). Its current trigger still targets one hardcoded
agent/environment pair; that stays until the later adaptation epic.

- **Cost-optimization-candidates epic (deferred — `cost-optimization-candidates.md`, documented).**
  The candidate pipeline configurations to compare once the redesign above lands. *(Note, per the
  redesign PRD's rev. 2: eval-harness re-integration is now its **own** deferred epic between this
  one and the redesign — the redesign delivers a triggerable/retrievable candidate mechanism, a
  later epic adapts `deploy/eval/` to it, and this cost-optimization epic then uses both.)*
  Documented now so the candidate list (owner + Claude contributed) is durable, but explicitly
  **not** being built until the redesign epic above ships — building it against the current
  architecture would mean rebuilding it again once the redesign changes how candidates are
  deployed/run.

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
