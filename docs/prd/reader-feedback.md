# PRD: Reader feedback form for the daily AI brief

- Status: Draft
- Author: product-manager (Claude)  ·  Date: 2026-07-03
- Linked ADRs: none yet — the Architect must write one for the feedback-link token
  design (signed, per-edition, identity-carrying) and one (or the same one) for the new
  standalone `deploy/feedback/` CDK app, mirroring how `public-subscriptions.md` spun up
  ADRs 0001–0003. Flagged in §7.

## 1. Problem

The daily narrated AI brief goes out every weekday to the owner (`mail@mschweier.com`) and
to a growing list of confirmed public subscribers. Today there is **no way for a reader to
tell us what they think of it** — whether the story selection is right, whether the coverage
is accurate and balanced, whether the length and technical depth suit them, or which sources
they wish we covered. The only signal we get is unsubscribes, which is a blunt, terminal, and
uninformative one. We are shipping an AI-curated product daily with effectively zero
structured reader feedback loop.

The owner wants a simple, low-friction way for any reader — subscriber or owner — to rate the
brief and leave suggestions, reachable straight from the email they just read, and traceable
(internally, in stored data) to **who** submitted it and **which day's edition** they were
reacting to, while still honoring a reader's choice to submit **anonymously**.

### Why now
The subscription surface is live and the subscriber base is growing, so the volume of readers
who *could* give feedback is increasing daily. The delivery email is already a personalized,
per-recipient artifact (the fan-out and the welcome email both build a per-recipient message),
so embedding a personalized feedback link is a small additive change to an existing code path
rather than new plumbing. Building the collection surface now — even before we decide what to
*do* with the data — means we start accumulating signal immediately instead of losing it.

## 2. Goals & non-goals

### Goals
- **A standalone public feedback web form** served over HTTPS at a new subdomain
  `feedback.mschweier.com`, deployed by a **new, self-contained CDK app** under
  `deploy/feedback/` (own CloudFront distribution, own site assets, own Lambda(s), own
  DynamoDB table, own least-privilege IAM) — structurally modeled on `deploy/subscribers/`
  but **not** bolted onto or sharing resources with the subscribers stack.
- **A feedback link in every daily brief email.** The same underlying email-building code path
  that produces the daily fan-out email (owner + subscribers) and the instant-welcome-brief
  email embeds a personalized feedback link, so readers reach the form in one click.
- **Per-edition, per-recipient traceability (internal, storage-only).** The link carries a
  token that identifies **(recipient identity, brief date)** so a stored submission can be
  attributed — internally, in the data only — to a specific reader and the specific edition
  they were reacting to. The token works for the **owner's own copy** (`mail@mschweier.com`),
  not just for subscribers who happen to have a DynamoDB row.
- **Honor anonymity.** The form offers a **"Provide feedback anonymously"** checkbox; when
  checked, the identity encoded in the link is **not persisted** with the submission — the
  stored record has **no reversible link** to who they are.
- **Work as a plain public form too.** Because `feedback.mschweier.com` is a public,
  un-access-controlled URL, the page must work with **no token in the URL at all** (a walk-up
  visitor), treated as anonymous by default (no identity to associate, no edition to attribute).
- **Collect a defined question set.** Seven graded questions on a 1–5 scale plus two free-text
  questions (see FR-6/FR-7), with a short note that feedback is valued and acted on where
  possible.
- **Durably store every submission** in a DynamoDB table and **confirm + thank the reader
  in-page** on submit.
- **Least-privilege, standalone IAM.** The feedback Lambda(s) need only DynamoDB write (and
  read for any operational needs) on the one new table — no SES grant, no reuse or extension
  of the subscribers stack's IAM roles.

### Non-goals (explicitly out of scope for this PRD/epic)
- **No analysis, action, aggregation, BI, or dashboard of the feedback data.** What we *do*
  with the collected feedback — how it's reviewed, summarized, or fed back into the brief — is
  explicitly **"discussion for later"** per the owner. This PRD covers **collection and durable
  storage only**. A DynamoDB table is sufficient; no analytics pipeline, no reporting UI, no
  admin console.
- **No response-side email.** Submitting feedback does **not** send any email (no
  acknowledgment email, no owner-notification email). Confirmation is **in-page only**. The
  feedback Lambda(s) therefore need **no SES rights**.
- **No changes to how the brief text or audio is produced**, to the daily schedule, to the
  fan-out recipient logic, to the subscribe/confirm/unsubscribe flows, or to the
  instant-welcome-brief behavior — other than the **single additive change** of embedding a
  personalized feedback link into the shared email-building path.
- **No CAPTCHA and no WAF for v1.** Abuse mitigation is a honeypot field + API Gateway
  throttling + basic input validation only — the same risk posture already accepted for the
  subscribe form. Revisit only if abuse is observed.
- **No user accounts, login, or passwords.** The form is anonymous-capable and requires no
  authentication.
- **No editing, retrieval, or deletion of a submission by the reader after submit** (no "view
  my past feedback", no self-service delete). One-way collection.
- **No changes to SES sandbox status, DNS ownership, or the registrar.** DNS records for the
  new subdomain are a documented, human-only manual step (see §6/§8).

## 3. Users & use cases

- **Subscribed reader (from an email)** — the primary path.
  - *US-1:* "As a reader who just read today's brief, I click the **'Share feedback'** link in
    the email, land on a form pre-associated with today's edition, rate the brief on a few
    dimensions, optionally suggest sources or leave a comment, submit, and see a thank-you —
    all without logging in."
  - *US-2:* "As a reader who wants to be candid, I check **'Provide feedback anonymously'** so
    my identity is not stored with what I said, and I trust that the stored record cannot be
    traced back to me."
- **Owner (from their own copy)** — must be a first-class feedback author too.
  - *US-3:* "As the owner, the feedback link in *my* copy of the brief also works and is
    attributed to me and today's edition, even though I'm not a row in the subscribers table."
- **Walk-up public visitor** — no email, no token.
  - *US-4:* "As someone who found `feedback.mschweier.com` directly (no link, no token), I can
    still submit feedback; it's simply recorded as anonymous with no edition attribution."
- **Owner / operator (data consumer, later)** — out of scope to *build for*, but the storage
  must serve them.
  - *US-5:* "As the owner, I can later look at the raw stored feedback and, for
    non-anonymous submissions, see who said it and about which edition — without any dashboard
    being in scope now, the durable record must capture identity + edition when not anonymous."
- **Reviewer / security-engineer**
  - *US-6:* "As a reviewer, I can verify that an anonymous submission persists **no** reversible
    identity, that a walk-up (no-token) submission is treated as anonymous, that a
    tampered/expired token is rejected without letting an attacker forge someone else's
    identity onto a submission, and that the feedback Lambda has DynamoDB-only, single-table,
    least-privilege IAM with no SES grant."

## 4. Functional requirements

Numbered; each maps to acceptance criteria in §5. "The system shall …".

### A. The feedback site (standalone CDK app)
1. The system shall serve a **public feedback web page** over HTTPS from a **new standalone
   CDK app** under `deploy/feedback/`, structured like `deploy/subscribers/` (own README, own
   `cdk.json` context pattern, own `site/` static assets, own Lambda(s), own DynamoDB table,
   own least-privilege IAM roles — **no** reuse or extension of the subscribers stack or its
   roles). The distribution shall have its **own CloudFront distribution**, intended for the
   `feedback.mschweier.com` subdomain, and shall serve on its default `*.cloudfront.net`
   domain until a custom domain + ACM cert are attached (FR-16).
2. The page shall be **lightweight** (not a marketing site) and meet basic **accessibility**
   expectations, matching the subscribe page's bar: labeled form fields, sufficient contrast,
   keyboard-operable, readable on mobile, no heavy client framework.
3. The page shall display a **short note stating that feedback is valued and that the team
   tries to implement it wherever possible.** Draft copy (low-risk PM decision, adopted as-is):
   > **Your feedback shapes this brief.** We read every submission and put your suggestions
   > into practice wherever we can. Thanks for helping make the Daily AI Brief better.
4. The form shall include a **hidden honeypot field**; submissions with the honeypot filled
   shall be **silently accepted-looking** but **not persisted** (no record created, response
   looks normal) — mirroring the subscribe form's honeypot behavior.

### B. The personalized feedback link (embedded in the brief email)
5. The **shared email-building code path** — the daily fan-out
   (`deploy/managed-agent/pipeline/audio_email.py`) **and** the instant-welcome-brief send
   (`deploy/subscribers/functions/welcome-send/`) — shall embed a **feedback link** to the
   feedback site in every recipient's email (owner and every subscriber), carrying a token
   that encodes **(recipient identity, brief date)**. The link shall also be embedded in the
   **owner's own copy**, so the owner's feedback is attributable even though the owner is not a
   subscriber row.

### C. The form's questions
6. The form shall present exactly **seven graded questions on a 1–5 scale** (1 = worst,
   5 = best; scale labelling is a PM decision, adopted as stated):
   1. **Overall rating**
   2. **Content selection** (were the right stories chosen?)
   3. **Content representation** (were stories framed fairly / neutrally?)
   4. **Content correctness** (was the information accurate?)
   5. **Content comprehensiveness** (was important context missing?)
   6. **Length** (too short / too long / about right — captured on the same 1–5 satisfaction
      scale, not a separate widget)
   7. **Technical depth** (too shallow / too deep — captured on the same 1–5 satisfaction
      scale)
   Each graded question shall be **optional** (a reader may submit having answered only some),
   so a partial rating is still captured rather than blocked.
7. The form shall present exactly **two free-text questions**:
   1. **"Which additional sources should the brief feature?"**
   2. **"Any other suggestions or feedback?"**
   Both shall be optional and length-bounded (server-side validated) to a reasonable cap.

### D. Anonymity and identity handling
8. The form shall include a **"Provide feedback anonymously"** checkbox. When it is
   **checked**, the submission shall be persisted with **no reversible link to the reader's
   identity** — neither the recipient email/identity nor any value from which it can be
   recovered shall be written to the feedback record. The **brief-date/edition** attribution
   (which is not personally identifying on its own) **may** still be stored on an anonymous
   submission (see FR-11 for the resolved decision on how edition attribution behaves under
   anonymity).
9. When the anonymity checkbox is **unchecked** and a **valid** token is present, the
   submission shall be persisted **with** the reader's identity (recipient email/id) and the
   brief date the token attests, so the owner can later see who said it and about which edition.
10. When the page is loaded with **no token in the URL** (a walk-up public visitor), the
    submission shall be treated as **anonymous by default**: no identity is associated
    (there is none to associate) and no edition is attributed. The anonymity checkbox is
    irrelevant in this case (there is nothing to suppress).
11. **[DECISION — resolved by PM, not an open question] Server-side abuse control may use the
    token without persisting identity.** When a submission is anonymous (checkbox checked, or
    walk-up), the server **may** validate and use the token **transiently in-request** for
    abuse/rate-limiting purposes (e.g. deriving a per-edition or per-recipient throttle key),
    **provided that** nothing derived from the identity portion of the token is **written to
    the persisted feedback record**. Concretely: the persisted anonymous record shall contain
    **no** recipient email, **no** recipient id, and **no** raw token; a **non-reversible,
    salted keyed hash** of the identity **may** be used only in-memory for throttling and shall
    **not** be stored on the record. The brief-**date** (edition) is not personally identifying
    and **may** be stored even on an anonymous record, since it enables edition-level rollups
    later without deanonymizing anyone. *(Rationale in §7 — this is the standard
    anonymity-vs-abuse-control tradeoff, decided here rather than deferred.)*

### E. Token validation and integrity
12. The feedback link token shall be **integrity-protected** so that the server can trust the
    (identity, brief-date) it asserts without a database lookup: a reader shall **not** be able
    to **forge, alter, or swap** the identity or the edition encoded in a token (e.g. submit
    feedback attributed to a different person or a different edition). A token that is
    **tampered, malformed, or fails its integrity check** shall be rejected — the submission
    is then handled exactly as the **walk-up anonymous** case (FR-10): recorded anonymously
    with no identity and no edition attribution, **never** with an attacker-supplied identity.
    *(Whether the token is an HMAC-signed value, or a signed value with an expiry, is an
    Architect decision — see §7. This PRD requires only the tamper-resistance property.)*
13. The token shall **not require a DynamoDB row to exist** for the identity it carries — it
    must self-attest identity for both subscribers **and** the owner. (This is why a stored,
    looked-up token like the subscribe/confirm token is insufficient on its own for the owner
    path; see §6/§7.)

### F. Submit, storage, and confirmation
14. On a valid submit, the system shall **durably persist one feedback record** to the new
    DynamoDB table capturing: the graded answers provided (FR-6), the free-text answers
    (FR-7), the anonymity flag as applied, and — **only when not anonymous** — the recipient
    identity and brief date (FR-9); plus a server-generated submission id and server-side
    timestamp. It shall **not** send any email (FR: no response-side email).
15. On a successful submit, the page shall display an **in-page confirmation / thank-you**
    state; on a validation failure (e.g. malformed payload, over-length free-text, honeypot),
    the reader shall get a **graceful outcome** (inline error for genuine validation problems;
    a normal-looking accepted response with no record for honeypot) with **no server error
    leaked**.

### G. IAM & abuse posture
16. The feedback stack's Lambda execution role(s) shall be **least-privilege and standalone**:
    `dynamodb:PutItem` (and any `Get`/`Query` strictly needed for in-request throttling) on
    **exactly the one new feedback table**, and **no** SES permission, **no** access to the
    subscribers table, the `cowork-polly-tts` bucket, or any subscribers-stack role. No static
    access keys.
17. The HTTP API fronting the submit Lambda shall be **throttled** at the stage level and
    shall enforce **basic server-side input validation** (payload shape, value ranges 1–5 for
    graded answers, free-text length caps, honeypot) — the same non-CAPTCHA posture as the
    subscribe form.

## 5. Acceptance criteria

Given/When/Then, testable against the new `deploy/feedback/` stack, the shared
email-building path, and the IAM in account `740353583786`, `us-east-1`.

### Site & form
- **AC-1 (site served):** Given the deployed feedback stack, When the CloudFront distribution
  URL (or `feedback.mschweier.com` once DNS is attached) is loaded, Then the feedback form
  renders over HTTPS with all seven graded questions (1–5), both free-text questions, the
  anonymity checkbox, and the "feedback is valued" note (FR-1, FR-2, FR-3, FR-6, FR-7).
- **AC-2 (honeypot):** Given a submission with the hidden honeypot field filled, When it is
  submitted, Then no feedback record is created and the response looks normal (FR-4).
- **AC-3 (partial graded answers):** Given a reader who answers only some graded questions and
  leaves the rest blank, When they submit, Then the submission is accepted and the answered
  values are persisted (FR-6).
- **AC-4 (free-text length cap):** Given a free-text answer exceeding the server-side cap,
  When submitted, Then it is rejected inline with no partial/oversized record written (FR-7,
  FR-15).

### Link in the email
- **AC-5 (link in fan-out):** Given a daily fan-out run, When a subscriber's email is built,
  Then it contains a feedback link to the feedback site carrying a token encoding that
  recipient's identity and the brief's date (FR-5).
- **AC-6 (link in welcome email):** Given a newly confirmed subscriber, When the
  instant-welcome-brief email is built, Then it contains a feedback link carrying that
  recipient's identity and the archived edition's date (FR-5).
- **AC-7 (owner link works):** Given the owner's own copy (`mail@mschweier.com`), When it is
  built, Then it contains a feedback link whose token attributes feedback to the owner and the
  brief date, even though the owner has no subscribers-table row (FR-5, FR-13).

### Identity, anonymity, and tokens
- **AC-8 (attributed when not anonymous):** Given a valid token and the anonymity checkbox
  **unchecked**, When feedback is submitted, Then the stored record contains the recipient
  identity and the brief date from the token (FR-9, FR-14).
- **AC-9 (anonymous suppresses identity):** Given a valid token and the anonymity checkbox
  **checked**, When feedback is submitted, Then the stored record contains **no** recipient
  email, **no** recipient id, and **no** raw token — with **no** field from which the identity
  can be recovered — while the answers are still persisted (FR-8, FR-11, FR-14).
- **AC-10 (walk-up anonymous):** Given the form loaded with **no token in the URL**, When
  feedback is submitted, Then it is stored as anonymous with no identity and no edition
  attribution (FR-10, FR-14).
- **AC-11 (tamper rejected, no forgery):** Given a token whose identity or date has been
  altered, or that is malformed/fails its integrity check, When feedback is submitted, Then
  the submission is stored **anonymously** (no identity, no edition) and is **never** stored
  with the attacker-supplied identity or edition (FR-12).
- **AC-12 (abuse-control uses token without persisting identity):** Given an anonymous
  submission with a valid token, When the server applies rate-limiting/abuse control, Then any
  identity-derived value used for throttling exists only in-request and is **not** written to
  the persisted record (only a non-identifying brief-date may appear) (FR-11).

### Storage & confirmation
- **AC-13 (durable record, no email):** Given a valid submit, When it succeeds, Then exactly
  one feedback record is written to the new DynamoDB table with the answers, anonymity flag,
  submission id, and timestamp, and **no** email is sent by the feedback path (FR-14).
- **AC-14 (in-page thank-you):** Given a successful submit, When the response returns, Then the
  reader sees an in-page thank-you/confirmation state without leaving the form site (FR-15).

### IAM & throttling
- **AC-15 (least-privilege IAM):** Given the feedback Lambda's role, When inspected, Then it
  grants only `dynamodb:PutItem` (plus any `Get`/`Query` strictly needed for throttling) on
  the one new feedback table, has **no** SES permission, **no** access to the subscribers table
  or the `cowork-polly-tts` bucket, uses **no** static keys, and reuses **no** subscribers-stack
  role (FR-16).
- **AC-16 (throttling + validation):** Given the submit endpoint, When inspected, Then the
  HTTP API stage is throttled and the Lambda enforces value ranges (graded answers 1–5),
  free-text length caps, and the honeypot check server-side (FR-17).

## 6. Constraints & dependencies

*(Items below are settled decisions for this epic — do not relitigate.)*

- **AWS account** `740353583786`, region `us-east-1` — confirm the active account before any
  deploy. **IaC: AWS CDK (Python)**, matching the existing stacks.
- **New standalone `deploy/feedback/` CDK app.** Structured like `deploy/subscribers/`: own
  `README.md`, own `cdk.json` context pattern with a `feedbackDomainName` key (and a
  `DEFAULT_FEEDBACK_DOMAIN` fallback of `feedback.mschweier.com`) plus a `certificateArn` key,
  own `site/` static assets, own Lambda(s), own DynamoDB table, own **least-privilege IAM
  specific to this stack**. It shall **not** reuse or extend the subscribers stack's resources
  or IAM roles, and shall have its **own** CloudFront distribution (not bolted onto
  `SubscribeSiteDistribution`). This is a separate deploy surface/lifecycle from
  `deploy/subscribers/` and `deploy/managed-agent/`.
- **No CAPTCHA / no WAF for v1.** Honeypot field + API Gateway stage throttling + basic
  server-side validation only — the same accepted risk posture as the subscribe form. Revisit
  only if abuse is observed. CORS on the HTTP API shall be **locked to the feedback site
  origin**, mirroring the subscribe stack's CORS lock.
- **DNS for `feedback.mschweier.com` is a human-only manual step.** The registrar for
  `mschweier.com` is external (not Route53); this sandbox has **no DNS API access**, exactly as
  documented for `briefing.mschweier.com` in `deploy/subscribers/README.md` §"DNS ... requires
  access this sandbox does not have". The two DNS records required (the ACM DNS-validation CNAME
  and the site-alias CNAME to the CloudFront `*.cloudfront.net` domain) must be added by the
  human. Everything else — infra, form, submit, storage — is built and validated end-to-end on
  the CloudFront default domain **before** DNS exists (see §8 sequencing). This is a rollout
  sequencing constraint, **not** a blocker for building the rest of the feature.
- **No response-side SES/email change.** The feedback response side sends no email, so the
  feedback Lambda(s) need **DynamoDB write (+ read for throttling) only — no SES grant.** The
  **only** email change is on the send side: the shared brief-email-building path embeds the
  personalized link. That send path (`audio_email.py` fan-out and `welcome-send/handler.py`)
  already runs where the token can be generated and already builds per-recipient HTML — this is
  an additive link, not new send infrastructure.
- **Token generation lives in / near the shared send path — exact home is an Architect
  decision (flagged, §7).** The link/token must be generated from **within** both send paths:
  `deploy/managed-agent/pipeline/audio_email.py` (daily fan-out, which has each recipient's
  email and the run's local date `_today_local_date()` in hand) **and**
  `deploy/subscribers/functions/welcome-send/handler.py` (welcome email, which has the
  recipient email + the archived edition's date). Because these are **two independent deploy
  units** (a microVM image and a Lambda layer), a shared token helper must be reachable from
  both — analogous to the hand-synced duplicated constants already documented between those two
  files (`MAX_AUDIO_ATTACHMENT_BYTES`) and to `instant-welcome-brief.md`'s "where the
  latest-brief read helper lives" open decision. **Input the helper needs:** the recipient
  email/identity, the brief date, and a **shared signing secret** (see next bullet). **Output:**
  a URL-safe token embedding (identity, brief-date) that the feedback stack can validate. The
  Architect must decide the helper's packaging (shared module copied into both units vs. a
  narrow new module) and place it cleanly for both.
- **Signed, self-attesting token — not the stored subscribe/confirm token.** The existing
  subscribe/confirm/unsubscribe tokens (`subscriber_common.generate_token`) are opaque random
  values **stored and looked up** against a DynamoDB row (ADR-0003). That model **cannot serve
  the owner**, who has no subscribers-table row, and does not by itself bind an **edition**.
  This feature therefore needs a **cryptographically signed** token that self-attests
  `(identity, brief-date)` without a DB lookup, so the feedback stack (a different app that does
  **not** read the subscribers table) can validate it (FR-12/FR-13). The signing secret must be
  a **new secret** (e.g. SSM Parameter/Secrets Manager) available to **both** the send-side
  token generator and the feedback-side validator, and **never committed** (existing
  credentials-never-committed convention). The exact signing scheme (HMAC, payload fields,
  whether to include an expiry) is the Architect's to specify in the ADR — this PRD fixes only
  the **requirements** (tamper-resistant, self-attesting, works for owner + subscribers,
  distinguishes valid-vs-invalid so an invalid token degrades to walk-up-anonymous).
- **Anonymity is a hard data-handling constraint, not a UI nicety.** An anonymous record must
  carry no reversible identity (FR-8/FR-11). The security review must verify no identity leaks
  into the persisted record via any field (including logs written at INFO on the persisted
  path) for anonymous submissions.
- **SES sandbox unchanged.** This feature adds no recipients and requests no SES production
  access; the send-side link embedding rides the existing owner + confirmed-subscriber sends.
- **Managed Agents beta / schedule unchanged.** No change to `deployment.json`, the agent, the
  skill, or the schedule — only `audio_email.py`'s per-recipient HTML gains a link.

## 7. Risks & open questions

- **[DECISION — resolved, PM] Anonymity vs. server-side abuse control.** Decided in FR-11: the
  server **may** use the token transiently in-request for rate-limiting (deriving a throttle key,
  e.g. a salted keyed hash of identity or a per-edition counter) but must persist **nothing**
  from which identity is recoverable on an anonymous record. Rationale: this is the standard
  privacy-vs-abuse tradeoff; suppressing identity in *storage* is what "anonymous" means to the
  reader and is what protects them, whereas denying the server any *in-flight* use of the token
  would forfeit the only cheap abuse signal we have (no CAPTCHA/WAF in v1) for no additional
  privacy benefit, since the transient value is never written down. Recorded here as a settled
  product/security decision — not deferred to the human.
- **[DECISION NEEDED — Architect] Token signing scheme.** The token must be tamper-resistant and
  self-attesting for both owner and subscribers (FR-12/FR-13). HMAC-signing `(identity,
  brief-date, [optional expiry])` with a shared secret is the recommended shape, but the exact
  payload encoding, whether to add an expiry (and its length — note a subscriber may legitimately
  open an old edition's link weeks later; an over-tight expiry would silently downgrade genuine
  late feedback to anonymous), and the secret's storage/rotation are the Architect's to specify
  in an ADR. Not human-strategic; flag for the ADR.
- **[DECISION NEEDED — Architect] Where the token helper lives and how both deploy units reach
  it.** As in `instant-welcome-brief.md`'s latest-brief-helper decision: the generator must be
  callable from both the microVM pipeline and the welcome-send Lambda, and a matching validator
  from the feedback stack. Favor a small, dependency-light helper; decide copied-module vs.
  shared-module packaging and where the shared signing secret is sourced from in each runtime.
- **Owner's copy has no unsubscribe row — identity source.** The owner is identified by the
  literal `mail@mschweier.com` (the `RECIP` constant), not a subscriber id. The token generator
  must handle "recipient identity" uniformly for a subscriber email and the owner email so
  AC-7/AC-8 both work; confirm the identity field chosen is stable and not itself sensitive to
  store when non-anonymous.
- **Edition attribution on anonymous submissions.** Storing the brief-date on an anonymous
  record (allowed by FR-11) enables edition-level rollups later without deanonymizing anyone —
  but the security review should confirm brief-date alone (a public calendar date shared by all
  that day's readers) is not, in this low-volume context, a quasi-identifier. Flagged as a
  review check, not a blocker.
- **Walk-up spam with no token.** The no-token public path (FR-10) is the most abusable surface
  (no identity to throttle on). Mitigations are stage throttling + honeypot + validation only
  (v1 posture); if abuse appears, revisit per the accepted "revisit only if observed" stance —
  out of scope to solve pre-emptively here.
- **Free-text as an injection/XSS vector.** Free-text answers are stored and will later be read
  by a human (and possibly rendered somewhere out of scope). The submit path must treat them as
  untrusted input (length-bound, stored as data, never reflected unescaped) — a standard
  validation/output-encoding concern for the security review.
- **Two-file link-embedding drift.** The link/header must be added to **both** send paths
  (`audio_email.py` and `welcome-send/handler.py`), which are hand-synced deploy units. Missing
  one means some emails lack the link; the reviewer should verify both carry it (AC-5/AC-6).

## 8. Rollout & metrics

- **Phasing.**
  1. **Feedback stack (infra + form + storage), on the CloudFront default domain.** Build the
     `deploy/feedback/` CDK app end-to-end: DynamoDB table, submit Lambda(s) with
     least-privilege IAM (FR-16), throttled HTTP API with locked CORS (FR-17), the static form
     (FR-1..FR-4, FR-6, FR-7), and the token **validator** (FR-12). Request the ACM cert for
     `feedback.mschweier.com` (DNS validation) but deploy without `certificateArn` first — the
     site is reachable on `*.cloudfront.net`, exactly as `deploy/subscribers/README.md`
     describes for the subscribe site.
  2. **Token generator in the shared send path.** Add the signed-token/link generation to
     `audio_email.py` and `welcome-send/handler.py` per the Architect's helper decision, wired
     to the shared signing secret (FR-5, FR-12, FR-13). **Do not yet point the link at the live
     `feedback.mschweier.com` host** — point it at whatever host is validated (the CloudFront
     default domain) during this phase, behind a config value.
  3. **End-to-end validation on the default domain.** Confirm: a fan-out email and a welcome
     email each carry a working link (AC-5/AC-6/AC-7); a not-anonymous submit stores identity +
     edition (AC-8); an anonymous submit stores neither (AC-9); a walk-up (no-token) submit is
     anonymous (AC-10); a tampered token degrades to anonymous, never forges identity (AC-11);
     honeypot/validation/throttle behave (AC-2/AC-4/AC-16); and the record is written with **no
     email sent** (AC-13/AC-14). The security review confirms IAM (AC-15) and the anonymity
     no-leak property before PR.
  4. **DNS cutover to the live subdomain (human-only, gated).** Only after phases 1–3 pass:
     the **human** adds the two DNS records (ACM validation CNAME + site-alias CNAME to the
     `*.cloudfront.net` domain) for `feedback.mschweier.com`, the stack is re-deployed with
     `-c certificateArn=<arn>` so CloudFront gets the alias, and **then** the send-path config
     value is flipped so the **live production** brief emails point at
     `https://feedback.mschweier.com`. Until the human completes DNS, production emails either
     omit the link or point at the validated default domain — the link is **not** wired to the
     live subdomain in production email templates before DNS is in place. This mirrors the
     subscribe stack's documented DNS deferral and is a **sequencing** decision, not a build
     blocker.
- **Ship gate.** AC-1..AC-16 pass on the CloudFront default domain; the security review confirms
  (a) the feedback Lambda's DynamoDB-only, single-table, no-SES least-privilege IAM (FR-16), and
  (b) that anonymous submissions persist no reversible identity (FR-8/FR-11) with no identity in
  logs on the persisted path; the token signing/validation ADR is recorded. DNS cutover
  (phase 4) is a post-gate human step and does not block the PR.
- **Success metric.** After ship: readers can submit feedback from the email link and from a
  walk-up visit; **100%** of anonymous submissions persist with no recoverable identity;
  non-anonymous submissions are correctly attributed to (reader, edition); a tampered/forged
  token **never** results in a misattributed submission; and there is **zero** regression to the
  daily fan-out, the subscribe/confirm/unsubscribe flows, the instant-welcome-brief, or the
  brief's content/audio/schedule. (Volume of feedback and what we learn from it is explicitly a
  later concern — this epic's success is "collection works and respects anonymity," not "we
  acted on N submissions.")
- **Handoff.** The Architect writes the ADR(s): the signed-token scheme + secret storage
  (FR-12/FR-13) and the `deploy/feedback/` standalone-app design + where the token helper lives
  for both send units (§7). The Developer then builds phases 1–2 across the new stack and the
  two send files, with the security-engineer reviewing the new IAM and the anonymity no-leak
  guarantee before PR. The human performs the DNS cutover (phase 4) post-merge.
