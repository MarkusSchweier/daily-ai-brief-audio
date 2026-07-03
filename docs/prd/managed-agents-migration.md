# PRD: Migrate the daily AI brief pipeline to Claude Managed Agents

- Status: Draft
- Author: product-manager (Claude)  ·  Date: 2026-07-03
- Linked ADRs:
  [0004 AWS credentials for boto3 in the Managed Agents sandbox](../adr/0004-aws-credentials-for-boto3-in-managed-agents-sandbox.md) (**Accepted — Option B, self-hosted Lambda MicroVM**),
  [0005 external cross-run persistence store](../adr/0005-cross-run-persistence-store-for-brief-history.md),
  [0006 self-hosted Managed Agents environment + scheduled deployment](../adr/0006-managed-agents-environment-and-scheduled-deployment.md),
  [0007 porting the research/writing half into the Managed Agent](../adr/0007-porting-the-research-writing-half-into-the-managed-agent.md)

## 1. Problem

The daily AI brief runs today as a **Claude Desktop local scheduled task**
(`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`), firing weekdays at 6:07 AM local. It
does the full pipeline end-to-end: research the day's AI news across ~9 tiers of sources, write
and validate the Markdown brief, derive the HTML + a speech-optimized listening script,
synthesize a narrated MP3 via Amazon Polly, and email it via Amazon SES — the owner's copy to
`mail@mschweier.com` (unchanged, MP3 attached, text-only fail-safe) plus a failure-isolated
fan-out to every confirmed subscriber in DynamoDB table `brief-subscribers`. This repo is the
versioned source-of-truth for the audio/email half (`deploy/audio_email.py` mirrors STEP 6).

The pipeline's reliability depends on the owner's laptop. Per Anthropic's own docs
(https://code.claude.com/docs/en/desktop-scheduled-tasks), a **local Desktop scheduled task only
fires while the Desktop app is open and the Mac is awake**. If either is false at 6:07 AM, the
run is **silently skipped** — only a single catch-up run happens the next time the app/machine
wakes, discarding anything older. This has already caused a **real missed run**. The owner wants
this machine dependency **eliminated entirely**.

### Why now
The pipeline is live and stable, but its trigger is fragile in a way that has now visibly failed.
Anthropic offers a managed, cloud-hosted execution surface with **native scheduling** — Claude
Managed Agents — that removes the "laptop must be awake" dependency. Migrating the trigger and
runtime is the smallest change that fixes reliability without touching the brief's content logic
or the already-shipped subscriber feature.

## 2. Goals & non-goals

### Goals
- Run the **full daily brief pipeline** — both the research/writing half (today's steps 1–4,
  currently the separate `daily-ai-brief` skill) and the audio/email half (today's steps 5–8,
  `deploy/audio_email.py`) — **inside Claude Managed Agents**, with **no dependency on the owner's
  Mac being open or awake**.
- Use Managed Agents' **native scheduling** (the Deployments API `schedule.cron` + timezone) to
  fire the run on a weekday schedule directly — **no external trigger fires the *schedule***
  (no EventBridge/Lambda scheduling it). Note: because the runtime is a **self-hosted** sandbox
  (ADR-0004/ADR-0006), the native schedule firing causes Anthropic's webhook to invoke a
  **launcher Lambda in our own AWS account**, which boots the microVM that runs the session. That
  launcher Lambda is part of the self-hosted sandbox integration reacting to the webhook — it is
  **not** an external trigger of the *schedule* itself (the schedule stays native), so this does
  not contradict FR-4/AC-3.
- Stand up **"Claude Platform on AWS"** in AWS account `740353583786` (AWS Marketplace
  subscription + IAM-federated console access per Anthropic's docs) as an **in-scope early step**
  of this epic — it is not yet set up.
- Replace today's **local-file cross-run persistence** (the `Daily AI Briefs/` working folder,
  which Managed Agents sessions cannot share) with a **real external store** so the run can still
  read "yesterday's brief" and archive each day's output — Managed Agents sessions do **not**
  share filesystem state across runs.
- Reproduce today's output **byte-for-byte in intent**: the same Markdown brief, HTML, listening
  script, narrated MP3, and both email paths (owner copy unchanged; subscriber fan-out from
  `aibriefing@mschweier.com`, failure-isolated) — reusing the **existing** AWS resources.
- **Parallel-run** the new Managed Agents path alongside the existing local Desktop task (kept as
  a monitored fallback) for a validation period (~1–2 weeks), producing observable per-run
  success/failure signal, before any decision to retire the local task.

### Non-goals (explicitly out of scope for this PRD/epic)
- **Retiring or deleting the local Desktop scheduled task.** It stays running as a monitored
  fallback for the whole epic. Disabling it is a **separate follow-up decision**, gated on the
  parallel-run results — not part of this epic.
- **No changes to the brief content logic** — source tiers, research method, writing style,
  dollar/benchmark validation rules, listening-script optimization, Polly voice/settings. Only
  **where and how the pipeline is triggered and run** changes.
- **No changes to the subscriber-facing website or the subscribe/confirm/unsubscribe flow or
  anything under `deploy/subscribers/`.** That feature is complete and unaffected; the fan-out
  still reads the same table and sends the same mail.
- **No new or recreated downstream AWS resources.** The S3 bucket, SES domain identity + both
  verified senders, and the DynamoDB table already exist and are reused as-is.
- **No AWS service-limit increases** beyond what is already in place (e.g. SES stays in sandbox;
  no production-access request here).
- **No migration to Claude Code "Cloud routines"** and **no Amazon Bedrock** — both were
  considered and rejected (Bedrock does not support Managed Agents; Cloud routines are
  GitHub-clone-based and do not fit this file-based pipeline). Target is Managed Agents via
  "Claude Platform on AWS", decided.
- **No "keep the Mac awake" mitigations** (caffeinate/power settings) as the primary fix — the
  goal is to remove the machine dependency, not paper over it.

## 3. Users & use cases

- **Owner (operator/recipient)** — depends on the brief arriving every weekday without babysitting
  a laptop, and on their own copy never regressing.
  - *US-1:* "As the owner, the daily brief runs on schedule every weekday **even when my Mac is
    asleep, closed, or the Desktop app isn't open**, so I stop missing runs."
  - *US-2:* "As the owner, my personal copy keeps arriving unchanged — from/to `mail@mschweier.com`,
    with the MP3 attached and the text-only fail-safe on audio failure — exactly as today."
  - *US-3:* "As the owner, I can see whether each scheduled run **succeeded or failed** (and why)
    without having to check my inbox, via the Managed Agents run-history/webhook signal."
- **Confirmed subscriber** — receives the daily brief; must not notice the migration.
  - *US-4:* "As a confirmed subscriber, I keep receiving the same daily brief (HTML + MP3) from
    `aibriefing@mschweier.com` with a working unsubscribe footer, regardless of where the pipeline
    now runs."
- **Owner during cutover** — needs confidence before turning off the old path.
  - *US-5:* "As the owner, during the validation window I can run **both** the new Managed Agents
    path and the old local task and compare their output, so I only retire the local task once the
    new one is proven."
- **Future maintainer** — edits the pipeline later.
  - *US-6:* "As a maintainer, this repo remains the versioned source-of-truth: the Managed Agents
    deployment definition and any adapted run code live in the repo, and the STEP 6 code stays
    consistent with its counterpart, so I can change the pipeline from git."

## 4. Functional requirements

Numbered; each maps to acceptance criteria in §5. "The system shall …".

### Platform setup
1. The system shall have **"Claude Platform on AWS"** provisioned in AWS account `740353583786`
   (AWS Marketplace subscription active and IAM-federated console access working per Anthropic's
   docs), sufficient to create and run a Managed Agents deployment.
2. All Managed Agents API usage shall include the required beta header
   (`managed-agents-2026-04-01`), and the repo shall record which beta/API version the deployment
   was built against.

### Scheduling & execution
3. The system shall run the **full pipeline** (research + write + validate + derive HTML/listening
   script + Polly synthesis + SES owner-copy + subscriber fan-out) inside a **self-hosted**
   Managed Agents session (AWS Lambda MicroVM) — no step shall depend on the owner's Mac being
   open or awake.
4. The run shall be triggered by a Managed Agents **native scheduled deployment**
   (`schedule.cron` + timezone), on the **same weekday cadence and target local time** as today's
   6:07 AM run — with **no external trigger** (no EventBridge, Lambda, or Mac task) firing it.
5. The scheduled deployment shall be **pausable/unpausable** and expose **per-run history**
   (success/failure), so the owner can monitor it and pause it without deleting it.
6. The **self-hosted microVM** shall have **default public internet egress** — reaching Anthropic
   (`api.anthropic.com`), the AWS API endpoints the pipeline uses (`*.amazonaws.com` for Polly,
   S3, SES, DynamoDB), and the brief's news sources — with **no VPC or allowlist configuration
   required**. A VPC egress connector would only be needed to reach *private* resources, which is
   not applicable here (everything the pipeline touches is a public AWS or internet endpoint).

### Cross-run persistence (replacing the local working folder)
7. Because Managed Agents sessions **do not share filesystem state** across runs, the system shall
   persist each day's brief output to an **external store** (e.g. S3 or DynamoDB — Architect's
   choice) rather than a local folder, so nothing needed across days lives only in the ephemeral
   sandbox.
8. The run shall be able to **read the previous run's brief from that external store** to avoid
   repeating stories — reproducing today's "read yesterday's brief" behavior without the local
   `Daily AI Briefs/` folder.
9. Each run shall **archive that day's produced brief** (at minimum the Markdown; HTML/script as
   the Architect specifies) to the external store, so tomorrow's run can read it and the owner has
   a durable record.

### Output parity (reuse existing resources)
10. The Managed Agents run shall produce the **same daily artifacts** as today — Markdown brief,
    derived HTML, listening script, and narrated MP3 via **async Polly** (`OutputUri` to the
    existing bucket, never a hand-built key) — using the **existing** S3 bucket
    `cowork-polly-tts-740353583786`.
11. The run shall send the **owner's copy unchanged**: to `mail@mschweier.com`, from
    `mail@mschweier.com`, with the MP3 attachment and the **text-only fail-safe** if Polly fails —
    identical to today's behavior, and **not gated** on subscriber sends.
12. The run shall perform the **subscriber fan-out unchanged**: query the existing DynamoDB table
    `brief-subscribers` (GSI `status-index`) for confirmed subscribers and send each the brief
    (HTML + MP3) from `aibriefing@mschweier.com` with a working unsubscribe footer,
    **failure-isolated per recipient** (one bad address never blocks others or the owner), failures
    logged not fatal.
13. The migration shall **not recreate or modify** the downstream AWS resources (S3 bucket, SES
    domain identity + both senders, DynamoDB table + GSI); it reuses them as-is.

### Credentials & identity
14. The Managed Agents run shall authenticate to AWS via the **self-hosted microVM's IAM execution
    role** — short-lived, auto-rotating temporary credentials delivered through **IMDSv2** (the
    same mechanism a normal EC2 instance or Lambda function uses), picked up automatically by boto3
    and signed locally with real, valid credentials. That role shall carry **least-privilege**
    permissions scoped **verbatim to `deploy/iam-policy.json`** (Polly synth; S3 rw on the one
    bucket; SES send from `mail@mschweier.com` and `aibriefing@mschweier.com`; DynamoDB Query on the
    `brief-subscribers` GSI). This resolves the former open question: **no static AWS access key is
    created, injected, or stored anywhere**, so **boto3/SigV4-signed** AWS calls succeed with valid
    signatures.
15. No AWS secret shall be committed to git, printed in logs, or exposed in the repo — consistent
    with existing credential conventions. In the new path there is **no AWS static access key at
    all**; the only secrets are the **environment key** (worker auth) and the **webhook signing
    secret**, both held in **AWS Secrets Manager** and referenced **by name** in versioned IaC,
    never committed.

### Source-of-truth & consistency
16. The **Managed Agents deployment definition** (schedule, environment reference) **and the
    self-hosted CDK stack** (launcher Lambda, API Gateway webhook endpoint, microVM container image
    definition, Secrets Manager secrets, and the launcher + microVM IAM roles) shall be
    **versioned in this repo** as the source-of-truth for what runs — under a repo path like
    `deploy/managed-agent/`, alongside the existing `deploy/` artifacts.
17. Any change to the STEP 6 audio/email logic shall keep `deploy/audio_email.py` and its
    counterpart consistent — the existing lockstep-copy convention shall be preserved or explicitly
    updated to reflect the new runtime (the local `SKILL.md` inline copy remains the counterpart
    while the local task is still the fallback).

### Cutover (parallel run)
18. During the validation window, **both** the Managed Agents path and the existing local Desktop
    task shall be able to run, and the system shall not require the local task to be disabled to
    consider the Managed Agents path live.
19. The system shall provide an **observable per-run success/failure signal** for the Managed Agents
    path (run history and/or webhook) so the owner can judge reliability during the parallel run.

## 5. Acceptance criteria

Given/When/Then, testable in AWS account `740353583786`, region `us-east-1`, with SES in sandbox
and the owner's own verified addresses as stand-in subscribers.

### Platform & scheduling
- **AC-1 (platform up):** Given the epic starts with "Claude Platform on AWS" **not** provisioned,
  When setup is complete, Then the AWS Marketplace subscription is active and IAM-federated console
  access works, and a Managed Agents deployment can be created in account `740353583786`.
- **AC-2 (native schedule fires without the Mac):** Given a scheduled Managed Agents deployment and
  the owner's **Mac powered off (or asleep, and the Desktop app closed)**, When the scheduled time
  arrives, Then the run executes and completes — proving no dependency on the local machine.
- **AC-3 (no external trigger):** Given the deployment, When its trigger is inspected, Then it is
  fired by the Managed Agents native `schedule.cron` and **not** by any EventBridge/Lambda/Mac task.
- **AC-4 (monitorable & pausable):** Given a scheduled deployment, When the owner inspects it, Then
  per-run success/failure history is visible and the deployment can be paused and unpaused without
  being deleted.

### Cross-run persistence
- **AC-5 (yesterday readable from external store):** Given yesterday's brief was archived to the
  external store, When today's run executes in a **fresh sandbox with no shared filesystem**, Then
  it successfully reads yesterday's brief and uses it to avoid repeating stories.
- **AC-6 (today archived):** Given a run completes, When the external store is inspected, Then that
  day's brief output has been archived there (durably, not only in the ephemeral sandbox).

### Output parity
- **AC-7 (full pipeline output):** Given a scheduled run, When it completes, Then it has produced
  the Markdown brief, HTML, listening script, and a narrated MP3 (async Polly → existing bucket via
  `OutputUri`), equivalent in form to today's output.
- **AC-8 (owner copy unchanged):** Given any run (with zero, one, or many subscribers, including a
  run where a subscriber send fails), When it executes, Then the owner receives their copy at
  `mail@mschweier.com` from `mail@mschweier.com` with the MP3 attachment, unchanged, and **not**
  gated on subscriber sends.
- **AC-9 (audio fail-safe preserved):** Given a Polly failure during a run, When it executes, Then
  the owner still receives the **text-only fail-safe** email exactly as today.
- **AC-10 (subscriber fan-out unchanged):** Given confirmed subscribers in `brief-subscribers`, When
  a run executes, Then each receives the brief (HTML + MP3) from `aibriefing@mschweier.com` with a
  working unsubscribe footer.
- **AC-11 (fan-out failure isolation):** Given three confirmed subscribers where one address is
  guaranteed to fail the SES send, When a run executes, Then the other two subscribers and the owner
  still receive the brief and the failure is logged (run does not abort).
- **AC-12 (no resource recreation):** Given the migration, When downstream AWS resources are
  inspected, Then the S3 bucket, SES identity/senders, and DynamoDB table + GSI are the **existing**
  ones, unmodified and not recreated.

### Credentials & identity
- **AC-13 (boto3 calls succeed):** Given the microVM's IAM execution role (IMDSv2), When the run
  makes SigV4-signed AWS calls (Polly/S3/SES/DynamoDB), Then they succeed with valid signatures (the
  known invalid-signature failure mode of the credential-vault placeholder is avoided).
- **AC-14 (least-privilege, no leaked secret):** Given the microVM execution role, When its
  permissions are inspected, Then they are least-privilege equivalent to `cowork-polly-tts`
  (scoped verbatim to `deploy/iam-policy.json`); and the "no AWS secret in git/logs" criterion
  now trivially holds since there is **no AWS static key at all** in this path — additionally, the
  two Secrets Manager secrets (environment key, webhook signing secret) are also never committed
  (referenced by name in IaC).

### Source-of-truth & cutover
- **AC-15 (versioned in repo):** Given the migration is done, When the repo is inspected, Then the
  Managed Agents deployment definition and any adapted run code are committed as source-of-truth
  alongside `deploy/`, and STEP 6 logic remains consistent with its counterpart.
- **AC-16 (parallel run):** Given the Managed Agents path is live, When the validation window runs,
  Then the local Desktop task **still runs** as a monitored fallback and is **not** disabled by this
  epic, and both paths' outputs can be compared.
- **AC-17 (run signal observable):** Given a scheduled Managed Agents run (success or failure), When
  the owner checks, Then a per-run success/failure signal is available (run history and/or webhook)
  without inspecting the inbox.

## 6. Constraints & dependencies

- **AWS account** `740353583786`, region `us-east-1` — confirm active account before any deploy or
  mutation.
- **Target runtime: Claude Managed Agents via "Claude Platform on AWS"** (AWS Marketplace-billed,
  IAM-federated). **Not** Amazon Bedrock (no Managed Agents support) and **not** Claude Code "Cloud
  routines". This is decided; do not relitigate.
- **Managed Agents is in beta** — the beta header `managed-agents-2026-04-01` is required on all API
  calls; the API surface may change during the beta.
- **Self-hosted runtime on AWS Lambda MicroVMs** — the Managed Agents environment is `self_hosted`
  (per ADR-0004/ADR-0006), backed by our own AWS infrastructure (launcher Lambda + API Gateway
  webhook + microVM container image), built with **AWS CDK (Python)** adapting AWS's reference
  implementation (`aws-samples/sample-lambda-microvm-claude-managed-agents`). Not Anthropic's
  default `cloud` sandbox.
- **Native scheduling only** — trigger via the Deployments API `schedule.cron` + timezone; no
  EventBridge/Lambda/Mac trigger fires the *schedule* (the launcher Lambda is part of the
  self-hosted integration reacting to Anthropic's webhook, not a scheduler).
- **Environment/networking** — the self-hosted **microVM has default public internet egress**,
  reaching Anthropic, the AWS API endpoints (Polly/S3/SES/DynamoDB), and the news sources with **no
  VPC, NAT, or allowlist configuration**. A VPC egress connector is only needed for *private*
  resources, which is not applicable here. (Least privilege is enforced by the microVM IAM
  execution role, not a network allowlist.)
- **No shared filesystem across sessions** — the local `Daily AI Briefs/` working folder has **no
  equivalent** in Managed Agents. Cross-run state (reading yesterday's brief, archiving today's) must
  move to an external store. This is a required design change, not a lift-and-shift.
- **Reuse existing AWS resources, do not recreate:** S3 bucket `cowork-polly-tts-740353583786`; SES
  domain identity `mschweier.com` with both senders `mail@mschweier.com` and
  `aibriefing@mschweier.com` verified; DynamoDB table `brief-subscribers` with GSI `status-index`.
  SES stays in **sandbox**; owner's own verified addresses stand in as subscribers.
- **Credentials never committed / never printed** — existing credential conventions apply. The
  microVM execution role holds no static AWS credential to inject; the only secrets (environment
  key, webhook signing secret) live in Secrets Manager, referenced by name in versioned IaC.
- **Session runtime** — autonomous Managed Agents sessions on this platform run up to ~6 hours
  before needing a reauthentication event; a ~10-minute daily job is well within budget, so this is
  a **non-issue**, noted for completeness.
- **Downstream unchanged:** the subscriber website and `deploy/subscribers/` flow are complete and
  out of scope; the fan-out reuses the same table and sends the same mail.

## 7. Risks & open questions

- **Self-hosted build/operate effort — the main residual risk (credential question resolved).**
  The AWS-credential question is **decided** (ADR-0004, Accepted — Option B): the pipeline runs in a
  **self-hosted Managed Agents sandbox on AWS Lambda MicroVMs** and authenticates via the microVM's
  own **IAM execution role** (short-lived, auto-rotating credentials via IMDSv2) — **no static AWS
  key anywhere**. This is a security-posture *improvement* over today's local static-credential file,
  not merely a non-regression. The residual risk is now **build and operate effort**: this path
  requires **real new AWS infrastructure to build** — a launcher Lambda, a microVM container image,
  an API Gateway webhook endpoint, Secrets Manager secrets, and IAM roles (starting from AWS's
  reference implementation `aws-samples/sample-lambda-microvm-claude-managed-agents`, built with AWS
  CDK Python) — and **to operate**: webhook-secret rotation and monitoring of the launcher/microVM
  path. There is also **beta churn risk** specifically on the self-hosted sandbox + Lambda MicroVM
  integration (new/beta surface). The Architect/Developer build against the reference implementation
  and confirm the microVM execution role resolves via IMDSv2 with a live boto3 call.
- **Cross-run persistence design.** Moving "yesterday's brief" and archival off the local folder
  onto an external store (S3 vs DynamoDB, key/layout, retention) is a required design change; the
  Architect specifies the store and schema. Risk: subtle behavior differences vs. the local-file
  read (e.g. what counts as "yesterday" on a Monday after a weekend). Validate in the ADR.
- **Managed Agents beta.** The API/feature surface (Deployments API, environments, credential model)
  may change during the beta; the beta header pins a version. Risk of churn; the repo should record
  the version built against and the run should fail loudly (not silently) if the platform changes.
- **Reproducing the research half faithfully.** Today's steps 1–4 (the `daily-ai-brief` skill,
  ~9 source tiers, dollar/benchmark validation) live **outside this repo**. Migrating *where* they
  run must not alter *what* they do; the Architect/Developer must carry the same source list,
  validation rules, and listening-script optimization into the Managed Agents run. Risk of drift —
  parallel-run comparison against the local task is the mitigation.
- **Silent-skip parity check.** The whole point is that the native schedule fires without the Mac.
  AC-2 must be tested with the Mac genuinely off/asleep — not merely assumed from docs.
- **Cutover discipline.** The local task must **keep running** through the validation window; the
  epic must not disable it. Retiring it is a **separate follow-up decision** gated on parallel-run
  results — flagged here so it is not done prematurely.
- **Open question (design-level, Architect):** does the Managed Agents environment's default
  networking actually reach all required AWS endpoints and news sources in practice, and does the
  full run (research + Polly async wait + fan-out) complete within the session/run budget? Assumed
  yes; validate empirically in the first real runs.

## 8. Rollout & metrics

- **Phasing.**
  1. **Stand up the platform** — provision "Claude Platform on AWS" in `740353583786` (Marketplace +
     IAM federation), confirm a deployment can be created (AC-1).
  2. **Build the self-hosted stack** — provision the Lambda MicroVM environment (launcher Lambda,
     API Gateway webhook endpoint, microVM container image, Secrets Manager secrets, launcher +
     microVM IAM roles) per ADR-0004/ADR-0006, so boto3 authenticates via the microVM execution
     role (IMDSv2, no static key) before any run code is finalized (AC-13/AC-14).
  3. **Build the migrated pipeline** — research/write half + audio/email half running in a Managed
     Agents session, with external-store persistence replacing the local folder; versioned in repo.
  4. **Schedule it** natively (`schedule.cron`) and prove it fires with the Mac off (AC-2/AC-3).
  5. **Parallel run** the Managed Agents path and the existing local Desktop task for ~1–2 weeks,
     comparing output and monitoring per-run signal — local task stays on as fallback (AC-16).
- **Ship gate (this epic).** All acceptance criteria pass: the native schedule fires and completes a
  full run **with the Mac off** (AC-2), producing output equivalent to today (AC-7), owner copy
  unchanged incl. fail-safe (AC-8/AC-9), subscriber fan-out unchanged and failure-isolated
  (AC-10/AC-11), cross-run "yesterday" read + today archived from an external store (AC-5/AC-6),
  boto3 calls succeed under a least-privilege identity with no leaked secret (AC-13/AC-14), and the
  local task is still running as a monitored fallback (AC-16). **Retiring the local task is
  explicitly NOT part of this ship gate.**
- **Success metric.** Across the ~1–2 week parallel-run window: **100%** of scheduled weekday runs on
  the Managed Agents path fire and complete **without** any dependency on the Mac being awake, with
  **zero** regression to owner delivery and to subscriber fan-out vs. the local task's output over at
  least two consecutive weekday runs. The prior failure mode (silently skipped run because the laptop
  was asleep) does not recur on the new path.
- **Operational signal.** The Managed Agents run-history/webhook gives per-run success/failure
  without inbox inspection; the fan-out's per-recipient logging remains the delivery health signal.
- **Follow-up (out of this epic).** After a clean parallel-run window, a **separate decision**
  disables/retires the local Desktop scheduled task. This PRD neither disables it nor commits to a
  date.
- **Handoff.** Architect writes the design ADR(s) — covering (1) the AWS credential/identity
  mechanism — resolved: self-hosted Lambda MicroVM, IMDSv2 execution role (ADR-0004), (2) the external cross-run
  persistence store + schema replacing the local folder, (3) the Managed Agents environment +
  scheduled-deployment definition, and (4) how the migrated run reproduces the research half faithfully
  — before the Developer begins.
