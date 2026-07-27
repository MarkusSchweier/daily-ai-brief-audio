# PRD: Audio-as-link delivery (stop attaching the MP3)

- Status: **Draft (2026-07-27)** — awaiting owner gate, then architect review.
- Author: product-manager (Claude) · Date: 2026-07-27
- Linked ADRs: none yet — **an ADR is recommended** (a new public read path on the existing
  `briefing.mschweier.com` distribution, a second CloudFront origin, and a resource-policy change
  on the shared, non-CDK-managed pipeline bucket). Architect to confirm.
- Scope boundary: **delivery boundary only** (`deploy/delivery/` + `deploy/subscribers/`). The
  content pipeline (`deploy/managed-agent/`, the microVM, the `daily-ai-brief` skill, the
  `POST /deliver` contract v2) is **not touched**.

## 1. Problem

Every daily brief email carries the narrated edition as an **MP3 attachment** — built in
`delivery_core._build_message()` for the owner's copy and for **every** subscriber copy, and again
in the welcome-send Lambda (`deploy/subscribers/functions/welcome-send/handler.py`) for each new
confirmation. A typical narrated brief is multiple megabytes, so:

- **Subscriber inboxes fill up.** A daily multi-MB attachment is a recurring, compounding storage
  cost imposed on every reader — the single most likely reason a subscriber mutes or unsubscribes
  for a reason that has nothing to do with the content.
- **Large attachments are fragile.** The code already carries a defensive
  `MAX_AUDIO_ATTACHMENT_BYTES = 15 MB` cut-off in **both** deploy units: past that, the attachment
  is silently **dropped** to avoid an SES raw-message-size rejection, and the reader gets **no
  audio at all** with no explanation.
- **It costs money per send.** SES charges $0.12/GB of attachment data, multiplied by the
  subscriber count, every weekday.
- **The MP3 is already in S3 anyway.** Polly writes it to `cowork-polly-tts-740353583786`
  under `audio/`, the key is already archived as `briefs/<date>/audio-pointer.json`, and the
  bucket already has a 7-day lifecycle expiry on `audio/`. The email re-ships bytes that already
  exist at a durable location — it just isn't reachable from a browser today.

Replacing the attachment with a **link that streams on click** removes all four problems at once,
for readers and for the sender, with no change to what is narrated or written.

### Why now
The delivery boundary is live and is now the **only** thing that emails a real subscriber
(ADR-0015 cut-over, 2026-07-07). Email composition is therefore concentrated in exactly two
places — `delivery_core.py` and the welcome-send Lambda — instead of being smeared across the
microVM. This is the cheapest this change will ever be. The subscriber list is also still small
(SES sandbox), so the reader-visible change lands before it affects many people.

## 2. Goals & non-goals

### Goals
- **Zero MP3 attachments.** No daily brief email, subscriber copy, welcome email, or eval email
  carries an MP3 attachment after this ships.
- **One-click listening from the top of the email.** A plain, direct link to the MP3 in the email
  **header area**, opened by the browser's native audio player, streamed progressively (no full
  download before playback starts).
- **Honest expiry.** The email states the **exact date the audio link stops working**, so a reader
  who clicks an old edition understands the 404 rather than reporting a broken product.
- **Fail-safe preserved.** A Polly failure, a missing object, or a link-derivation failure **never**
  blocks the brief. The reader always gets the written brief, plus an explicit "no audio this
  edition" line instead of silence.
- **No new secrets, no new auth surface, no per-recipient tokens.** The audio is not private.
- **Cheaper, not just nicer.** SES attachment-data charges go away; CloudFront delivery at this
  scale sits inside the always-free tier (owner-validated).

### Non-goals (explicitly out of scope)
- **No player page.** No hosted HTML page with a styled player, no waveform, no transcript view.
  A direct MP3 URL, handed to the browser.
- **No signed URLs, no CloudFront signed cookies, no per-recipient audio links.** The URL is
  public-but-unguessable, identical for every recipient of an edition. Nothing in the audio is
  private (owner decision, locked).
- **No lifecycle change.** The 7-day `audio/` expiry stays exactly as it is. The email adapts to
  the lifecycle; the lifecycle does not adapt to the email.
- **No content-pipeline change.** No new fields on `POST /deliver`, no skill edit, no microVM image
  rebuild, no change to what Polly narrates.
- **No listen analytics.** No click tracking, no per-recipient attribution, no download counts
  beyond whatever CloudFront logs by default (and no new logging is being enabled for this).
- **No change to the `briefs/` archive shape** beyond what already exists (the audio pointer is
  already written; it is now also *read* by the daily path for expiry, not just by welcome-send).
- **No bespoke "this audio has expired" landing page.** A 404 is accepted (owner decision).
- **No retroactive fix-up of already-sent emails.** Editions already in inboxes keep their
  attachments.

## 3. Users & use cases

- **Subscriber (public list, primary).** Opens the brief on a phone at breakfast, taps "Listen"
  at the top, audio starts playing within a second or two while the rest downloads; scrolls the
  written brief while it plays. Their inbox no longer grows by several MB every weekday.
- **Subscriber, one week later.** Digs out last Tuesday's brief, taps the link, gets a 404 — and
  the line right next to the link already told them the audio expired on that date, so it reads as
  designed behaviour, not breakage.
- **New subscriber (welcome email).** Confirms their address, immediately receives the latest
  edition. That edition may be up to seven days old, so its audio may have one day of life left —
  or be gone already. The welcome email states the **real** remaining expiry of that specific file,
  never a generic "seven days from today".
- **Owner (`mail@mschweier.com`).** Receives the same email shape as subscribers, plus the existing
  post-send confirmation summary. Needs to see at a glance when an edition went out without audio.
- **Eval runs (`POST /deliver` owner-only mode).** Evals do not synthesize audio at all
  (cost-optimization epic decision) — they must keep working unchanged and must show the no-audio
  line rather than a broken link.

## 4. Functional requirements

### A. Serving the audio publicly

- **FR-1.** The system shall serve `audio/*` objects from the existing pipeline bucket
  `cowork-polly-tts-740353583786` over HTTPS through the **existing CloudFront distribution** that
  already serves `briefing.mschweier.com`, via a new `/audio/*` cache behavior whose origin is that
  bucket, accessed with **Origin Access Control**.
- **FR-2.** The bucket's **public-access block shall stay enabled** and no object shall be made
  public. The only new grant shall be a bucket-policy statement allowing the CloudFront service
  principal (scoped to this distribution) to read **`audio/*` and nothing else** — `briefs/`
  (archived brief HTML/Markdown, listening scripts, candidates, source-usage records) and every
  other prefix shall remain unreachable through CloudFront.
- **FR-3.** The public URL shall be derived from the object's **actual Polly `OutputUri`-derived
  key** (the repo's standing invariant: use `OutputUri`, never hand-build the S3 key). No new key
  format, no renaming, no copy-to-a-second-prefix. Unguessability comes from the Polly-generated
  task identifier already embedded in the key; the implementation shall **verify against a real
  live object** that this identifier is high-entropy before the email change ships.
- **FR-4.** A request to a valid audio URL shall return `200` with `Content-Type: audio/mpeg` and
  `Accept-Ranges: bytes`, and a `Range` request shall return `206 Partial Content` — i.e. the
  browser's native player can start playback before the file has fully downloaded.
- **FR-5.** A request for an audio object that no longer exists (expired under the 7-day lifecycle)
  or never existed shall return HTTP status **404**. No bespoke expired-audio page is in scope.

### B. The daily brief email

- **FR-6.** The daily brief email shall carry **no MP3 attachment** — for the owner's copy, for
  every subscriber copy, and for eval sends alike.
- **FR-7.** The email shall carry an **audio link in the top header/meta box** (the existing
  `_HEADER_SLOT` box that already carries the subscribe/unsubscribe/feedback/disclaimer lines), as
  a plain `<a href>` to the MP3 URL, positioned as the **first line of that box** so it is the
  first thing a reader reaches.
- **FR-8.** The audio line shall display the **exact expiry date of that specific audio object**,
  read from the object's **real S3 lifecycle expiry metadata** — never computed as "send date + 7".
  The date shall be rendered in the **Europe/Berlin** timezone (the pipeline's own
  `PIPELINE_TIMEZONE`) and formatted with an **unambiguous, spelled-out month** — e.g.
  `4 August 2026` — never a purely numeric form (the audience is German/European; `04/08/2026` is
  ambiguous). No time-of-day is shown.
- **FR-9.** The link shall resolve to **that edition's own MP3** — the object produced by that
  run's synthesis — not the latest audio, not a fixed alias.
- **FR-10.** **Fail-safe (preserves today's behaviour).** If audio synthesis fails, or the audio
  object cannot be resolved, or the expiry cannot be read, or the audio base URL is unconfigured,
  the email shall **still be sent**. In that case the header box shall show a **single short,
  non-linked line stating that no audio is available for this edition** (rather than silently
  omitting the line — with attachments gone, silence would be indistinguishable from a bug).
  Sub-case: if the object exists and the link is buildable but only the **expiry date** is
  unreadable, the link shall still be shown, with a generic availability note instead of a date.
- **FR-11.** The `MAX_AUDIO_ATTACHMENT_BYTES` size cut-off shall no longer suppress audio: an
  oversized MP3, which today is silently dropped from the email, shall now still be **linked**
  (there is no message-size risk in a link).
- **FR-12.** The delivery request contract shall be **unchanged** — no new `POST /deliver` fields,
  no caller change, no microVM image rebuild required to ship this.

### C. The welcome email

- **FR-13.** The welcome email (`deploy/subscribers/functions/welcome-send/`) shall follow the same
  rules as the daily email: no MP3 attachment, an audio link as the first line of its meta box, and
  the exact expiry date of the audio file it links to.
- **FR-14.** Because the welcome email sends the **latest archived edition**, whose audio may be up
  to seven days into its lifecycle, the displayed date shall be **that file's real remaining
  expiry** — not "confirmation date + 7". If the audio object has already expired (today's
  `WELCOME_AUDIO_POINTER_STALE` path) or no audio pointer exists, the welcome email shall show the
  no-audio line (FR-10) and still send. The cold-start case (no brief archived at all) shall show
  no audio line at all, as today.
- **FR-15.** The welcome email's chrome shall stay **visually identical** to a daily subscriber
  email, per the existing hand-duplicated-chrome convention documented in both files.

### D. Operations

- **FR-16.** **Owner confirmation summary — finding: it does not mention audio today.**
  `delivery_core._build_confirmation_email()` reports only the date and the subscriber send/fail
  counts. **Default: leave it unchanged** — the owner's own copy of the brief already carries the
  FR-10 no-audio line, so the signal is not lost, and `audio_ok` is already in the delivery summary
  and CloudWatch logs. (An optional one-line addition is offered as an open question in §7.)
- **FR-17.** Each send shall emit a structured log line recording whether an audio link was
  embedded and, when it was, the expiry date used — so an audio-less day is diagnosable from logs
  without opening a mailbox. No secrets, no per-recipient data beyond what is logged today.
- **FR-18.** The audio base URL shall be a single named configuration value per deploy unit
  (matching the existing `SUBSCRIBE_SITE_URL` / `FEEDBACK_BASE_URL` conventions), not a literal
  repeated at each use site, and an unset/blank value shall degrade via FR-10, never raise.

## 5. Acceptance criteria

### Serving
- **AC-1.** *Given* the `/audio/*` behavior is deployed, *when* an unauthenticated client requests
  `https://briefing.mschweier.com/audio/<a real, existing Polly key>`, *then* the response is `200`
  with `Content-Type: audio/mpeg` and `Accept-Ranges: bytes`, and the body is a playable MP3 whose
  byte length matches the S3 object. *(FR-1, FR-4)*
- **AC-2.** *Given* the same URL, *when* the client sends `Range: bytes=0-1023`, *then* the response
  is `206 Partial Content` with exactly 1024 bytes and a `Content-Range` header. *(FR-4)*
- **AC-3.** *Given* the deployed distribution, *when* an unauthenticated client requests
  `https://briefing.mschweier.com/briefs/<any known archived date>/brief.html` (and any other
  non-`audio/` key on that bucket), *then* the request does **not** return that object's content.
  *(FR-2)*
- **AC-4.** *Given* the pipeline bucket, *when* its public-access-block configuration is inspected
  after deployment, *then* all four block settings are still enabled and no object has a public
  ACL. *(FR-2)*
- **AC-5.** *Given* an `audio/` key that has been expired by the lifecycle rule (or a fabricated
  non-existent key), *when* it is requested through the distribution, *then* the HTTP **status is
  404**. *(FR-5)*
- **AC-6.** *Given* a real, live audio object, *when* its `OutputUri`-derived key is inspected,
  *then* the key contains a high-entropy Polly-generated identifier (recorded in the validation
  evidence), and the email-side URL is built from that key verbatim — a test asserts that the URL
  is never assembled from the brief date or any other guessable input. *(FR-3, FR-9)*

### Daily email
- **AC-7.** *Given* a successful run, *when* the owner's copy and a subscriber copy are inspected as
  raw MIME, *then* neither contains an `audio/mpeg` (or any) attachment part, and both are under
  ~100 KB. *(FR-6)*
- **AC-8.** *Given* a successful run, *when* the rendered HTML is inspected, *then* the **first**
  line of the top meta box is the audio line, containing an `<a href>` whose URL equals the audio
  base URL plus that run's actual audio key. *(FR-7, FR-9)*
- **AC-9.** *Given* an audio object whose S3-reported lifecycle expiry is `2026-08-04T00:00:00Z`,
  *when* the email is built, *then* the displayed expiry reads `4 August 2026` (Europe/Berlin,
  spelled-out month, no time). A unit test covers at least one date where the UTC→Europe/Berlin
  conversion changes the calendar day. *(FR-8)*
- **AC-10.** *Given* the same edition, *when* the owner copy and every subscriber copy are compared,
  *then* the audio URL and the expiry date are **identical** across all of them (no per-recipient
  variation). *(non-goal: per-recipient links)*
- **AC-11.** *Given* Polly synthesis fails (`audio_ok=False`), *when* the run completes, *then* the
  delivery still reports `succeeded`, every recipient still receives the written brief, and the
  header box shows the no-audio line with no `<a>` and no date. *(FR-10)*
- **AC-12.** *Given* synthesis succeeds but the expiry metadata is unreadable, *when* the email is
  built, *then* the audio link is present and playable and the line carries a generic availability
  note in place of a date. *(FR-10)*
- **AC-13.** *Given* an MP3 larger than `MAX_AUDIO_ATTACHMENT_BYTES`, *when* the email is built,
  *then* the audio link is present and correct (the old silent-drop path is gone). *(FR-11)*
- **AC-14.** *Given* the deployed delivery boundary, *when* the `POST /deliver` request schema and
  the microVM client are inspected, *then* they are byte-identical to pre-change `main` — this
  feature required no content-pipeline change. *(FR-12)*

### Welcome email
- **AC-15.** *Given* a new confirmation and an archived latest edition **with** live audio, *when*
  the welcome email is inspected, *then* it has no attachment, carries the audio link as the first
  meta-box line, and the displayed expiry equals that object's real S3-reported expiry — **not**
  confirmation-date + 7. A test asserts this with an audio object created 5 days ago. *(FR-13,
  FR-14)*
- **AC-16.** *Given* a latest edition whose audio has already expired (pointer resolves to a missing
  object), *when* a subscriber confirms, *then* the welcome email is still sent, with the no-audio
  line and no broken link. *(FR-14)*
- **AC-17.** *Given* the cold-start case (no brief ever archived), *when* a subscriber confirms,
  *then* the welcome email is unchanged from today — welcome-only, no audio line. *(FR-14)*
- **AC-18.** *Given* a daily subscriber email and a welcome email for the same edition, *when* both
  meta boxes are compared, *then* they are visually identical apart from the welcome intro line and
  the deliberately-absent forward/subscribe line. *(FR-15)*

### Operations
- **AC-19.** *Given* a completed run, *when* the CloudWatch logs are read, *then* a single
  structured line states whether an audio link was embedded and, if so, the expiry date used.
  *(FR-17)*
- **AC-20.** *Given* the audio base URL configuration is blank, *when* a delivery runs, *then* the
  send completes normally with the no-audio line, and no exception is raised. *(FR-18, FR-10)*
- **AC-21.** *Given* this change ships, *when* the owner confirmation summary email is compared to
  the pre-change version, *then* it is unchanged (unless §7's optional line is approved at the
  gate). *(FR-16)*

## 6. Constraints & dependencies

- **AWS account `740353583786`, us-east-1.** Confirm the active account before any deploy
  (`/aws-account`).
- **SES From must remain exactly `aibriefing@mschweier.com`**; owner recipient stays
  `mail@mschweier.com`. Unchanged by this feature.
- **SES is still in the sandbox.** Only verified recipients can be mailed, so subscriber-visible
  validation is limited to verified test addresses plus the owner — this constrains §8's rollout,
  not the design.
- **The pipeline bucket is not CDK-managed.** `cowork-polly-tts-740353583786` predates these stacks
  and its lifecycle rules were created imperatively (the same precedent is documented for the
  `briefs/` 90-day rule in `deploy/managed-agent/README.md`). The OAC bucket-policy statement is
  therefore expected to be an **out-of-band runbook step**, documented in the affected README(s), not
  a CloudFormation resource. Architect to confirm.
- **The 7-day `audio/` lifecycle rule is fixed** (owner decision). S3 lifecycle expiry lands at
  UTC midnight and deletion may lag by hours — availability can therefore only ever be *longer*
  than the stated date, never shorter. The copy must not over-promise.
- **Cross-deploy-unit coupling.** The audio URL is produced by **two independently deployed units**
  (`deploy/delivery/` and `deploy/subscribers/`) that cannot import each other; the existing
  hand-duplicated-constant convention (documented in `latest_brief.py` and the welcome handler)
  applies. The CloudFront distribution itself lives in `deploy/subscribers/`, while the primary
  consumer is `deploy/delivery/` — a deliberate, documented cross-unit dependency.
- **Existing distribution behaviour.** The `briefing.mschweier.com` distribution currently maps
  **both** 403 and 404 to `/index.html` with a 404 status, distribution-wide. An expired audio URL
  will therefore return status 404 with the subscribe page as its body (see §7).
- **Fail-safe philosophy is binding** (`CLAUDE.md`): "never lose the brief over an audio/email
  glitch." Every new step in this feature must be best-effort.
- **Cost:** validated with the owner — CloudFront's always-free tier covers this scale; removing
  attachments removes SES's $0.12/GB attachment-data charge. No new spend expected.
- **No new secrets.** Nothing in this feature is signed or authenticated, so nothing is committed
  or rotated.
- **Depends on:** `deploy/delivery/` (live production delivery path, ADR-0015),
  `deploy/subscribers/` (the distribution and the welcome Lambda), and the existing
  `briefs/<date>/audio-pointer.json` archival.

## 7. Risks & open questions

**Assumptions made by the PM (all flagged for confirmation at the gate):**

- **[PM assumption — confirm at gate] Audio line placement inside the meta box.** The box's line
  order was explicitly owner-specified on 2026-07-08 (forward/subscribe → unsubscribe → feedback →
  disclaimer). This PRD puts the audio line **first, above all of them**, as the most direct reading
  of "header area, convenient to reach." Confirm, or state a different position.
- **[PM assumption — confirm at gate] Copy wording.** Proposed link line:
  `🎧 Listen to this brief (link expires 4 August 2026)`. Proposed no-audio line:
  `🎧 No audio version available for this edition.` Proposed unreadable-expiry variant:
  `🎧 Listen to this brief (link available for about 7 days)`. Wording is easy to change later;
  the *rules* (FR-7/FR-8/FR-10) are the contract.
- **[PM assumption — confirm at gate] Date format.** `4 August 2026` — English spelled-out month
  (the whole email is English), Europe/Berlin, no time-of-day. Chosen over `04.08.2026` and
  `2026-08-04` because it is unambiguous for a German/European reader **and** matches the email's
  language. The word "expires" (not "available until") is used deliberately, so the exact
  S3-reported date is never an over-promise.
- **[PM assumption — confirm at gate] Expired-link response body.** CloudFront custom error
  responses are distribution-wide, so an expired audio URL will return **status 404 with the
  subscribe page as the body** unless a per-path exception is added. The owner accepted "a plain
  404"; this is a 404 that happens to render the subscribe page — arguably better, but it is not
  literally what was described. AC-5 asserts the **status**, not the body.
- **[PM assumption — confirm at gate] Confirmation summary left unchanged** (FR-16). The optional
  alternative: add one conditional line, `No audio link in this edition (synthesis failed).`, only
  when audio is unavailable. Low cost, mildly useful, strictly additive. **[DECISION NEEDED]** —
  default is "no change."
- **[PM assumption — confirm at gate] Expiry-metadata source.** The expected mechanism is S3's own
  lifecycle-expiry metadata returned on a head/get of the object; if that turns out to be
  unavailable in practice, FR-10's generic-note fallback carries the feature, but the owner's
  "exact expiry date" requirement would be unmet and should come back to the gate rather than being
  quietly replaced with a computed date.
- **[PM assumption — confirm at gate] Key unguessability needs no key change.** The repo's own test
  fixtures show Polly's async output key shaped as `audio/.<task-id>.mp3`, and Polly task ids are
  UUID-shaped — i.e. already unguessable, satisfying the owner's decision 2 with **zero** key
  changes. FR-3/AC-6 require this be **verified against a live object** during phase 1 rather than
  assumed; if entropy turns out to be low, this returns to the gate (it would change the
  architecture, not just an implementation detail).

**Open questions for the architect:**

- **Downloaded filename UX.** The Polly key begins with a dot, so a reader who chooses "Save" gets a
  hidden dotfile (`.7f3a….mp3`) rather than something like `daily-ai-brief-2026-07-27.mp3`. Options:
  accept it; set a friendlier name via a response-content-disposition mechanism at the edge; or
  ignore (most readers stream rather than save). Recommend: accept for v1, note it.
- **Edge-cache TTL vs the 7-day expiry.** A cached edge copy can outlive the deleted S3 object by up
  to the cache TTL (the current default behavior uses `CACHING_OPTIMIZED`). This only ever *extends*
  availability, so it is benign, but the `/audio/*` behavior's cache policy should be a deliberate
  choice (recommend a TTL comfortably under the lifecycle window, and no cookie/header forwarding).
- **Cross-unit configuration of the audio base URL** — both units already hardcode
  `https://briefing.mschweier.com` as a constant; is a constant sufficient, or should this be CDK
  context/env in both stacks?
- **Dead code cleanup.** With no attachment, the deliver Lambda no longer needs to download the MP3
  to `/tmp` (only to read its metadata), and its `s3:GetObject` on `audio/*` could be narrowed.
  Same for the welcome Lambda's `_fetch_audio_bytes`. In scope as tidy-up, or a follow-up?
- **Is an ADR warranted?** PM's view: yes, a short one — this creates the project's first
  **publicly readable** path into the pipeline bucket and adds a second origin to a distribution
  owned by a different deploy unit.

**Risks:**

- **R1 — Prefix-scoping mistake exposes `briefs/`.** The single highest-consequence failure mode: a
  bucket-policy statement or behavior path pattern that is broader than `audio/*` would make the
  archive publicly readable. Mitigated by FR-2 + AC-3/AC-4 and a mandatory security-engineer pass on
  the bucket policy and the behavior's path pattern before deploy. *(Severity: high; likelihood: low
  with the AC in place.)*
- **R2 — Perceived feature loss.** Some readers may prefer an attachment (offline listening on a
  commute with no signal). A link requires connectivity at play time. Accepted: the inbox-cost
  problem is the one the owner is solving, and readers can still save the file.
- **R3 — Email-client link handling.** Some clients rewrite or proxy links, and some mobile clients
  hand `.mp3` URLs to a download manager rather than an inline player. Behaviour is therefore
  client-dependent; the AC set validates the *server* contract (FR-4), not every client. Validate on
  the owner's own primary clients during phase 2.
- **R4 — Two code paths drift.** The daily chrome and the welcome chrome are hand-duplicated across
  deploy units with no automated cross-check (a pre-existing, documented limitation). Adding a line
  to both is a fresh chance to drift. Mitigated by AC-18.
- **R5 — Silent audio-less days become less visible.** Today the owner notices a missing attachment;
  after this change the difference between "link" and "no audio line" is subtler. Mitigated by FR-17
  (structured log) and, if approved, §7's optional confirmation line.

## 8. Rollout & metrics

**Staged, in this order — the infrastructure lands additively before anything a reader sees
changes.**

1. **Phase 1 — serve the audio (additive, invisible).** Deploy the `/audio/*` behavior + OAC +
   the `audio/*`-scoped bucket policy. **No email change ships in this phase.** Validate:
   - `curl -I` a real, current audio key → `200`, `Content-Type: audio/mpeg`, `Accept-Ranges: bytes`
     (AC-1);
   - `curl -H 'Range: bytes=0-1023'` → `206` with 1024 bytes (AC-2);
   - a `briefs/` key and the bucket's public-access block → still not reachable / still blocked
     (AC-3, AC-4);
   - a non-existent key → `404` (AC-5);
   - record the real key shape as the FR-3/AC-6 entropy evidence.
   Nothing a subscriber sees has changed at this point, and there is nothing to roll back beyond
   removing the behavior.
2. **Phase 2 — flip the email template, owner-only.** Ship the delivery-side change and run an
   **owner-only validation send** (`enable_subscriber_fanout=false`, the same discipline used for
   the ADR-0015 cut-over). Confirm on the owner's real mail clients: no attachment, link in the
   header, correct expiry date, link plays. Also exercise the FR-10 no-audio path deliberately.
3. **Phase 3 — subscriber-visible.** Let the next scheduled weekday run fan out. Verify against a
   verified sandbox test address as well as the owner's copy.
4. **Phase 4 — welcome email.** Ship the welcome-send change and validate with a real confirm →
   welcome cycle on a verified test address, specifically against an edition whose audio is **not**
   brand-new (so FR-14's "real remaining expiry" is genuinely exercised), plus the expired-audio and
   cold-start paths.

**Rollback.** Revert the email-template change to restore the attachment code path — the delivery
boundary is independently deployable, the change is confined to email composition, and no contract,
schema, or content-pipeline change is involved. The `/audio/*` behavior can stay in place while
rolled back (it is harmless on its own), which keeps rollback to a single small revert per deploy
unit.

**Success metrics.**
- **Primary:** zero MP3 attachments on all sent editions after phase 3, with the written brief
  still delivered on 100% of runs (no regression in the fail-safe) — measured on the delivery
  summary and SES send counts.
- **Per-email size** drops from multiple MB to well under 100 KB (AC-7).
- **Cost:** SES attachment-data charge for the brief goes to zero; CloudFront stays inside the
  always-free tier (check the first full month's bill).
- **Correctness:** every sampled edition's link resolves to that edition's audio and the displayed
  expiry matches the object's real S3 expiry (spot-checked weekly for the first two weeks).
- **Reader signal (qualitative):** no feedback-form submissions reporting a broken or confusing
  audio link in the first two weeks.
