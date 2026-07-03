# PRD: Instant welcome brief for newly confirmed subscribers

- Status: **Design complete (2026-07-03)** — human product decisions resolved and the Architect's
  sync-vs-async question resolved (§7: async-decoupled, ADR-0009 Accepted). Ready for the Developer.
- Author: product-manager (Claude)  ·  Date: 2026-07-03
- Linked ADRs:
  [0009 Decouple the welcome send from the confirm request path via async Lambda invoke](../adr/0009-async-welcome-send-decoupling.md)
  (**Accepted** — resolves §7's synchronous-in-confirm-path vs. async-decoupled question).
- Source: GitHub issue
  [#17 "New sign-ups should receive this day's brief right away"](https://github.com/MarkusSchweier/daily-ai-brief-audio/issues/17)

## 1. Problem

Today, when someone subscribes and clicks the confirmation link, they see a static "You're
subscribed" page and then **wait until the next weekday 06:07 send** to receive their first
brief. If they confirm just after a send (or on a Friday afternoon, or before a holiday),
that is a long, silent gap between committing to the product and getting any value from it —
the classic empty first-run experience.

The owner wants a new subscriber to get **the latest edition immediately** on confirmation,
with a short welcome header explaining that this is the current edition and that a fresh one
arrives every weekday at a stated time. The issue also asks to **parameterize the weekday
send time** so the welcome email can state it cleanly and the deployment schedule and the
email prose derive from **one** canonical value rather than two hand-maintained copies that
can silently drift.

### Why now
The public subscription feature is live and the subscriber list is growing. Every new
subscriber currently hits the empty-gap experience. This is a small, additive,
retention-focused change that builds on infrastructure that already exists (the S3 `briefs/`
archive, the daily Polly→SES send, the confirm Lambda) without touching the daily fan-out or
the schedule itself.

## 2. Goals & non-goals

### Goals
- **Send the latest edition on confirmation.** When a subscriber's status transitions
  `pending` → `confirmed`, immediately email them the most recently archived brief, prepended
  with a short welcome header, matching the format of a regular daily email (HTML body plus
  the narrated MP3 when one is available).
- **Reuse the existing audio, gracefully degrade.** Attach the MP3 that already exists for
  the most recent brief within the existing 7-day `audio/` lifecycle window. When no audio
  object exists for that brief (aged out, or a bad-audio day), send the welcome email
  **without** audio — matching the pipeline's existing "never lose the brief over an audio
  glitch" fail-safe.
- **Make the most recent brief locatable.** Persist a durable pointer to each day's actual
  MP3 S3 key (from Polly's `OutputUri`, never a hand-built key) alongside the existing
  `briefs/<date>/` archival, so a later reader can find it without reconstructing the key.
- **Centralize the weekday send time.** Define the weekday send time (06:07 Europe/Berlin) in
  one canonical, named source; the welcome email prose renders from it, and the live
  deployment schedule is verified to match it.
- **Stay least-privilege.** Grant the confirm Lambda only the new permissions it strictly
  needs (SES send from the one sender; S3 read scoped to the two relevant prefixes on the one
  bucket) — no broad grants.

### Non-goals (explicitly out of scope)
- **No change to the daily scheduled fan-out.** The weekday research → write → narrate →
  owner-copy → subscriber fan-out flow (`deploy/managed-agent/pipeline/audio_email.py` send
  path, the DynamoDB `status-index` query) is unchanged in behavior. The welcome send is a
  **separate, additional** send triggered by confirmation, not a modification of the fan-out.
- **No change to the weekday send time.** It stays **06:07 Europe/Berlin**
  (`deployment.json` cron `"7 6 * * 1-5"`, timezone `"Europe/Berlin"`). This PRD *centralizes*
  that value; it does **not** change it and does **not** build a two-way live-sync mechanism.
- **No change to `audio/` retention.** The 7-day S3 lifecycle expiry on the `audio/` prefix is
  unchanged. The welcome email reuses whatever MP3 still exists; it does **not** re-synthesize
  audio, and it does **not** copy the MP3 into a longer-retention location.
- **No change to the subscribe page UX or copy**, the subscribe (confirmation-request) email,
  or the unsubscribe flow. Nothing under `deploy/subscribers/site/` changes; the subscribe and
  unsubscribe Lambdas are untouched.
- **No change to SES sandbox status**, DNS, CloudFront, the DynamoDB schema, or the
  Managed Agents migration infrastructure (CDK stack, microVM, launcher, webhook, agent /
  deployment identity). The deployment's `agent.json` / `deployment.json` **agent id,
  environment, and skill reference** do not change.
- **Not a re-send / digest / backfill feature.** A subscriber receives the welcome brief
  **once**, on first confirmation. Re-clicking an already-confirmed link does not resend it.

## 3. Users & use cases

- **New subscriber (general audience)** — the reason for this change.
  - *US-1:* "As a new subscriber, the moment I confirm my email I receive the latest edition
    of the brief, with a short note that this is today's edition, so I get value immediately
    instead of waiting until the next weekday morning."
  - *US-2:* "As a new subscriber, that first email includes the narrated audio just like the
    daily ones, unless the audio isn't available — in which case I still get the written
    brief, not an error or nothing."
  - *US-3:* "As a new subscriber, the welcome tells me exactly when to expect future editions
    (weekdays at 06:07 Europe/Berlin), so I know what I signed up for."
- **Owner / operator**
  - *US-4:* "As the owner, the send time stated in the welcome email always matches the actual
    deployment schedule, because both come from one source I maintain in a single place."
  - *US-5:* "As the owner, the welcome send never breaks confirmation: if the welcome email
    fails to send, the subscriber is still confirmed and the confirm page still loads."
- **Reviewer / maintainer**
  - *US-6:* "As a reviewer, I can verify the confirm Lambda's new permissions are tightly
    scoped (one sender, two S3 prefixes on one bucket) and that a missing/expired MP3 produces
    a graceful audio-less send rather than a failure."

## 4. Functional requirements

Numbered; each maps to acceptance criteria in §5. "The system shall …".

### A. Locate the most recent brief and its audio
1. **Audio pointer at archive time.** The pipeline shall, when it archives a day's brief to
   `briefs/<date>/`, additionally persist a **durable pointer to that day's MP3** — the actual
   `audio/…` S3 key derived from Polly's `OutputUri` (never a reconstructed key) — as a small
   object under `briefs/<date>/` (e.g. `audio-pointer.txt`/`.json`). This is **additive** to
   the existing `brief.md` / `brief.html` / `listening-script.txt` archival and is written
   **only when audio synthesis for that run succeeded**; on an audio-failure day no pointer is
   written. Writing the pointer is **best-effort** (a failure is logged, never raised, never
   gates the send), consistent with `archive_todays_brief`'s existing fail-safe.
2. **Latest-brief read helper.** There shall be a read-only helper that resolves the **single
   most recent** archived brief and returns its `brief.html` body and, if present, the
   resolved MP3 S3 key from that brief's pointer. It shall **degrade gracefully**: return a
   clear "no brief archived yet" result (not an error) when the store is empty, and a
   "brief but no audio pointer" result when the pointer is absent. It reads HTML (not the
   research-oriented Markdown that the existing `read_recent_prior_briefs` returns).

### B. Welcome email on confirmation
3. **Send on first confirmation.** When the confirm flow transitions a subscriber from
   `pending` to `confirmed` (the existing successful-confirmation branch), the system shall
   send that subscriber a **welcome email** whose body is the most recent archived
   `brief.html`, prepended with a welcome header (FR-4), sent from `aibriefing@mschweier.com`.
4. **Welcome header copy.** The welcome header shall state, in plain language, that this is the
   latest edition and that new editions arrive on weekdays at the centralized send time.
   Concrete draft (final wording is a low-risk product detail, see §7):
   > **Welcome to the Daily AI Brief!** This is the most recent edition — the same one that
   > went out to subscribers today. Going forward, you'll receive a fresh edition every
   > weekday at **06:07 (Europe/Berlin)**.

   The "06:07 (Europe/Berlin)" value shall be rendered from the centralized send-time source
   (FR-10/FR-11), not hard-coded in the header string.
5. **Audio included when available, graceful when not.** The welcome email shall attach the
   MP3 for the most recent brief when the pointer resolves to an object that still exists in
   S3. When the pointer is absent **or** points to an object that no longer exists (expired
   under the 7-day `audio/` lifecycle, or otherwise gone), the email shall be sent **without**
   audio (written body only) — never failing or being withheld over a missing MP3.
6. **Consistent framing with daily emails.** The welcome email shall include the same
   AI-curation disclaimer and an unsubscribe footer that regular emails carry, using the
   subscriber's unsubscribe token generated during confirmation. (The welcome header from FR-4
   is in addition to, or in place of, the daily "received this as a forward?" prompt — exact
   composition is an implementation detail, provided the disclaimer and a working unsubscribe
   link are present.)
7. **Sent once, idempotent confirm.** The welcome email shall be sent **only** on the actual
   `pending` → `confirmed` transition. Re-clicking an already-confirmed link (the existing
   idempotent no-op branch) shall **not** resend the welcome email.
8. **First-ever confirmation, no brief archived.** When there is **no** archived brief at all
   (a cold-start store, before any brief has ever been archived), the system shall send a
   **welcome-only email** that confirms the subscription and states the schedule (weekday
   send time), with **no** brief content and **no** audio — so the subscriber always receives
   an acknowledgment. *(Decided, §7 — human confirmed 2026-07-03.)*
9. **Never blocks confirmation.** The welcome send shall be **failure-isolated** from the
   confirmation state transition: the DynamoDB update to `confirmed` and the confirm-page
   response shall succeed regardless of whether the welcome send succeeds, fails, or is
   skipped. A welcome-send failure is logged, not surfaced to the user as a confirmation
   failure.

### C. Centralized weekday send time
10. **Single canonical source.** The weekday send time (currently 06:07 Europe/Berlin) shall
    be defined in **one** named, canonical source, from which both the human-readable form
    used in email prose and the deployment schedule are derived or against which they are
    validated. The value shall not be independently hand-duplicated in the welcome email code
    and the deployment config as two free-standing literals.
11. **Prose renders from the source.** The welcome email's stated send time (FR-4, FR-8) shall
    be produced from the canonical source (FR-10), so changing the canonical value changes the
    email text without a separate edit.
12. **Schedule consistency guaranteed.** There shall be a documented, automatable consistency
    check that the live deployment schedule (`deployment.json` cron + timezone) agrees with the
    canonical send-time value. `deployment.json` shall remain **06:07 Europe/Berlin**
    (`"7 6 * * 1-5"`, `"Europe/Berlin"`) — unchanged by this PRD. *(Whether the deployment
    derives from the source or is merely validated against it is an implementation detail;
    a validating check is the recommended minimum since `deployment.json` is applied manually
    via the Deployments API and cannot import runtime code.)*

### D. Least-privilege IAM
13. **SES send for the confirm Lambda.** The confirm Lambda's execution role shall gain
    `ses:SendEmail` / `ses:SendRawEmail` (raw is needed for the MIME attachment) **restricted
    by an `ses:FromAddress` condition to `aibriefing@mschweier.com`**, mirroring the subscribe
    Lambda's existing scoped SES grant (`stack.py` `SesSendConfirmationFromAibriefing`). No
    broader SES access.
14. **Scoped S3 read for the confirm Lambda.** The confirm Lambda's role shall gain read
    access to the `cowork-polly-tts-740353583786` bucket **scoped to exactly the prefixes it
    needs**: `s3:ListBucket` limited to the `briefs/*` prefix (to find the latest dated
    folder), `s3:GetObject` on `briefs/*` (brief.html + pointer), and `s3:GetObject` on
    `audio/*` (the MP3 the pointer resolves to). No bucket-wide or account-wide grant, and no
    write permission. *(Note: `audio/*` read is required because the MP3 lives under `audio/`,
    not under `briefs/`; the parent brief kept audio under `audio/` deliberately to honor the
    7-day retention decision — so read scope is these two prefixes, not `briefs/` alone.)*

## 5. Acceptance criteria

Given/When/Then, testable against the confirm Lambda, the pipeline archival, and the IAM in
account `740353583786`, `us-east-1`.

### Locate the brief + audio
- **AC-1 (audio pointer written):** Given a scheduled run whose audio synthesis succeeded,
  When the brief is archived, Then a durable pointer object containing that run's actual
  `audio/…` S3 key (the `OutputUri`-derived key, not a reconstructed one) exists under
  `briefs/<date>/`, alongside the existing `brief.md`/`brief.html`/`listening-script.txt`, and
  a pointer-write failure does not fail the run (FR-1).
- **AC-2 (no pointer on audio-failure day):** Given a run where audio synthesis failed, When
  the brief is archived, Then no audio pointer is written for that date (and the read helper
  later treats that day as "brief but no audio") (FR-1, FR-2).
- **AC-3 (latest-brief helper):** Given ≥1 archived brief, When the read helper runs, Then it
  returns the single most recent brief's `brief.html` and, if a pointer exists, the resolved
  MP3 key; And Given an empty store, When it runs, Then it returns a "none archived" result
  without raising (FR-2).

### Welcome email
- **AC-4 (sent on first confirm):** Given a `pending` subscriber with a valid, unexpired
  token and ≥1 archived brief with available audio, When they confirm, Then exactly one
  welcome email is sent to them from `aibriefing@mschweier.com`, containing the most recent
  `brief.html`, the welcome header stating the weekday send time, the AI-curation disclaimer,
  a working unsubscribe link, and the MP3 attached (FR-3..FR-6).
- **AC-5 (graceful no-audio):** Given the most recent brief's audio pointer is absent or
  resolves to an object that no longer exists in S3, When a subscriber confirms, Then the
  welcome email is still sent with the written brief body but **no** MP3 attachment, and no
  error is surfaced (FR-5).
- **AC-6 (idempotent — no resend):** Given an already-`confirmed` subscriber, When they
  re-click the confirm link, Then the existing confirmed page is shown and **no** welcome
  email is sent (FR-7).
- **AC-7 (cold start):** Given **no** brief has ever been archived, When a subscriber
  confirms, Then they receive a welcome-only email confirming the subscription and stating the
  weekday send time, with no brief content and no audio (FR-8, decided).
- **AC-8 (confirmation never blocked):** Given the welcome send raises for any reason (S3
  read error, SES error, missing brief), When a subscriber confirms, Then their status is
  still updated to `confirmed`, the confirm page still returns success, and the failure is
  logged (FR-9).

### Centralized send time
- **AC-9 (one source, prose derives):** Given the centralized send-time source is changed,
  When the welcome email is generated, Then the stated time in the email reflects the new
  value without any other code edit (FR-10, FR-11).
- **AC-10 (schedule matches):** Given the consistency check runs, When `deployment.json`'s
  cron/timezone and the canonical value are compared, Then they agree (both 06:07
  Europe/Berlin); And `deployment.json` is unchanged by this feature (FR-12).

### IAM
- **AC-11 (SES scope):** Given the confirm Lambda's role, When inspected, Then it allows
  `ses:SendEmail`/`ses:SendRawEmail` only under an `ses:FromAddress == aibriefing@mschweier.com`
  condition and grants no other SES access (FR-13).
- **AC-12 (S3 scope):** Given the confirm Lambda's role, When inspected, Then it allows
  `s3:ListBucket` scoped to the `briefs/*` prefix and `s3:GetObject` only on `briefs/*` and
  `audio/*` of `cowork-polly-tts-740353583786`, with no write and no broader read (FR-14).

## 6. Constraints & dependencies

- **Cross-subsystem, two CDK apps.** This change spans **both** `deploy/subscribers/` (the
  confirm Lambda + its IAM role in `brief_subscribers/stack.py`) **and**
  `deploy/managed-agent/pipeline/` (the archival pointer in `brief_history.py`, wired from
  `audio_email.py`). These are two separate CDK stacks/apps in one repo; the confirm Lambda
  and the `cowork-polly-tts-740353583786` bucket are in the **same** AWS account
  (`740353583786`), so the S3 grant is same-account but **cross-stack** — call this out in the
  design.
- **Archival is single-place, not lockstep.** Verified against the current tree: the local
  Desktop scheduled task (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`) does **not**
  write to the S3 `briefs/` archive — only the Managed Agents pipeline does. Therefore the
  audio-pointer archival (FR-1) is added to the Managed Agents pipeline **only** and is **not**
  a lockstep-duplicated change (unlike the `audio_email.py` send logic). The `briefs/` store
  the confirm Lambda reads is populated by the Managed Agents path.
- **`OutputUri` invariant.** The MP3 key must always come from Polly's `OutputUri` (Polly
  inserts a dot before the TaskId; a hand-built key returns 403 because the policy omits
  `ListBucket` on `audio/`). The pointer (FR-1) captures that `OutputUri`-derived key so the
  confirm Lambda never reconstructs a key. This is a load-bearing existing convention.
- **7-day audio retention is fixed.** The pointer under `briefs/<date>/` (no expiry) will
  outlive the MP3 it points to on days older than 7 days — this is expected and is exactly the
  "pointer exists but object is gone → send without audio" path (FR-5/AC-5), not a defect.
- **SES sandbox.** Sending still requires the recipient to be a verified SES identity while the
  account is in the SES sandbox (same constraint as the subscribe/confirm and fan-out sends);
  production access is a separate, pre-existing follow-up and is out of scope here.
- **Managed Agents beta.** No change to the beta-pinned deployment; FR-12 only *validates*
  `deployment.json`, it does not re-version the agent or skill.
- **AWS account** `740353583786`, `us-east-1`. IAM changes get a least-privilege security
  review before PR (per the global manual); confirm the active account before any deploy.

## 7. Risks & open questions

- **[RESOLVED — Architect, 2026-07-03] Synchronous vs. asynchronous welcome send.** Decided:
  **async-decoupled.** The confirm Lambda, only on the actual `pending` → `confirmed` transition
  branch, **asynchronously invokes (`InvocationType='Event'`) a dedicated welcome-send Lambda**,
  passing a small metadata payload (email, first name, unsubscribe token); that welcome Lambda
  does the S3 read + SES `SendRawEmail`. Rationale: keeps the confirm page instant (the invoke
  returns as soon as the event is queued, independent of MP3 size / S3 / SES latency), makes the
  send independently retriable (async invoke's automatic retries + an on-failure DLQ destination),
  and *improves* least-privilege — the SES-send and scoped S3-read grants (FR-13/FR-14) move to
  the welcome Lambda's role while the confirm Lambda gains only `lambda:InvokeFunction` on that one
  target and never holds SES/S3 rights. Chosen over sync-inline (couples page load to S3/SES health,
  no retry, forces SES/S3 onto the public confirm Lambda), DynamoDB Streams (table-level config
  change, consumer must filter every write and reconstruct context, coarser retry — no benefit at
  this volume), EventBridge (extra bus/rule with no advantage here), and SQS (heaviest option, DLQ
  overkill for a human-paced flow). **Note for implementers:** FR-13/FR-14's SES + S3 grants now
  apply to the **welcome-send Lambda's** role, not the confirm Lambda's (a documented, intentional
  deviation from those requirements' literal wording — the security review should inspect the
  welcome Lambda's grants). Full rationale and alternatives in
  [ADR-0009](../adr/0009-async-welcome-send-decoupling.md).
- **[RESOLVED] Cold-start behavior (FR-8).** Decided: send a welcome-only email (confirm +
  schedule, no brief/audio) — a subscriber always gets an acknowledgment rather than silence.
  Adopts the PM's recommendation directly (low-stakes, only affects the very first
  confirmations before any brief exists).
- **[RESOLVED] Exact welcome header wording (FR-4).** Decided: use the drafted copy as-is —
  "**Welcome to the Daily AI Brief!** This is the most recent edition — the same one that
  went out to subscribers today. Going forward, you'll receive a fresh edition every weekday
  at **06:07 (Europe/Berlin)**." Adopts the PM's draft directly (low-stakes wording, can be
  revisited later without any structural change).
- **Where the latest-brief read helper lives (design).** It logically belongs beside
  `brief_history.py` (Managed Agents pipeline), but the *consumer* is the confirm Lambda in a
  different CDK app. The Architect should decide how the helper is packaged into the confirm
  Lambda (shared module in the common layer, a small copied helper, or a narrow new module) —
  favor a small, read-only, HTML-oriented helper over reusing the Markdown-oriented
  `read_recent_prior_briefs`.
- **Duplicate/near-simultaneous confirms.** If a subscriber clicks the confirm link twice in
  quick succession, only the first should send the welcome email (FR-7). The transition guard
  must key off the actual state change, not a best-effort check, to avoid a double welcome.
- **Latest brief vs. "today's" brief near midnight / on weekends.** "Most recent archived
  brief" is intentionally last-archived, not "today's" — a subscriber confirming on a Saturday
  gets Friday's edition, which is correct and matches the `read_recent_prior_briefs` "whatever
  actually exists" semantics. No date arithmetic.
- **Send-time source shape (minor).** `deployment.json` is applied manually and cannot import
  runtime code, so full "single-source derivation" for the cron is impractical; a validating
  check (FR-12) is the pragmatic canonical-consistency guarantee. Flagged so it isn't mistaken
  for a gap.

## 8. Rollout & metrics

- **Phasing.**
  1. **Archival pointer** — add FR-1 to the Managed Agents pipeline (`brief_history.py` +
     the `audio_email.py` wiring that has the `OutputUri` key in hand). Backward-compatible:
     days archived before this ships simply have no pointer and read as "brief, no audio."
  2. **Latest-brief read helper** — FR-2, read-only, gracefully-degrading.
  3. **Centralized send time** — FR-10..FR-12: canonical source + prose rendering + the
     `deployment.json` consistency check (schedule value unchanged).
  4. **Confirm Lambda welcome send + IAM** — FR-3..FR-9, FR-13, FR-14, per the Architect's
     sync/async decision (§7).
  5. **Validate** — confirm a test subscriber (SES-verified identity) receives the welcome
     email with audio; force the no-audio path (expired/missing pointer) and confirm graceful
     send; re-click to confirm no resend; simulate a welcome-send failure and confirm the
     subscriber is still `confirmed` and the page still loads.
- **Ship gate.** AC-1..AC-12 pass; the security review confirms the confirm Lambda's SES and
  S3 grants are scoped exactly as FR-13/FR-14 specify; the §7 architecture decision is recorded
  (ADR if async infra is chosen).
- **Success metric.** After ship: newly confirmed subscribers receive a welcome email within a
  short, acceptable window of confirming (target defined by the sync/async decision), including
  the current edition and its audio when available; **zero** confirmations are blocked or
  errored by a welcome-send failure; the send time stated in the welcome email always matches
  the live schedule; and no regression to the daily fan-out, subscribe/unsubscribe flows, or
  `audio/` retention.
- **Handoff.** The Architect should resolve the sync-vs-async welcome-send question (§7) and,
  if async infra is chosen, write the ADR; the human should confirm the cold-start behavior
  (FR-8) and welcome wording (FR-4). Then the Developer implements phases 1–4 across the two
  CDK apps, with the security-engineer reviewing the new IAM before PR.
