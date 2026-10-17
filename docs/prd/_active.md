# Active PRD

The current active PRD for this project:

@web-fetch-source-urls.md

---

Status: **DEPLOYED 2026-10-17; AC-10 pending.** The source-URL block and a `web_fetch` content cap went live as production agent v4 (scheduled deployment `depl_01WXFPPJfy5JaQju9LUtXrcH`), and AC-10 is judged over the first three production runs, starting Mon 2026-10-19 06:07 Europe/Berlin. A bounded production fix, not an epic.
Anthropic's 2026-10-16 change means `web_fetch` can only retrieve a URL that already appeared in the
conversation (user message, client-side tool result, or an earlier `web_search`/`web_fetch` result) —
and the agent reading `sources.md` with its built-in file/shell tools does **not** count. Production
research has been degraded since **2026-10-08** and since the 2026-10-12 Haiku 5.5 runs it fetches
**zero** curated sources (3–6 `url_not_in_prior_context` rejections every run), running on
`web_search` alone. Sends are unaffected. The second announced change (a cloud environment's
`limited` `allowed_hosts` now also limiting the web tools) is a **no-op here**: production is
`self_hosted`, the eval `cloud` environment is `unrestricted`, and no per-tool domain lists are set.
**Owner-approved solution + full build→test→evaluate→deploy cycle authorized:** append a
`sources.md`-generated **source-URL block to the initial user message** — the scheduled deployment's
`initial_prompt` and the matching eval candidate's task prompt — keeping `sources.md` the single
source of truth (a drift test fails if they diverge), with the block also teaching fetch-the-feed-
then-follow-its-links, never retry a rejection, and fall back to search. **No** skill change, **no**
model change (Haiku 5.5 at effort high stays), **no** bash/curl fetching, **no** delivery
change, **no** environment-config change. It was gated by a matched cloud eval (3 reps, all four judges), a
no-delivery run on production self-hosted, and a new deployment with the old one archived. The
deployment pins agent v4 rather than the approved v3, because of the content cap. Requirements, ACs,
risks and rollout are in `web-fetch-source-urls.md`, imported above, and its §9 has the outcome,
deviations and follow-ups.

---

### Previous epic — agent cost optimization (DECIDED 2026-10-11 — production on Haiku 5.5)

Status: **DECIDED.** Step C's first matched, replicated comparison ran 2026-10-11 and the owner took
the decision the same day: **production moved to Claude Haiku 5.5 at effort high** (agent v3,
scheduled deployment `depl_01YRoLodhcr3HuhVS3zxVuLx`, first send Mon 2026-10-12 06:07 Europe/Berlin;
rollback = a deployment pinned to agent v2 / Haiku 4.5). The eval harness built in step A remains the
standing measurement tool — it is what gates the active fix above. Build history below.

Build history (as written while it was active): **ACTIVE — agent cost-optimization epic
(2026-07-07).** A **combined A/B/C epic** (owner
decision): **A** re-integrate the eval harness with the new candidate mechanism, **B** configure &
deploy the candidate set, **C** run the comparison & decide the future production set-up. Sequenced
**B → A → C**. **B is DONE** (all four candidates declared AND synced to real Platform `agent_id`s —
the multi-agent coordinator roster shape was fixed against the live API in the process). **A is
BUILT + reviewed + security-cleared + live-validated** (PR #37): the local-first, git-native
`deploy/eval-harness/` per **ADR-0016** (Accepted) — per-thread model-aware cost, run CLI, Flask UI
(runs permanently as a local launchd service on 127.0.0.1:5151), three real judged run records
committed. **Judge methodology v2** (owner-directed 2026-07-07, ADR-0016 amendment; branch
`feat/judge-methodology`, reviewer + security GO): all-Opus judges, the accuracy judge WEB-VERIFIES
claims via server-side search/fetch (kills the v1 knowledge-cutoff bias — all three candidates
re-scored factual=5), content-selection sharpens disagreements via search, dedup gets a strictly
prior-days one-per-day feed + labelled/justified findings, prompt caching on every judge call
(measured −45%). **C (run & decide) has started:** the first matched, replicated comparison ran on 2026-10-11 (the
production configuration against three current-generation models, judges on Opus 5.5; results in
`cost-optimization-candidates.md` §6). **Decided the same day (owner): production moved to Claude Haiku 5.5 at
effort high** — agent v3, scheduled deployment `depl_01YRoLodhcr3HuhVS3zxVuLx`, first send Mon 2026-10-12 06:07
Europe/Berlin; rollback = a deployment pinned to agent v2 (Haiku 4.5). Full plan, final candidate set (incl. the
aggressive-#3 split and the #5/#6 backburner), A UI requirements, and the decisions/flags log live
in `cost-optimization-candidates.md` (no longer imported here — read it directly).
**This branch was reconciled with `main` on 2026-07-07 (merge): it now carries the shipped
agent-system-redesign + the live ADR-0015 delivery cut-over described below.**

---

### Previous epic — agent-system-redesign + ADR-0015 delivery cut-over (SHIPPED + LIVE)

Status: **DONE (live).** The agent-system-redesign epic **shipped and merged to main (PRs #31/#32)**,
decoupling content generation from AWS delivery (the `deploy/delivery/` boundary, git-native
candidates in `deploy/candidates/`, the shared `cloud` environment; topology ADR-0014 Decision 1 =
**hybrid** — `cloud` for candidate/eval, `self_hosted` for production). Its deferred Phase 7 —
**ADR-0015 production delivery decoupling — was CUT OVER TO PRODUCTION 2026-07-07** (owner-approved,
PRs #34/#35/#36): production DELIVERY now runs on the `deploy/delivery/` boundary (contract v2), the
content MicroVM holds **zero AWS delivery IAM** (`deliveryDecoupled=true`; FR-1/AC-1), the scheduled
weekday deployment runs the decoupled `delivery_client.py` → `POST /deliver` prompt (new
`depl_01VP2gkocBheZF9dybQQ8aUN`; old in-VM deployment archived), and the welcome-send Lambda ships the
new chrome. Validated owner-only on the stripped role first (all four artifacts archived,
`SUBSCRIBER_FANOUT_SKIPPED`, `DELIVERY_SUCCEEDED`); first real subscriber send via the new path = Wed
2026-07-08 06:07 Europe/Berlin. Rollback: re-point the scheduled deployment at the archived in-VM
prompt + `cdk deploy ManagedAgentSandboxStack` WITHOUT `-c deliveryDecoupled=true` (image v13 still
has `audio_email.py`). The build-history detail below (Phases 1–6) predates the ADR-0015 refinements —
e.g. "`POST /deliver` locked" and "one fixed HTML template" are now superseded by contract v2 and the
refined template. **Built, each reviewed + security-cleared, on the merged branch `feat/agent-system-redesign`:**
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
