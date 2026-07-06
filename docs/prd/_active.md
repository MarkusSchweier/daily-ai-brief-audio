# Active PRD

The current active PRD for this project:

@agent-system-redesign.md

---

Status: **BUILD COMPLETE — all phases shipped + Phase 6 validated (AC-1…AC-14 all PASS); PR opened for
the owner's review on `feat/agent-system-redesign`. NOT merged (main stays owner-gated).** The
agent-system-redesign PRD (rev. 2) is the active planning doc; ADR-0014 records the
decisions. It decouples content generation (Claude Platform) from AWS delivery so a candidate agent
system deploys via a **pure API call with no container build** and is triggered/retrieved with **zero
AWS infrastructure**, git-tracked and declarative. **Built, each reviewed + security-cleared, all on
branch `feat/agent-system-redesign`:**
- **Phase 1** — `deploy/delivery/`: standalone CDK stack, async bearer-authed `POST /deliver` +
  `GET /deliver/{id}` (async trigger/poll — API Gateway's 30s cap can't hold a multi-minute send),
  deterministic no-LLM Markdown→HTML via one fixed template (the "reproduce THE standardized design"
  premise was disproven — 3 real production briefs had 3 different structures — so it establishes one
  fixed template instead), IAM = today's delivery grants moved not duplicated.
- **Phase 2** — `deploy/candidates/`: git-native candidate versioning (per-dimension files, one stable
  `agent_id` per candidate, `git show <ref>:<path>` for history, `sync.py` create-once/update-in-place
  via the confirmed native Agents-API versioning). No `registry.json`, no per-sync git tag.
- **Phase 3** — shared `cloud` environment (`env_01W3Envi4NfK7ypQMfoZccRY`) + `trigger.py`
  (Deployments-API trigger + Sessions-events-API retrieval, the Files-API auto-`file_id` assumption
  having been refuted). Proved live: a skill-version push reaches a candidate with **no image rebuild**
  (the ADR-0008 failure mode this epic exists to fix).
- **Phase 4** — per-brief **source-usage record** (FR-8a, GitHub #28), skill-emitted + archived, live
  skill version pushed.
- **Phase 5** — `production-baseline` candidate (today's real prod config re-expressed), synced to its
  OWN new `agent_id`, triggered for a real research run, output validated **structurally/qualitatively
  equivalent** to same-day production. Surfaced (and fixed) real bugs only a long real run exposed.
**Topology decision (ADR-0014 Decision 1) — RATIFIED HYBRID by the owner 2026-07-06:** `cloud` for
candidate/eval, `self_hosted` RETAINED for production (`deploy/managed-agent/cdk/` + `microvm/` kept,
Phase 7 cut-over is a **no-op**). Chosen over full-cloud because live testing confirmed a `cloud`-only
egress safety-blocklist permanently blocks 4 curated `sources.md` domains (The Verge, Ars Technica,
Reuters, Reddit) with no config workaround, while self-hosted reaches them — a real but bounded
production-quality cost the hybrid avoids for free (the epic's full value lands regardless). (A
parallel `web_search` 429 scare was proven a transient backend blip, not a topology issue.)
**Difference B closed + Phase 6 done.** The delivery-side **`GET /recent-briefs`** read endpoint (so
cloud candidates read the same recent priors production does) is built, **deployed live**, and
validated end-to-end — its auth is a **short-lived HMAC-signed token** (ADR-0014 Decision 2d
correction, ratified 2026-07-06), scoped so a candidate can NEVER reach `POST /deliver`. **Phase 6**
(AC-1…AC-14 + AC-2a/AC-8a) all PASS, independently reviewer-verified against real code, live API
evidence, git history, and both suites; a fresh signed-token end-to-end run confirmed the runtime
criteria live (priors fetched via the endpoint, all 4 artifacts produced, **no email**). ADR-0008 was
reconciled to a **two-way** lockstep (dead Desktop fallback dropped; image-rebuild retained for
self-hosted production). **Live on AWS:** the `deploy/delivery/` stack (signed-token endpoint live;
`POST /deliver` deployed but **locked** — secret undistributed), the shared `cloud` environment, and
the `production-baseline` candidate. **Production self-hosted untouched.** The FR-8 "narrated version =
listening-script text, no TTS for evals" reading was owner-confirmed. **Follow-ups (out of scope
here):** re-integrate the `deploy/eval/` harness against this candidate mechanism (later epic); the
cost-optimization-candidates epic; GitHub issue #30 (candidate-sync drift-check); a one-line skill
clarification that source-usage `featured` = "directly cited."

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
