# 0011. Signed, self-attesting feedback-link token scheme

- Status: Accepted
- Date: 2026-07-03
- Deciders: architect (Claude)

## Context

PRD `docs/prd/reader-feedback.md` requires (FR-12, FR-13, and the "Signed, self-attesting
token" bullet in §6) a token embedded in the per-recipient feedback link that:

- encodes **(recipient identity, brief date)** so a stored submission can be attributed —
  internally, in the data only — to a specific reader and the specific edition they reacted to;
- is **tamper-resistant**: a reader must not be able to forge, alter, or swap the identity or
  the edition (e.g. submit feedback attributed to a different person or a different edition);
- works for **both** subscribers (who have a `brief-subscribers` DynamoDB row) **and** the owner
  (`mail@mschweier.com`, who has **no** subscribers-table row);
- validates **without a database lookup** — the feedback stack is a separate CDK app/deploy unit
  that does **not** read the subscribers table (and cannot, for the owner, since there is no row);
- degrades a tampered/malformed/invalid token to the **walk-up anonymous** case (FR-10/FR-12) —
  never forging or accepting a spoofed identity.

This is explicitly **not** the existing subscribe/confirm/unsubscribe token model (ADR-0003),
which is an opaque random value **stored on the row and looked up**. That model cannot serve the
owner (no row) and does not bind an edition. This feature needs a **cryptographically signed**,
self-attesting token.

The token must be generated from **three** places and validated in a fourth:

- Generated in `deploy/managed-agent/pipeline/audio_email.py` (the daily fan-out, running inside
  the microVM, which has each recipient email and the run's local date `_today_local_date()` in
  hand — for both the owner copy and every subscriber);
- Generated in `deploy/subscribers/functions/welcome-send/handler.py` (the instant-welcome-brief
  send, which has the recipient email and the archived edition's date);
- Validated in the new `deploy/feedback/` stack's submit Lambda.

(The token **generator** lives in the two send paths; the **packaging** of the shared helper and
the **shape of the feedback stack** are covered in ADR-0012. This ADR fixes the **token scheme,
the signing algorithm, the expiry policy, and where the signing secret lives**.)

## Decision

### Token format

A feedback token is a URL-safe string of exactly **two dot-separated base64url segments**:

```
<payload_b64url>.<sig_b64url>
```

- **`payload_b64url`** — the base64url encoding (RFC 4648 §5, **no padding**) of a UTF-8 JSON
  object with a small, fixed, versioned schema:

  ```json
  {"v": 1, "id": "<recipient-email-lowercased>", "d": "<YYYY-MM-DD>"}
  ```

  - `v` (int) — scheme version, `1`. Present so the format can evolve (e.g. add a field or
    change the MAC construction) without ambiguity; the validator rejects unknown versions.
  - `id` (string) — the **recipient identity = email string**, lowercased and trimmed. For a
    subscriber this is their `email` (already the PK of `brief-subscribers`, ADR-0003, already
    normalized lowercase). For the owner it is the literal `mail@mschweier.com` — the `RECIP`
    constant already used in `audio_email.py`. A **single uniform "identity = email string"**
    representation covers both, so AC-7 (owner link) and AC-8 (attributed submit) share one code
    path with no owner special-case in the token itself.
  - `d` (string) — the brief/edition date, `YYYY-MM-DD` (the fan-out's `_today_local_date()` /
    the welcome path's archived-edition date). Not personally identifying on its own.

  **No expiry field** — see "Expiry" below.

- **`sig_b64url`** — the base64url (no padding) encoding of
  **`HMAC-SHA256(key = shared_secret, msg = payload_b64url_ascii_bytes)`**. The MAC is computed
  over the **exact base64url payload string bytes** that appear in the token (sign-what-you-send),
  not over a re-serialized JSON object — this removes any JSON canonicalization ambiguity between
  the three generators and the validator.

The whole token is a URL-query-safe string (base64url alphabet `A–Z a–z 0–9 - _`, plus the `.`
separator — all safe in a query-param value). It is carried as a single query parameter, e.g.
`https://feedback.mschweier.com/?t=<token>`.

### Signing algorithm and verification

- **HMAC-SHA256** via Python stdlib `hmac` + `hashlib` — no new dependency, in all three runtimes
  (the microVM pipeline, the welcome-send Lambda, and the feedback submit Lambda all already run
  Python with stdlib available).
- **Generation:** build the JSON payload → base64url-encode (no padding) → `hmac.new(secret,
  payload_b64url.encode("ascii"), hashlib.sha256).digest()` → base64url-encode (no padding) →
  join with `.`.
- **Verification (validator, in the feedback submit Lambda):**
  1. Split on the **last** `.` into `payload_b64url` and `sig_b64url`; a token that does not split
     into exactly two non-empty segments is **invalid**.
  2. Recompute the expected signature over `payload_b64url` with the shared secret and compare
     using **`hmac.compare_digest`** (constant-time — mirrors ADR-0003's token-compare choice).
     A mismatch is **invalid**.
  3. base64url-decode and `json.loads` the payload; enforce `v == 1`, `id` and `d` present and
     well-typed, and `d` matches a strict `^\d{4}-\d{2}-\d{2}$` shape. Any failure is **invalid**.
  4. On success, the validator trusts `(id, d)` **without any database lookup**.
- **Invalid ⇒ walk-up anonymous.** An invalid, tampered, malformed, or absent token is handled
  **exactly** as FR-10's no-token case: the submission is recorded anonymously, with **no**
  identity and **no** edition attribution, and is **never** stored with the caller-supplied
  identity/date. The validator returns a clean "no attested identity" result — it does **not**
  raise a 4xx or leak which check failed (AC-11).

### Expiry: none (deliberately)

The token carries **no expiry**, and the validator enforces none. The PRD (§7, "Token signing
scheme") explicitly warns that a subscriber may legitimately open an **old edition's** email and
give feedback **weeks later**, and that an over-tight expiry would silently downgrade genuine late
feedback to anonymous. The purpose of the token is **attribution integrity**, not session
freshness: there is no security benefit to expiring it (the signed `(id, d)` is not a
capability that grants access to anything — it only attests who/which-edition on a public,
otherwise-anonymous form), and a real downside to expiring it (lost attribution on genuine late
feedback). We therefore **omit expiry**.

The versioned `v` field is the escape hatch: if we ever needed to invalidate all outstanding
tokens (e.g. secret compromise), we rotate the shared secret (which invalidates every existing
token's signature) and/or bump the scheme version — both are blunt, deliberate operator actions,
not a per-token clock. This is the right granularity for this data.

### Where the signing secret lives, and how it is provisioned

**One shared AWS Secrets Manager secret**, referenced by ARN, fetched at runtime — the exact
convention this repo already uses for the webhook signing secret
(`deploy/managed-agent/microvm/launcher/launcher.py` `_get_secret()` → `boto3.client(
"secretsmanager").get_secret_value(SecretId=arn)["SecretString"]`, scoped by ARN in the launcher
role's `ReadSigningSecret` statement) and for the Anthropic environment key. **Never committed.**

- **Owning stack.** The secret is created (empty, populated out-of-band) by the **new
  `deploy/feedback/` stack** — it is the token system's natural home, is the only *new* stack, and
  the two send-side consumers already cross grants at deploy time in this repo. Shape, mirroring
  `managed_agent/stack.py`'s `_build_signing_secret()`:
  - `secret_name = "daily-ai-brief/feedback-token-signing-secret"`;
  - created with **no `SecretString`** (CDK/CloudFormation cannot set a real value without it
    landing in a template/state file) — populated after deploy via
    `aws secretsmanager put-secret-value` (documented in the feedback README);
  - `removal_policy = RemovalPolicy.RETAIN` (matches the other secrets in this repo);
  - a **32-byte random value**, generated out-of-band (e.g.
    `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`) — 256 bits, ample for an
    HMAC-SHA256 key.
  - The secret's **ARN is a CDK output** of the feedback stack, so the two other stacks can be
    given it as a context/env value at their own deploy time (they are independent deploy units;
    a hard cross-stack CloudFormation import would couple their deploy lifecycles, which the PRD
    forbids — see ADR-0012's "standalone app" decision). The three roles reference the secret by
    its **ARN string**, not a CDK object import.

- **Least-privilege grant on each of the three roles** — a single statement, identical shape,
  `secretsmanager:GetSecretValue` **scoped to that one secret ARN only**:
  - **Feedback submit Lambda role** (new, in `deploy/feedback/`): granted directly on the secret
    construct it owns (`secret.grant_read(role)` or an explicit ARN-scoped statement).
  - **microVM execution role** (`managed_agent/stack.py`, the role `audio_email.py` runs under):
    a new `add_to_policy` statement `sid="ReadFeedbackTokenSecret"`,
    `actions=["secretsmanager:GetSecretValue"]`, `resources=[<feedback token secret ARN>]`. The
    ARN is supplied to this stack as a context value (e.g. `feedbackTokenSecretArn`) so the
    managed-agent stack does not take a build-time dependency on the feedback stack's synthesis.
  - **welcome-send Lambda role** (`brief_subscribers/stack.py` `WelcomeSendFunctionRole`): the
    same new `sid="ReadFeedbackTokenSecret"` statement, ARN supplied via a
    `feedbackTokenSecretArn` context value on the subscribers app.
  - Each runtime fetches the secret **once per cold start** (module-level cache) via the same
    `_get_secret` shape the launcher uses — never printed, never logged, never committed.

### Identity, attribution, and anonymity interaction

- The token attests only `(email, date)`. When the reader has **not** checked "anonymous" and the
  token is **valid**, the submit Lambda persists the identity email and the brief date (FR-9,
  AC-8). Storing an email address the reader's own link carried, when they explicitly did **not**
  opt into anonymity, leaks nothing beyond what the token already asserted about them — it is the
  expected, acceptable attribution data (US-5).
- When the reader **has** checked "anonymous" (or the token is absent/invalid — walk-up), the
  submit Lambda persists **no** identity, **no** raw token, and nothing identity-derived; it may
  use a **salted keyed hash** of the identity **transiently in-request** for throttling only, per
  FR-11 (that transient throttle logic is an ADR-0012 / implementation concern, not this ADR's) —
  but never writes it. The brief **date** (a public calendar date) may be stored even on an
  anonymous record, since it is not personally identifying and enables edition-level rollups.

## Alternatives considered

- **Reuse the ADR-0003 stored random token (opaque, DB-looked-up).** Rejected by the PRD's own
  constraints: it cannot self-attest the **owner** (no `brief-subscribers` row), does not bind an
  **edition**, and would force the feedback stack — deliberately isolated from the subscribers
  table — to read that table. The signed token exists precisely to avoid the DB lookup.

- **A signed **JWT** (e.g. via PyJWT).** A JWT is HMAC-SHA256 signing plus a standardized header
  and claim set. Rejected: it adds a third-party dependency to **three** independent deploy units
  (a bundling/packaging cost the repo has deliberately avoided — see ADR-0012), and pulls in
  claim semantics (`exp`, `nbf`, `aud`, `iss`) we don't want — the one claim it would naturally
  add, `exp`, is exactly what we are deliberately **omitting**. A ~35-line stdlib HMAC helper is
  the "boring, well-supported, minimal-interface" choice and carries no dependency.

- **Include an expiry.** Rejected — see "Expiry" above. It provides no security benefit here (the
  token is not an access capability) and actively harms a valid use case (late feedback on an old
  edition), against the PRD's explicit warning.

- **Encrypt the payload (AEAD, e.g. Fernet / AES-GCM) so the email is not exposed in the URL.**
  Rejected: the email in the link is only ever visible to the person who already owns that inbox
  (same reasoning as ADR-0003's confirm-link email-in-URL), so confidentiality buys nothing here,
  while symmetric encryption adds a dependency (`cryptography`) and key/nonce management across
  three deploy units. Integrity (HMAC), not confidentiality, is the required property (FR-12).
  We do avoid *gratuitously* exposing the raw email: the payload is base64url of JSON, so the
  email is not eyeball-obvious in the URL, but this is obfuscation, not a security claim.

- **Sign the JSON object rather than the base64url string.** Rejected: signing a re-serialized
  object risks a canonicalization mismatch between the three generators and the validator (key
  order, whitespace, unicode escaping) that would reject valid tokens. Signing the exact
  transmitted base64url bytes (sign-what-you-send) is unambiguous and simpler to verify.

- **Per-stack duplicated secrets (three separate Secrets Manager secrets holding the same
  value).** Rejected: it triplicates a secret value across three places (a rotation and drift
  hazard — rotate one, break the others) for no benefit. One shared secret referenced by ARN in
  three ARN-scoped `GetSecretValue` grants is strictly simpler and is exactly the launcher's
  established single-secret-by-ARN pattern.

## Consequences

Positive:
- One HMAC-SHA256 signed token, stdlib-only, self-attests `(identity, edition)` for both owner and
  subscribers with **no** DB lookup — satisfying FR-12/FR-13 with the minimum machinery.
- Tamper-resistance is a property of the MAC: any change to `id`, `d`, or `v` invalidates the
  signature; an invalid token cleanly degrades to walk-up anonymous, never a forged attribution
  (AC-11).
- No expiry means genuine late feedback on an old edition is still attributed (honors the PRD's
  explicit warning) with no downside, since the token grants no capability.
- One shared secret, ARN-scoped `GetSecretValue` on exactly three roles — least-privilege,
  no static keys, and identical to the launcher's proven secret pattern. Rotation is a single
  `put-secret-value` (existing-tokens invalidation is a feature, not a bug).

Negative / follow-ups:
- **Secret provisioning is a manual out-of-band step** (like the two existing secrets): the
  feedback README must document creating the secret value after first deploy, and the two
  send-side stacks must be (re)deployed with the `feedbackTokenSecretArn` context once the ARN
  exists — a documented cross-stack wiring, not an automatic import (deliberate, to keep the three
  deploy lifecycles independent per the PRD).
- **Three copies of the generator + one validator share one helper** — the drift risk the PRD
  flags (two-file link-embedding drift, plus now the validator). ADR-0012 addresses the helper's
  packaging and the sign/verify test that pins all copies to identical behavior.
- **A secret compromise forges any attribution** until rotated. Acceptable: the blast radius is
  "misattributed feedback on an internal-only form," not access to any resource or PII beyond an
  email in a self-owned inbox link; rotation is one command and invalidates all outstanding tokens.
- **Reversible.** Adding an expiry, bumping `v`, or switching the MAC construction later is a
  contained change gated by the `v` field; nothing here is a one-way door.

## Verification note

This rests on Python stdlib `hmac`/`hashlib`/`base64`/`json` behavior and on the repo's existing,
in-production Secrets Manager pattern (launcher `_get_secret`, the managed-agent stack's
ARN-scoped `GetSecretValue` grants), so no `aws-docs` MCP lookup gated it. At implementation time
the Developer must add a round-trip test (`generate` in one place → `validate` in the feedback
helper) and negative tests for each rejection path (bad signature, altered `id`, altered `d`,
wrong `v`, malformed base64, missing/empty segment, absent token) — each must degrade to the
walk-up-anonymous result, never a forged attribution.
