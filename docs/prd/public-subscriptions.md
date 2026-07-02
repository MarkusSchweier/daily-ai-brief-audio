# PRD: Public self-service subscriptions for the daily AI brief

- Status: Draft
- Author: product-manager (Claude)  ·  Date: 2026-07-01
- Linked ADRs: [0001 serverless architecture](../adr/0001-serverless-subscription-architecture.md),
  [0002 IAM & credentials for 2nd sender + fan-out](../adr/0002-iam-and-credentials-for-second-sender-and-fanout.md),
  [0003 data model & tokens](../adr/0003-subscriber-data-model-and-tokens.md)

## 1. Problem

Today the daily narrated AI brief (HTML body + MP3 attachment) is emailed to exactly one
hardcoded recipient — the owner (`mail@mschweier.com`) — via `deploy/audio_email.py`. Other
people have no way to receive it. We want interested people to **subscribe themselves** to the
same daily brief through a small public website, and to **unsubscribe just as easily**, with no
manual work by the owner and no accounts or passwords.

This must extend the existing live system **without regressing** the owner's own daily
delivery, which continues to run unchanged as steps 5–8 of the weekday scheduled task.

### Why now
The content and the audio+mail delivery pipeline are already live and stable. The only missing
piece to let others benefit is a compliant, self-service subscribe/confirm/unsubscribe surface
and a fan-out that mails confirmed subscribers alongside the owner.

## 2. Goals & non-goals

### Goals
- A lightweight public web page where a person can subscribe with **email + first name + last
  name** and learn what they are signing up for (daily, AI-focused, written + audio, unsubscribe
  anytime).
- **Double opt-in**: a subscribe submission triggers a confirmation email; only **confirmed**
  addresses are ever sent the brief. Unconfirmed signups **expire after ~48h**.
- Each daily brief run mails the brief to **all confirmed subscribers** in addition to the owner,
  with **per-recipient failure isolation** — one bad address never blocks anyone else.
- **One-click unsubscribe**, reachable both from the website and from a link in the footer of
  every brief email a subscriber receives. No login/password ever.
- The owner's existing delivery (`mail@mschweier.com`, from/to `mail@mschweier.com`, with MP3
  attachment and text-only fail-safe) keeps working **exactly as it does today**.

### Non-goals (explicitly out of scope for this PRD/epic)
- **No SES production access request.** Entire flow is built and tested in the **SES sandbox**
  using a handful of the owner's own additional verified personal addresses as test recipients.
  Requesting production access to email strangers is a separate LATER step.
- **No user accounts, login, or passwords** for any part of subscribe/confirm/unsubscribe.
- **No preference center** (no choosing topics, cadence, format, or delivery time).
- **No languages other than the brief's existing language.**
- **No analytics/tracking** beyond what is operationally necessary (e.g. delivery/bounce
  operational data); no marketing pixels, no third-party trackers.
- **No changes to how the brief text or audio is produced** (that is the `daily-ai-brief` skill,
  steps 1–4, and the Polly synthesis in step 6). Only the recipient set and email footer change.
- No admin dashboard / subscriber management UI (owner manages via the underlying data store if
  ever needed).

## 3. Users & use cases

- **Prospective subscriber** — visits the public page, wants to understand what the brief is
  before signing up, then submits their email + name.
  - *US-1:* "As a prospective subscriber, I can read on the page that this is a **daily**,
    **AI-focused** brief delivered as **written text + narrated audio**, and that I can
    **unsubscribe anytime**, so I know exactly what I'm opting into before I submit."
  - *US-2:* "As a prospective subscriber, I submit email + first name + last name and am told
    to check my inbox to confirm."
- **Confirming subscriber** — received the confirmation email and clicks the link.
  - *US-3:* "As a confirming subscriber, clicking the confirmation link activates my
    subscription and shows me a page that says I'm confirmed."
- **Subscribed reader** — receives the daily brief.
  - *US-4:* "As a confirmed subscriber, I receive the same daily brief the owner gets (HTML body
    + MP3), from `aibriefing@mschweier.com`, with a working unsubscribe link in the footer."
- **Unsubscribing subscriber** — no longer wants the brief.
  - *US-5:* "As a subscriber, I click the unsubscribe link in the email footer (or use the
    website) and am immediately unsubscribed and shown confirmation, with no login required."
- **Site owner** — depends on their own delivery never regressing.
  - *US-6:* "As the owner, my personal daily copy keeps arriving unchanged (same sender, same
    recipient, same attachment, same fail-safe) regardless of the subscriber flow."

## 4. Functional requirements

Numbered; each maps to acceptance criteria in §5. "The system shall …".

### Subscribe page
1. The system shall serve a **public web page** over HTTPS at a **subdomain of `mschweier.com`**
   (e.g. `briefing.mschweier.com`), reusing the existing verified domain/DNS.
2. The page shall clearly communicate, in the brief's existing language: the **topic** (AI),
   the **cadence** (daily), the **format** (written brief + narrated audio), and that the user
   can **unsubscribe anytime**.
3. The subscribe form shall collect exactly three fields: **email**, **first name**,
   **last name** — all required. No other required fields.
4. The page shall be **lightweight** (not a marketing site) and meet basic **accessibility**
   expectations: labeled form fields, sufficient contrast, keyboard-operable, works without
   client-side JavaScript frameworks beyond what the form submit needs, and readable on mobile.
5. The form shall include a **hidden honeypot field**; submissions with the honeypot filled
   shall be **silently dropped** (accepted-looking response, no email sent, no record created).
6. The system shall **validate email format** on submission and reject clearly invalid addresses
   with an inline error, without creating a record or sending mail.

### Subscribe / double opt-in
7. On a valid new submission the system shall create an **unconfirmed** subscriber record and
   send a **confirmation email** to that address from **`aibriefing@mschweier.com`**.
8. The confirmation email shall state what the person is confirming (daily AI brief, written +
   audio), include a **unique confirmation link**, and note the link **expires in ~48 hours**.
9. Clicking a valid, unexpired confirmation link shall mark the subscriber **confirmed** and
   display a **confirmation landing page** stating they are now subscribed and how to
   unsubscribe.
10. **Unconfirmed** records shall **expire ~48h** after creation and never receive the brief.
11. Confirmation and unsubscribe links shall use **non-guessable tokens** and require **no login
    or password**.

### Daily fan-out (extends `deploy/audio_email.py`)
12. Each daily brief run shall send the brief (HTML body + MP3 attachment, matching today's
    format) to **every confirmed subscriber**, from **`aibriefing@mschweier.com`**.
13. Each subscriber email shall include, in the **footer**, a **working one-click unsubscribe
    link** unique to that subscriber.
14. Sends shall be **failure-isolated per recipient**: a failure for one address (bounce, SES
    error, bad address) shall not prevent delivery to any other subscriber, and shall not affect
    the owner's copy. Failures shall be logged, not fatal — consistent with the existing "never
    lose the brief over a glitch" fail-safe.
15. The **owner's copy shall remain unchanged**: sent to `mail@mschweier.com`, from
    `mail@mschweier.com`, with the same MP3 attachment and the same text-only fail-safe on audio
    failure. The owner's copy shall **not** be gated on subscriber sends succeeding, and (unless
    the owner is separately a confirmed subscriber) shall **not** carry the subscriber
    unsubscribe footer.

### Unsubscribe
16. The system shall let a subscriber **unsubscribe with one click** from the email footer link,
    and also via the website, **without login**.
17. On unsubscribe the system shall mark the subscriber **unsubscribed** (stop all future sends)
    and display an **unsubscribe confirmation** page.
18. A subscriber who is unsubscribed shall be **excluded from the next and all subsequent** daily
    brief runs.

## 5. Acceptance criteria

Given/When/Then, testable end-to-end in the SES sandbox using the owner's additional verified
personal addresses as stand-in "subscribers".

### Happy path (end-to-end)
- **AC-1 (subscribe):** Given a verified test address that is not subscribed, When it is
  submitted with first + last name on the public page, Then an unconfirmed record is created and
  a confirmation email arrives from `aibriefing@mschweier.com` containing a unique confirm link.
- **AC-2 (confirm):** Given the confirmation email, When the confirm link is clicked within 48h,
  Then the record becomes confirmed and a landing page states the user is subscribed.
- **AC-3 (delivery):** Given a confirmed subscriber, When the next daily brief run executes,
  Then that address receives the brief (HTML body + MP3) from `aibriefing@mschweier.com` with an
  unsubscribe link in the footer.
- **AC-4 (unsubscribe):** Given a delivered brief, When the footer unsubscribe link is clicked,
  Then an unsubscribe-confirmation page is shown and the record is marked unsubscribed.
- **AC-5 (exclusion):** Given that unsubscribe, When the next daily brief run executes, Then that
  address does **not** receive the brief.

### Owner non-regression
- **AC-6:** Given any daily brief run (with zero, one, or many subscribers, and including runs
  where a subscriber send fails), When the run executes, Then the owner still receives their copy
  at `mail@mschweier.com` from `mail@mschweier.com` with the MP3 attachment, unchanged.
- **AC-7:** Given an audio (Polly) failure, When the run executes, Then the owner still receives
  the text-only fail-safe email exactly as today (existing behavior preserved).

### Failure isolation
- **AC-8:** Given three confirmed subscribers where one address is guaranteed to fail the SES
  send, When the daily run executes, Then the other two subscribers and the owner still receive
  the brief, and the failure is logged (run does not abort).

### Edge cases
- **AC-9 (already confirmed re-submits):** Given an already-**confirmed** address, When it is
  submitted again on the form, Then no duplicate active subscription is created and the user is
  shown a benign message (e.g. "you're already subscribed"); the system does not reveal error
  detail that leaks subscriber status beyond a neutral message.
- **AC-10 (unconfirmed re-submits):** Given an **unconfirmed** address whose link has not
  expired, When it re-submits, Then it is not duplicated and a confirmation email is (re)sent.
- **AC-11 (expired confirm link):** Given a confirmation link older than ~48h (or an expired
  unconfirmed record), When it is clicked, Then confirmation fails gracefully with a page telling
  the user to sign up again; the stale record does not become confirmed.
- **AC-12 (unsubscribe used twice):** Given an already-unsubscribed token, When the unsubscribe
  link is clicked again, Then the page still shows an unsubscribe confirmation (idempotent, no
  error, no re-subscription).
- **AC-13 (invalid email):** Given a syntactically invalid email, When the form is submitted,
  Then it is rejected inline with no record created and no email sent.
- **AC-14 (honeypot/bot):** Given a submission with the honeypot field filled, When submitted,
  Then it is silently dropped — no record, no confirmation email — and the response looks normal.
- **AC-15 (unsubscribe then re-subscribe):** Given a previously unsubscribed address, When it
  subscribes again via the form and confirms, Then it becomes confirmed and receives the brief
  again (unsubscribe is not a permanent block).

### Page content
- **AC-16:** Given the public subscribe page, When it is loaded, Then it visibly states topic
  (AI), cadence (daily), format (written + audio), and "unsubscribe anytime" before the form.

## 6. Constraints & dependencies

- **AWS account** `740353583786`, region `us-east-1` — confirm active account before any deploy.
- **Fully serverless AWS**, matching the existing Polly/S3/SES pattern; no standalone server.
- **IaC: AWS CDK (Python).**
- **SES stays in sandbox** for this build; all test recipients are the owner's own
  individually-verified addresses. No production-access request in this epic.
- **Subscriber-facing sender is `aibriefing@mschweier.com`** for confirmation, unsubscribe
  confirmation, and the subscriber copy of the daily brief. The owner's copy stays
  from/to `mail@mschweier.com`. (Note: `aibriefing@mschweier.com` must be usable as an SES From
  address — the existing IAM policy for `cowork-polly-tts` currently pins From to
  `mail@mschweier.com`; enabling a second sender is a design/permissions concern for the ADR.)
- **Domain/DNS:** reuse the existing verified `mschweier.com` identity; the subscribe page lives
  on a subdomain (e.g. `briefing.mschweier.com`).
- **Daily fan-out** extends the existing `deploy/audio_email.py`, triggered by the same
  Mac-based scheduled task (steps 5–8) — not a new Lambda. When that file changes, its inline
  copy in `~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md` must be updated in lockstep
  (existing repo convention).
- **Credentials never committed**; existing credential-chain conventions apply.
- **Downstream:** subscriber records live in a persistent store (recommended: DynamoDB) queried
  at fan-out time; the exact schema/tokens/expiry mechanism are the Architect's to specify.

## 7. Risks & open questions

- **Second SES sender / IAM scope.** The live least-privilege policy hard-pins SES From to
  `mail@mschweier.com`. Sending subscriber mail from `aibriefing@mschweier.com` requires
  broadening the SES permission (still least-privilege, e.g. both exact From addresses on the
  same domain identity) without loosening the owner path. **[DECISION NEEDED — Architect]** how
  to grant the second From cleanly (and whether the fan-out uses the same identity/credentials or
  a distinct one). Not a human-strategic decision; flagged for the ADR.
- **Compliance / list hygiene.** Even in sandbox, the flow must model correct opt-in/unsub
  behavior so it's production-ready later: functional unsubscribe in every email footer,
  double opt-in, no dark patterns. (Formal `List-Unsubscribe` header handling and bounce/
  complaint processing become materially important only at production access — call out for the
  LATER epic, not required to pass this PRD.)
- **Sandbox testing bound.** SES sandbox only delivers to verified addresses; every test
  "subscriber" must be pre-verified. Test plan must enumerate these addresses. This is by design
  and does not block acceptance.
- **Fan-out volume/timing.** Sandbox cap is 200 emails/day and subscriber count is tiny during
  this build, so throughput is a non-issue now; the ADR should still note the loop is
  failure-isolated and note where batching/throttling would go at production scale.
- **Token security.** Confirm/unsubscribe tokens must be non-guessable and not leak subscriber
  existence via differential responses — reflected in AC-9/AC-11.
- **Open question (design-level, Architect):** does the daily-run Mac scheduled task have
  network/IAM access to query the subscriber store (DynamoDB) at send time, and does the fan-out
  loop stay within the scheduled-task's runtime budget? Assumed yes; validate in the ADR.

## 8. Rollout & metrics

- **Phasing.** Build and validate entirely in the **SES sandbox** with the owner's verified test
  addresses. No public announcement / no real subscribers until a **separate LATER** epic obtains
  SES production access. The public page may be deployed but is only exercised by the owner's own
  test addresses during this epic.
- **Ship gate.** The full end-to-end chain passes with test addresses:
  subscribe → confirmation email arrives → confirm → next daily run includes the address →
  footer unsubscribe works → next daily run excludes the address — **and** the owner's copy is
  verified unchanged across all of it (AC-1 through AC-8, plus edge cases AC-9…AC-16).
- **Success metric (this epic):** 100% of the acceptance criteria pass in the sandbox using test
  addresses, with **zero** observed regression to owner delivery across at least two consecutive
  real daily runs.
- **Operational signal:** the fan-out logs per-recipient success/failure so a bad address is
  visible without inspecting inboxes; number of failed sends per run is the health signal.
- **Handoff:** Architect writes the design ADR (data store + token/expiry scheme + SES sender
  permissioning + how the fan-out queries subscribers within the existing scheduled task) before
  the Developer begins.
