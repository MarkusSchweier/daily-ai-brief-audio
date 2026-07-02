# 0003. Subscriber data model, tokens, and expiry

- Status: Accepted
- Date: 2026-07-02
- Deciders: architect (Claude)

## Context

The PRD requires: double opt-in with confirm links that expire in ~48h (FR-8, FR-10, AC-11);
non-guessable confirm/unsubscribe tokens with no login (FR-11); one-click unsubscribe from both
the site and every email footer (FR-16); idempotent unsubscribe (AC-12); re-subscribe after
unsubscribe (AC-15); benign responses that do not leak subscriber existence/status via
differential behavior (AC-9, AC-11, token security in §7); and a fan-out that reads **confirmed**
subscribers efficiently — by `Query`, never `Scan` (per ADR-0002).

This ADR fixes the DynamoDB schema, the token scheme, and the expiry mechanism.

## Decision

### Table `brief-subscribers` (DynamoDB, on-demand)

- **PK**: `email` (string, **normalized to lowercase, trimmed**). One row per address; keying by
  email makes subscribe/re-subscribe naturally idempotent and prevents duplicate active rows
  (AC-9, AC-10, AC-15 all resolve to `PutItem`/`UpdateItem` on the same key).
- **Attributes**:
  - `firstName`, `lastName` (strings, length-bounded on input).
  - `status` (string enum: `pending` | `confirmed` | `unsubscribed`).
  - `confirmToken` (string) and `confirmTokenExpiresAt` (number, epoch seconds) — present while
    `pending`; used as the DynamoDB **TTL attribute** so never-confirmed rows auto-purge ~48h
    after creation.
  - `unsubscribeToken` (string) — generated **at confirm time**, never expires (must work from
    an email footer indefinitely).
  - `createdAt`, `confirmedAt`, `unsubscribedAt` (epoch seconds, set as applicable).
  - `sourceIp` (string) — operational/abuse signal only, from the API Gateway request context.
- **GSI `status-index`**: partition key `status`. The fan-out issues
  `Query(IndexName="status-index", KeyConditionExpression="status = :confirmed")` to page
  confirmed rows — no `Scan`, and the fan-out's IAM is scoped to this index ARN only (ADR-0002).
  Project only the attributes the fan-out needs (`email`, `firstName`, `unsubscribeToken`) via a
  `KEYS_ONLY`+include or `INCLUDE` projection to keep the index small.

### Tokens — random opaque, stored on the row, looked up as a compound key

- Tokens are `secrets.token_urlsafe(32)` (256 bits of entropy, URL-safe). **Not** signed/HMAC —
  there is no verify-without-DB-read use case here, so a stored random token is simpler and has
  no key-management burden.
- **Verification is a single compound lookup, not "read then compare":** confirm/unsubscribe
  handlers `GetItem` by `email` (passed in the link) and then require the presented token to
  equal the stored token **and** (for confirm) `now < confirmTokenExpiresAt` **and** the status
  to be in the expected state. A mismatch, missing row, or expiry all return the **same neutral
  page** ("this link is invalid or has expired — please sign up again"), so responses do not
  differentiate "no such subscriber" from "wrong token" from "expired" (AC-9, AC-11, §7).
- Token comparison uses a constant-time compare (`hmac.compare_digest`) to avoid timing leaks.
- Links carry both `email` and `token` as query params on `GET /confirm` and `GET /unsubscribe`
  (GET so an email-client click works; both handlers are idempotent and side-effect-safe to
  re-invoke — see idempotency below).

### State transitions

- **subscribe** (`POST /subscribe`): validate email format + field lengths; if honeypot filled,
  return the normal success-looking response and do nothing (FR-5/AC-14). Otherwise upsert:
  - new or previously `unsubscribed`/`pending` (expired or not) → set `status=pending`, fresh
    `confirmToken` + `confirmTokenExpiresAt = now + 48h`, send confirmation email from
    `aibriefing@`. Re-subscribe (AC-15) and unconfirmed re-submit (AC-10) both land here.
  - already `confirmed` → do **not** reset anything; return the same benign "check your
    inbox / you're all set" response without revealing confirmed status (AC-9). No duplicate row
    (same PK).
- **confirm** (`GET /confirm?email=&token=`): if row exists, token matches, not expired, and
  status is `pending` → set `status=confirmed`, `confirmedAt=now`, generate `unsubscribeToken`,
  and (optionally) clear `confirmToken`/`confirmTokenExpiresAt` so TTL no longer applies. Show
  the confirmed landing page. Any failure → neutral invalid/expired page (AC-11). If already
  `confirmed`, showing the confirmed page again is fine (idempotent).
- **unsubscribe** (`GET /unsubscribe?email=&token=`): if row exists and `unsubscribeToken`
  matches → set `status=unsubscribed`, `unsubscribedAt=now`. Show unsubscribe-confirmation page.
  Re-clicking when already `unsubscribed` shows the same confirmation page (idempotent, no error,
  no re-subscribe — AC-12).

### Expiry

DynamoDB **TTL on `confirmTokenExpiresAt`** auto-deletes never-confirmed `pending` rows ~48h
after creation (FR-10). Because DynamoDB TTL deletion is best-effort (can lag hours), the
confirm handler **also** enforces `now < confirmTokenExpiresAt` at click time (AC-11) — the TTL
is for cleanup, the runtime check is for correctness. Confirmed rows clear the TTL attribute so
they are never purged.

## Alternatives considered

- **Signed/HMAC tokens (stateless verify).** Rejected: no requirement to verify without a DB
  read (both handlers write to the row anyway), and it adds a signing-key to manage/rotate. A
  stored random token is simpler and equally non-guessable at 256 bits.
- **Separate token as PK / a token-index GSI** so the link carries only the token. Rejected:
  carrying `email`+`token` and looking up by `email` (the base PK) avoids an extra index and
  still resists enumeration (neutral responses). It also makes idempotency and re-subscribe
  trivially key-based.
- **Table-wide `Scan` for the fan-out.** Rejected: violates least privilege and scales badly; a
  `status` GSI Query is both cheaper and lets ADR-0002 scope the fan-out to the index ARN only.
- **UUIDv4 tokens.** Adequate entropy but `secrets.token_urlsafe(32)` is purpose-built for this,
  URL-safe, and slightly higher entropy; chosen for clarity of intent.
- **Soft-delete vs hard-delete of unsubscribed rows.** Chosen soft-delete (`status=unsubscribed`
  retained) so re-subscribe and idempotent unsubscribe work and there's an audit trail; TTL only
  purges never-confirmed rows, not unsubscribed ones.

## Consequences

Positive:
- One small table, keyed by email, covers every state transition idempotently; all PRD edge
  cases (AC-9…AC-15) reduce to conditional writes on a single key.
- Neutral responses + constant-time token compare address the enumeration/leak concern (§7).
- TTL keeps the table self-cleaning for abandoned signups at zero operational cost.
- Fan-out reads are a bounded `Query` on the status GSI — least-privilege and cheap.

Negative / follow-ups:
- Unsubscribed rows persist indefinitely (by design). At real scale a later epic might age them
  out; trivial at sandbox scale.
- The confirm link exposes the subscriber's own email in the URL query string; this is standard
  for double-opt-in links and only reveals the address to the person who already owns the inbox.
  Avoid logging full confirm/unsubscribe URLs in plaintext (log the email + outcome, not the
  token) — note for the developer.
- Email normalization must be consistent everywhere (subscribe, confirm, unsubscribe, and the
  fan-out) or lookups miss; centralize the normalize function. Note for the developer.
- Storing `sourceIp` is minimal operational data (PRD allows this); it is not used for tracking.
