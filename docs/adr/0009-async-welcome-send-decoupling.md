# 0009. Decouple the welcome send from the confirm request path via async Lambda invoke

- Status: Accepted
- Date: 2026-07-03
- Deciders: architect (Claude)

## Context

PRD `instant-welcome-brief.md` (§7, "[DECISION NEEDED — Architect]") requires that when a
subscriber's status transitions `pending` → `confirmed`, they immediately receive a welcome
email whose body is the most recent archived `brief.html` with the narrated MP3 attached when
available. That send is materially heavier than everything the confirm flow does today:

- **Today's confirm Lambda is fast and lean** (`deploy/subscribers/functions/confirm/handler.py`):
  a DynamoDB `GetItem` + `UpdateItem` and an HTML page response, on a **10 s** Lambda timeout
  (`brief_subscribers/stack.py` `_base_function_kwargs`), behind an API Gateway **HTTP API** whose
  Lambda-proxy integration caps at the HTTP API's 30 s maximum. It has **no SES or S3
  permissions** at all.
- **The welcome send is a multi-second, multi-MB unit of work**: an S3 `ListBucket`/`GetObject`
  to resolve the latest brief, a `GetObject` of a potentially multi-MB MP3, MIME assembly, and an
  SES `SendRawEmail`. Its latency and failure surface (S3 and SES health, throttling) are entirely
  different from a DynamoDB row update.
- **FR-9/AC-8 already require failure isolation**: the confirmation state transition and the
  confirm page must succeed regardless of the welcome send's outcome. That constrains *correctness*
  but, if the send ran inline, would not address *latency* — the user would still watch a blank
  page while an MP3 is pulled from S3 and handed to SES.
- **This is a low-volume flow** (human confirmations, not high throughput), so the design should
  favor the smallest amount of new infrastructure that keeps the page instant and makes the send
  independently retriable, per the project's "prefer boring technology, keep interfaces small"
  principle and least-privilege posture.

The open question was whether to run the welcome send **synchronously inside the confirm
request/response path** or to **decouple it asynchronously**, and if async, by which AWS
mechanism (DynamoDB Streams, EventBridge, an async Lambda invoke, or SQS).

## Decision

**We will decouple the welcome send asynchronously. The confirm Lambda, only on the actual
`pending` → `confirmed` transition branch, will asynchronously invoke
(`InvocationType='Event'`) a dedicated, separate welcome-send Lambda, passing a small metadata
payload (subscriber email, first name, unsubscribe token). That welcome Lambda performs the S3
read and the SES `SendRawEmail`.**

Consequences of this shape:

1. **The confirm page stays instant.** The async invoke returns as soon as the event is queued;
   the confirm Lambda's response no longer depends on S3/SES latency or the MP3 size. The confirm
   Lambda keeps its 10 s timeout; the welcome Lambda gets a longer timeout sized for the S3
   download + SES send.
2. **The send is independently retriable.** Asynchronous Lambda invocation gives automatic retries
   (2 by default) and supports an on-failure destination (SQS/SNS DLQ) for observability of sends
   that ultimately fail — retriability the confirm request path cannot offer. FR-9's
   failure-isolation is satisfied structurally: a failed send never touches the already-committed
   `confirmed` state, and the confirm response was already returned.
3. **Least-privilege improves versus the PRD's literal FR-13/FR-14.** Those requirements were
   written assuming the *confirm* Lambda sends; with this decoupling the SES-send
   (`ses:SendRawEmail` under the `ses:FromAddress == aibriefing@mschweier.com` condition) and the
   scoped S3 read (`s3:ListBucket` on `briefs/*`, `s3:GetObject` on `briefs/*` and `audio/*` of
   `cowork-polly-tts-740353583786`) move to the **welcome-send Lambda's** role. The confirm
   Lambda gains only `lambda:InvokeFunction` on that one target and never holds SES or S3 rights.
   FR-13/FR-14 apply unchanged **to whichever Lambda performs the send** — here, the welcome
   Lambda. The security-engineer should review the grants on that role.
4. **"Sent once" (FR-7/AC-6) is preserved in code.** The confirm Lambda invokes the welcome
   Lambda *only* inside the successful-transition branch (after the guarded `UpdateItem`); the
   idempotent "already confirmed" re-click branch returns early and never invokes. The payload is
   well under the 256 KB async-invoke limit (it carries metadata only — never the MP3).

## Alternatives considered

- **Synchronous, inline in the confirm Lambda.** Add S3+SES perms to the confirm Lambda, bump its
  timeout, and send before responding. Simplest (no new component), and technically feasible within
  the 30 s HTTP API integration cap. **Rejected**: it couples the confirm page's responsiveness to
  S3/SES health and MP3 size (a poor first-run experience — the whole point of this feature is
  immediacy), offers no retry on a transient SES/S3 failure, and forces the confirm Lambda to hold
  SES + S3 rights it otherwise never needs. FR-9 would make it *correct* but not *snappy*.
- **DynamoDB Streams on the table, triggering the welcome Lambda on the transition.** Elegant in
  that it keys off the real state change (`OldImage.status == pending && NewImage.status ==
  confirmed`). **Rejected for this volume**: it requires enabling streams on `brief-subscribers`
  (a table-level config change), the consumer sees *every* table write and must filter, it must
  reconstruct the send context from the image rather than receiving a clean payload, and stream
  error handling has coarser, shard-blocking retry semantics. The idempotency it confers is equally
  achievable by invoking only in the confirm transition branch. More moving parts, no benefit here.
- **EventBridge (confirm puts an event; a rule targets the welcome Lambda).** Also decoupled and
  retriable. **Rejected**: adds a bus/rule and an `events:PutEvents` grant with no advantage over a
  direct async invoke at this volume — the extra indirection buys nothing we need (no fan-out to
  multiple consumers, no cross-account routing).
- **SQS queue between confirm and a consumer Lambda.** Most durable, with a natural DLQ.
  **Rejected**: the heaviest option (queue + event-source mapping + consumer) for a low-volume,
  human-paced flow. Async-invoke's built-in retries plus an on-failure destination give
  equivalent durability guarantees at a fraction of the infrastructure.

## Consequences

Positive:
- The confirm page load stays independent of S3/SES latency and MP3 size — the feature's core
  value (immediacy) is delivered without degrading the existing confirm UX.
- The send is retriable and its terminal failures are observable (on-failure destination), which
  inline sending cannot provide.
- Cleaner least-privilege: SES-send and S3-read rights live on a single-purpose welcome Lambda,
  not on the public-facing confirm Lambda.
- FR-7/FR-9 (sent-once, never-blocks-confirmation) fall out of the design rather than needing
  defensive plumbing in the request path.

Negative / follow-ups:
- **One new Lambda + its packaging.** The welcome Lambda needs the MIME/SES send logic and the
  read-only latest-brief helper (PRD FR-2); the Architect's separate open item on where that helper
  is packaged (§7) applies to this new function. Prefer a small, read-only, HTML-oriented helper
  over reusing the Markdown-oriented `read_recent_prior_briefs`.
- **FR-13/FR-14 are satisfied on the welcome Lambda's role, not the confirm Lambda's** — a
  deliberate, documented deviation from those requirements' literal wording; the security review
  must inspect the welcome Lambda's grants (SES FromAddress condition + the two S3 prefixes).
- **Async invoke is best-effort with bounded retries.** A subscriber whose send exhausts retries
  gets no welcome email (they remain correctly `confirmed`); the on-failure destination makes this
  visible. This is acceptable and consistent with the "never lose the confirmation over a
  send glitch" fail-safe — it is not a silent data-loss path for subscription state.
- **Reversible.** Should volume or requirements change, switching mechanisms (e.g. to SQS for
  stricter delivery guarantees) is a contained change to the confirm→welcome edge; nothing here is
  a one-way door.

## Verification note

The decision rests on well-established Lambda/API Gateway behavior (HTTP API integration 30 s cap;
the existing confirm Lambda's 10 s timeout; asynchronous invocation's automatic retries, 256 KB
payload limit, and on-failure destinations) rather than an account-specific limit, so no `aws-docs`
MCP lookup gated it. The Developer should, at implementation time, confirm the current SES raw
message-size limit against the largest expected MP3 (the graceful no-audio path, FR-5/AC-5, already
covers an over-limit or missing object) and size the welcome Lambda's timeout/memory for the S3
download plus SES send.
