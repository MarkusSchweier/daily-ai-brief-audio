# Project Memory — Daily AI Brief: Audio & Mail

Loaded alongside the global operating manual (`~/.claude/CLAUDE.md`). Keep this to what's
**specific to this repo**; the workflow, roles, gates, and conventions are inherited globally.

---

## Project overview

- **Name:** daily-ai-brief-audio
- **What it does:** Researches/writes, narrates (Amazon Polly), and emails (Amazon SES) a daily
  AI news brief — to the owner and to a public list of self-service subscribers — plus a public
  reader-feedback form. Started as a narration/delivery layer bolted onto a separate brief-writing
  skill; has since grown into the whole pipeline's deployment source-of-truth.
- **Status: LIVE.** Three production surfaces, each its own deploy unit:
  1. **`deploy/managed-agent/`** — the current production path. A self-hosted Claude Managed
     Agents microVM runs the full weekday pipeline (research → write → narrate → send →
     archive) on a schedule, triggered by Anthropic's webhook. See its `README.md` for the
     full runbook (image builds, secrets, the Deployments API's create-new/archive-old
     update mechanism — deployments are immutable, no in-place update).
  2. **`deploy/subscribers/`** — the public subscribe/confirm/unsubscribe site at
     `briefing.mschweier.com` (CloudFront + API Gateway + Lambdas + DynamoDB), plus the
     instant-welcome-brief send on confirmation.
  3. **`deploy/feedback/`** — the public reader-feedback form at `feedback.mschweier.com`,
     reached via a personalized per-recipient/per-edition link embedded in every brief email.
  - **`deploy/delivery/`** (internal boundary, not a public surface) — the decoupled AWS **delivery
    boundary**: Polly narration, SES send, subscriber fan-out, S3 archival, and deterministic
    no-LLM Markdown→HTML, reachable only via a bearer-authed `POST /deliver` (+ `GET /deliver/{id}`
    poll, `GET /recent-briefs` read). **Built, deployed, live-validated, and — as of 2026-07-07 —
    the LIVE production delivery path (ADR-0015 cut-over DONE).** Production DELIVERY now runs on this
    boundary; the content MicroVM holds **zero AWS delivery IAM** (the `deliveryDecoupled=true` IAM
    strip is applied — `MicroVmExecutionRole` = env-key + logs + the two delivery-secret reads only).
    The scheduled weekday deployment runs the decoupled prompt (`delivery_client.py` → `POST /deliver`
    with `ENABLE_SUBSCRIBER_FANOUT=1`); the old in-VM `audio_email.py` deployment is archived. Runbook
    + rollback: `deploy/delivery/CUTOVER-production-decoupling.md`. `GET /recent-briefs` is also
    live-used by `cloud` eval candidates.
  - **The original local Claude Desktop scheduled task** (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`)
    is **DEAD** — retired by the owner (agent-system-redesign epic; ADR-0014 / ADR-0008's 2026-07-06
    amendment). It will not run and will not be reactivated; it is no longer a skill-content lockstep
    member (the lockstep is now two-way: in-repo ↔ live Skills-API). The top-level
    `deploy/audio_email.py`, `deploy/iam-policy.json`, `deploy/audio-mail-integration.md`,
    `deploy/scheduled-task-audio.md`, `deploy/validation-handoff.md` files describe *that* now-dead
    local path and are kept only as historical reference — the live production code is
    `deploy/managed-agent/pipeline/audio_email.py`, not the top-level copy.
- **Not this project:** the actual research/writing prompt logic lives in the `daily-ai-brief`
  skill (ported into `deploy/managed-agent/skills/daily-ai-brief/` for the microVM; the original
  skill definition is Claude Desktop-side, outside this repo).
- **Brief source (local fallback only):** `/Users/markus/Claude Working Folder/Daily AI Briefs/`.

## Repo layout (source-of-truth for what runs)

- `deploy/managed-agent/` — production pipeline: CDK stack (microVM, launcher Lambda, webhook,
  idempotency table), the ported skill, `pipeline/audio_email.py` (Polly→S3→SES + subscriber
  fan-out + feedback-link embedding), `deployment.json`/`agent.json` (Deployments API payloads).
  **`README.md` here is the detailed runbook** — image rebuilds, secret rotation, deployment
  updates.
- `deploy/subscribers/` — CDK stack for `briefing.mschweier.com`: subscribe/confirm/unsubscribe
  Lambdas, `brief-subscribers` DynamoDB table, the welcome-send Lambda. **`README.md`** has the
  full setup/DNS runbook.
- `deploy/feedback/` — CDK stack for `feedback.mschweier.com`: submit Lambda, `brief-feedback`
  DynamoDB table, the signed HMAC feedback-link token scheme (ADR-0011/0012). **`README.md`**
  has the full setup/DNS/secret-wiring runbook.
- `deploy/delivery/` — the decoupled AWS **delivery boundary** (ADR-0015): async `POST /deliver`
  (contract **v2** = brief markdown + listening script + candidates + source-usage), `GET
  /deliver/{id}`, `GET /recent-briefs`, the `brief-deliveries` table (tracking + a caller-idempotency
  dedup row, TTL'd), and `functions/deliver/` (`delivery_core.derive_html` + Polly + SES + fan-out +
  archival), bearer-gated by `delivery_auth` / signed-token `recent_briefs_auth`. Deployed +
  live-validated; **this is the LIVE production delivery path as of the 2026-07-07 ADR-0015 cut-over**
  (`CUTOVER-production-decoupling.md`). The MicroVM-side API client is
  `deploy/managed-agent/pipeline/delivery_client.py` (Option B: reads the bearer + mints the
  recent-briefs token via the MicroVM's own scoped role), **baked into microVM image v13**. The
  scheduled `deployment.json` now invokes `delivery_client.py` (→ `POST /deliver`, `ENABLE_SUBSCRIBER_FANOUT=1`),
  **not** `audio_email.py`, and the `MicroVmExecutionRole` delivery-IAM strip is **applied**
  (`deliveryDecoupled=true`). `audio_email.py` remains in image v13 only as the documented rollback
  path (re-point the scheduled deployment + redeploy the CDK without the flag).
- `deploy/candidates/` — git-native candidate declarations + `sync.py`/`trigger.py` for `cloud`
  eval/experimentation (agent-system-redesign epic); `deploy/eval-harness/` — the local-first,
  git-native evaluation harness (ADR-0016): run CLI, all-Opus web-verifying judges, per-thread
  cost, Flask UI as a local launchd service on 127.0.0.1:5151, git-tracked run records. Both are
  their own deploy units with their own `README.md`. *(The ORIGINAL AWS-native harness,
  `deploy/eval/` + `BriefEvalStack` (ADR-0013), was RETIRED and fully torn down on 2026-07-08 per
  ADR-0016 phase 5 — directory removed from the tree, stack/table/secrets/bucket deleted; its old
  eval records are archived at `docs/notes/brief-eval-records-export-2026-07-08.json` and the code
  remains in git history. Historical references to `deploy/eval/` in ADRs/PRDs and code comments
  are intentional provenance notes.)*
- `deploy/scheduled-task-audio.md`, `deploy/audio_email.py`, `deploy/iam-policy.json`,
  `deploy/audio-mail-integration.md`, `deploy/validation-handoff.md` — the **now-dead local Desktop
  path** (retired, not reactivated — agent-system-redesign epic; kept as historical reference only).
- `docs/prd/`, `docs/adr/` — PRDs and ADRs for every epic; `docs/prd/_active.md` points at the
  current one and is auto-imported below.

## AWS resources (personal account `740353583786`, us-east-1)

Each stack's own `README.md` is the detailed source of truth (resources, IAM, DNS/cert setup,
secret rotation). Summary:

- **`ManagedAgentSandboxStack`** (`deploy/managed-agent/cdk/`) — microVM image/execution roles,
  launcher Lambda + webhook API, idempotency DynamoDB table, Anthropic environment-key +
  webhook-signing secrets.
- **`BriefSubscribersStack`** (`deploy/subscribers/`) — `brief-subscribers` DynamoDB table,
  subscribe/confirm/unsubscribe Lambdas, welcome-send Lambda, CloudFront site at
  `briefing.mschweier.com`.
- **`FeedbackStack`** (`deploy/feedback/`) — `brief-feedback` DynamoDB table, submit Lambda, the
  shared feedback-token signing secret, CloudFront site at `feedback.mschweier.com`.
- **`BriefDeliveryStack`** (`deploy/delivery/`) — `brief-deliveries` DynamoDB table (delivery
  tracking + idempotency dedup, `expiresAt` TTL), the delivery-bearer + recent-briefs-read-token
  secrets, the single `brief-delivery-deliver` Lambda (derive HTML → Polly → SES → archive), HTTP API
  `POST /deliver` + `GET /deliver/{id}` + `GET /recent-briefs`. Its role holds the delivery grants
  (Polly/S3/SES/DynamoDB-Query) that will move **off** `MicroVmExecutionRole` at the ADR-0015
  cut-over. **Not yet on the production delivery path** — production still delivers in-VM.
- IAM user `cowork-polly-tts` — the **original, now-fallback-only** least-priv user (Polly synth
  + S3 rw on one bucket + SES send from `aibriefing@mschweier.com` + a GSI-scoped DynamoDB Query)
  used by the local Desktop task. Policy: `deploy/iam-policy.json`.
- S3 bucket `cowork-polly-tts-740353583786` — SSE-S3, public access blocked, 7-day lifecycle
  expiry on `audio/`, plus the `briefs/` archive (brief.html + audio pointer) the feedback/welcome
  paths read from.
- SES domain identity `mschweier.com` (DKIM-verified); all mail sends from
  `aibriefing@mschweier.com`. Owner still receives at `mail@mschweier.com`. **Still SES
  sandbox** — production access is a tracked, not-yet-done follow-up (see
  `deploy/subscribers/README.md`).

## How to validate / change

Each subsystem has its own test suite and CDK app (`cdk synth`/`cdk diff`/`cdk deploy`, run from
within `deploy/managed-agent/cdk/`, `deploy/subscribers/`, or `deploy/feedback/` respectively) —
see that subsystem's `README.md` for the exact commands. The now-dead local-fallback files
(`deploy/audio_email.py`, `deploy/iam-policy.json`, etc.) are kept only as historical reference —
they are no longer maintained, no longer in lockstep with anything, and must not be edited to "keep
the Desktop copy in sync" (the Desktop task is dead; there is no Desktop copy to sync). If you ever
need to sanity-check them: `python3 -m py_compile deploy/audio_email.py` /
`python3 -m json.tool deploy/iam-policy.json`. The live production copy is
`deploy/managed-agent/pipeline/audio_email.py` — that is the one that matters.

## Conventions

- **Credentials are never committed.** They live outside git — the sandbox reads
  `<working folder>/.aws-cowork/credentials` via `AWS_SHARED_CREDENTIALS_FILE`, or env vars via
  `~/.claude/settings.json`. Never print or commit `AWS_SECRET_ACCESS_KEY`. Account-id-in-bucket
  name is expected; the live IAM access-key **ID** in the migrated runbook is redacted.
- **Async Polly only**; **use `OutputUri`, never build the S3 key**; **SES From must be exactly
  `aibriefing@mschweier.com`**. Fail-safe: never lose the brief over an audio/email glitch.
- Confirm the active AWS account before any deploy/mutation (`/aws-account`, `aws-account-safety`).

## Active planning docs (auto-imported)

@docs/prd/_active.md
@docs/notes/_latest-checkpoint.md
