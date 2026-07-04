# 0012. Standalone `deploy/feedback/` CDK app and token-helper packaging

- Status: Accepted
- Date: 2026-07-03
- Deciders: architect (Claude)

## Context

PRD `docs/prd/reader-feedback.md` requires a **new, self-contained CDK app** under
`deploy/feedback/` (FR-1, §6) — its own CloudFront distribution, own static `site/`, own
Lambda(s), own DynamoDB table, own least-privilege IAM — **structurally modeled on**
`deploy/subscribers/` but **not** bolted onto or sharing resources/roles with it. It also
requires that the signed feedback token (ADR-0011) be **generated** from two independent deploy
units that share no package today — `deploy/managed-agent/pipeline/audio_email.py` (microVM) and
`deploy/subscribers/functions/welcome-send/handler.py` (Lambda) — and **validated** in the new
feedback stack's submit Lambda (a third place). §7 flags "where the token helper lives and how
both deploy units reach it" as an Architect decision.

This ADR fixes **(A)** the packaging of the sign/verify helper across those three places, and
**(B)** the concrete shape of the `deploy/feedback/` stack, at a level a developer can build
against directly. ADR-0011 fixes the token scheme itself and the shared signing secret.

### Existing precedent for cross-deploy-unit code

The repo already accepts **hand-duplicated pure constants** between exactly these two send units:
`MAX_AUDIO_ATTACHMENT_BYTES` is defined identically in both `audio_email.py` and
`welcome-send/handler.py`, with a docstring in each noting "two independent deploy units, kept in
sync by hand." The subscribers app also keeps a real shared **layer**
(`deploy/subscribers/layers/common/python/`, e.g. `subscriber_common.py`, `latest_brief.py`) — but
that layer is reachable **only** by the subscribers-app Lambdas; it is **not** reachable from the
microVM pipeline, which is a wholly separate deploy unit (a microVM image, not a Lambda layer).
There is no existing mechanism that spans microVM + subscribers-Lambda + feedback-Lambda.

## Decision

### A. Token-helper packaging — a small, hand-duplicated, stdlib-only module

**We will ship the token helper as a small (~35–45 line) pure-Python, standard-library-only module,
hand-copied into each of the three locations that need it**, with a synchronization docstring in
each copy pointing at the others and at ADR-0011 — following the repo's existing
`MAX_AUDIO_ATTACHMENT_BYTES` precedent, deliberately, rather than inventing a new shared-packaging
mechanism.

- **Module name:** `feedback_token.py`. It exposes exactly two functions plus the scheme constants:
  - `generate(secret: str, identity: str, brief_date: str) -> str` — used by the two send paths.
  - `validate(secret: str, token: str) -> FeedbackTokenResult` — used by the feedback submit
    Lambda. Returns a small dataclass/named result: `valid: bool`, and on success
    `identity: str`, `brief_date: str`; on any failure `valid=False` with **no** caller data
    (the "walk-up anonymous" result of ADR-0011).
  - `_SCHEME_VERSION = 1`, and the base64url/HMAC helpers, all `hmac`/`hashlib`/`base64`/`json`
    stdlib. **No new dependency**, in any of the three runtimes.
- **The three copies:**
  1. `deploy/managed-agent/pipeline/feedback_token.py` — imported by `audio_email.py`.
  2. `deploy/subscribers/layers/common/python/feedback_token.py` — imported by
     `welcome-send/handler.py` (it goes in the **existing common layer**, alongside
     `subscriber_common.py`, so it needs no bundling and is already on that Lambda's path).
  3. `deploy/feedback/functions/submit/feedback_token.py` — imported by the submit handler (or,
     if the feedback stack grows a common layer, placed there; for one Lambda, co-locating it in
     the function directory is simplest).
- **Kept in sync by hand + a pinning test.** Each copy carries a docstring: "Duplicated in
  {other two paths} — three independent deploy units, kept identical by hand (same convention as
  `MAX_AUDIO_ATTACHMENT_BYTES`). See ADR-0011/0012." The Developer must add a test that a token
  `generate`d by one copy `validate`s under another (the send-side copy → the feedback-side copy),
  proving the copies agree on the wire format; and negative tests per ADR-0011's Verification note.
- **Why duplicated, not a new shared package.** The three consumers live in three deploy units with
  no common build root (a microVM image, a Lambda layer, and a new CDK app). A genuine shared
  package would require either publishing/installing an internal package into all three (new build
  machinery, versioning, and a dependency the repo currently has zero of) or a symlink/`sys.path`
  hack that does not survive the microVM image build or Lambda asset zipping. The logic is
  ~40 lines of dependency-free, rarely-changing crypto glue; the cost of a shared package
  dwarfs the cost of three identical copies plus a cross-copy compatibility test. This mirrors an
  established, working precedent in this exact pair of files. We accept the drift risk explicitly
  and mitigate it with the pinning test and the sync docstrings.

### B. The `deploy/feedback/` standalone CDK app

A **genuinely standalone** CDK app: its own `app.py`, its own `cdk.json`, its own deploy
lifecycle — **not** a nested stack inside `deploy/subscribers` or `deploy/managed-agent`, and
sharing **no** resource or IAM role with them. It is structured to mirror `deploy/subscribers/`.

**Layout:**
```
deploy/feedback/
  app.py                       # cdk.App() -> FeedbackStack, env from CDK_DEFAULT_* (mirror subscribers/app.py)
  cdk.json                     # "app": "python3 app.py"; context keys below
  README.md                    # setup/DNS runbook, mirroring deploy/subscribers/README.md
  requirements.txt             # aws-cdk-lib, constructs (mirror subscribers)
  brief_feedback/
    __init__.py
    stack.py                   # FeedbackStack
  functions/
    submit/
      handler.py               # the submit Lambda
      feedback_token.py        # copy #3 of the helper (ADR-0011/0012 §A)
  site/
    index.html                 # the form (7 graded 1-5 + 2 free-text + anonymity checkbox + honeypot + note)
    styles.css
    app.js                     # vanilla JS: read ?t= token, POST JSON to submit endpoint, in-page thank-you
    config.js                  # window.BRIEF_FEEDBACK_API_BASE_URL = "..."  (mirror subscribers/site/config.js)
  tests/
    test_submit_handler.py
    test_feedback_token.py
    test_stack_iam.py          # mirror subscribers/tests/test_stack_iam.py
```

**`cdk.json` context keys** (mirroring `DEFAULT_SUBSCRIBE_DOMAIN` / `subscribeDomainName` /
`certificateArn`):
- `feedbackDomainName` — default fallback constant `DEFAULT_FEEDBACK_DOMAIN = "feedback.mschweier.com"`
  in `stack.py`, used to lock CORS to the site origin (`https://<feedbackDomainName>`) even before
  DNS/cert exist. Optional at deploy; falls back to the default.
- `certificateArn` — optional ACM cert (us-east-1) for the custom domain. When both it and
  `feedbackDomainName` are set, CloudFront gets the alias; otherwise the site serves on its default
  `*.cloudfront.net` domain (exactly the subscribers pattern — DNS is a later human-only step).

**Resources in `FeedbackStack` (each a private `_build_*` method, mirroring the subscribers stack):**

1. **DynamoDB table `brief-feedback`**
   - `partition_key = Attribute(name="submissionId", type=STRING)` — a server-generated id
     (`uuid4` hex or `secrets.token_hex`). **No sort key, no GSI.** The PRD's success path is
     one-way collection + durable storage; there is no read/query access pattern in scope (US-5's
     later "look at the raw stored feedback" is served by a table scan/export by the owner, out of
     scope to build for). No GSI is needed.
   - `billing_mode = PAY_PER_REQUEST` (low, bursty, human-paced volume).
   - `encryption = TableEncryption.AWS_MANAGED` (SSE on).
   - `point_in_time_recovery_specification` enabled (matches the subscribers table — this is the
     durable record of reader feedback; PITR is cheap insurance).
   - `removal_policy = RemovalPolicy.RETAIN` (matches the subscribers table — do **not** lose
     collected feedback on a stack teardown; this is real data, unlike the idempotency table's
     transient DESTROY in ADR-0010).
   - **No TTL** — feedback is retained indefinitely (there is no "expire abandoned rows" notion
     here as there was for never-confirmed subscribers).

2. **The token-signing secret** (owned here, per ADR-0011): `secretsmanager.Secret`,
   `secret_name = "daily-ai-brief/feedback-token-signing-secret"`, created empty
   (no `SecretString`), `removal_policy = RETAIN`, ARN exposed as a `CfnOutput`
   (`FeedbackTokenSecretArn`) so the two send-side stacks can be granted it by ARN at their own
   deploy time.

3. **Submit Lambda `brief-feedback-submit`** with its **own least-privilege role**:
   - `runtime = PYTHON_3_13`, `architecture = ARM_64`, `handler = "handler.handler"`,
     `code = Code.from_asset(functions/submit)` — same kwargs shape as the subscribers functions.
     **No bundling / no `requirements.txt` for the function**: the handler uses only **stdlib +
     the runtime-provided `boto3`** (DynamoDB `PutItem`, Secrets Manager `GetSecretValue`, and the
     stdlib-only `feedback_token`), exactly like the subscribers functions, which carry no
     `requirements.txt` and rely on the runtime's boto3. The `_LocalPipBundling` platform-locked
     pip machinery used in `managed_agent/stack.py` is therefore **not needed here** (it exists
     only for functions with compiled/wheel dependencies like `anthropic`/`standardwebhooks`;
     this function has none). Note this explicitly in the stack so a future reader does not add it
     gratuitously.
   - Timeout `Duration.seconds(10)`, `memory_size = 128` (a DynamoDB `PutItem` + a cached secret
     fetch — sub-second work, the subscribers-function default sizing).
   - Environment: `FEEDBACK_TABLE_NAME`, `FEEDBACK_TOKEN_SECRET_ARN` (this stack's own secret ARN).
   - **Role — exactly these grants, nothing else** (FR-16, AC-15):
     - `AWSLambdaBasicExecutionRole` managed policy (own logs only).
     - `sid="FeedbackTablePut"`: `dynamodb:PutItem` on the one table ARN.
       - **In-request throttling read (FR-11/FR-17):** the throttle key is a **transient,
         non-persisted** salted keyed hash derived from the token identity (ADR-0011). The chosen
         throttle mechanism is a **conditional-write / small counter on the same `brief-feedback`
         table** keyed by a hashed throttle key + a coarse time bucket — implementable with
         `dynamodb:PutItem` (conditional) and `dynamodb:UpdateItem`/`dynamodb:GetItem` on the
         **same one table ARN**. So the role gets `dynamodb:GetItem` and `dynamodb:UpdateItem`
         **in addition to** `PutItem`, still **scoped to exactly the one `brief-feedback` table
         ARN** — no second table, no GSI, no broader resource. If the Developer instead relies on
         API Gateway stage throttling alone for v1 (acceptable per the "no CAPTCHA/WAF" posture)
         and defers the per-identity counter, the role may be `PutItem`-only; either way the
         resource is **only** the one table ARN. Document which was chosen.
     - `sid="ReadFeedbackTokenSecret"`: `secretsmanager:GetSecretValue` scoped to the one signing
       secret ARN this stack owns.
     - **No SES. No access to `brief-subscribers`. No access to `cowork-polly-tts-740353583786`.
       No reuse of any subscribers-stack role. No static keys.** (AC-15.)

4. **HTTP API (API Gateway v2)** front door — `POST /submit` → submit Lambda, mirroring the
   subscribers HTTP API:
   - Explicit `$default` `HttpStage` with `ThrottleSettings(rate_limit=..., burst_limit=...)`
     (stage-level throttle, FR-17/AC-16 — same posture as the subscribers stack's
     `rate_limit=10, burst_limit=20`; pick a low value suited to human-paced feedback).
   - `CorsPreflightOptions` **locked to the feedback site origin** —
     `allow_origins=["https://" + (feedbackDomainName or DEFAULT_FEEDBACK_DOMAIN)]`,
     `allow_methods=[POST]`, `allow_headers=["Content-Type"]` (mirrors the subscribers CORS lock).
   - `CfnOutput` the API endpoint (wire `site/config.js` `BRIEF_FEEDBACK_API_BASE_URL` to it until
     custom-domain DNS is attached — exactly the subscribers pattern).

5. **Static site** — private S3 bucket (`BlockPublicAccess.BLOCK_ALL`, `S3_MANAGED` encryption,
   `enforce_ssl=True`, `RemovalPolicy.RETAIN`) + **its own CloudFront distribution**
   (`S3BucketOrigin.with_origin_access_control`, `REDIRECT_TO_HTTPS`, `CACHING_OPTIMIZED`,
   `default_root_object="index.html"`, the same 403/404 → `/index.html` error responses) +
   `s3_deployment.BucketDeployment` uploading `site/` and invalidating `/*`. Custom domain + ACM
   cert attached only when `certificateArn` + `feedbackDomainName` are both supplied; otherwise the
   default `*.cloudfront.net` domain. This is **its own** distribution, **not** bolted onto
   `SubscribeSiteDistribution` (FR-1, §6).

**Submit handler behavior (for the developer, so no design questions remain):**
- Parse JSON body; enforce server-side validation (AC-16): each graded answer, when present, is an
  int in **1–5** (all seven optional — a partial set is valid, AC-3); each free-text answer is
  length-capped server-side (a sensible cap, e.g. 2000 chars — reject over-length inline with no
  partial record, AC-4); the **honeypot** field, if non-empty, ⇒ return a **normal-looking
  success response with no record written** (AC-2). Free-text is stored as **data only, never
  reflected unescaped** (the injection/XSS concern in §7 — it is a `PutItem` attribute value, not
  interpolated anywhere).
- Read `?t=` token from the request (the site forwards it in the POST body). `validate(secret,
  token)` per ADR-0011. The secret is fetched once per cold start (module-level cache) and
  **never logged**.
- **Anonymity resolution:** if the `anonymous` checkbox is set **or** the token is absent/invalid,
  the persisted record contains **no** identity, **no** raw token, and nothing identity-derived
  (AC-9/AC-10/AC-11); the brief **date** may be stored **only** when a valid token supplied one
  and (per FR-11) may remain even on an anonymous-checkbox record. If **not** anonymous **and**
  the token is valid, persist `identity` (email) + `briefDate` (AC-8). **No identity is written to
  logs on the persisted path** for anonymous submissions (the §6 anonymity data-handling
  constraint — the security review verifies this).
- Persist exactly one record with: the graded answers provided, the two free-text answers, the
  `anonymous` flag as applied, the server-generated `submissionId`, a server-side `createdAt`
  epoch timestamp, and `identity`/`briefDate` **only when not anonymous** (FR-14/AC-13).
  **Send no email** (the feedback path has no SES grant at all).
- Return an in-page-thank-you-driving success response (AC-14); genuine validation failures return
  a graceful inline error; nothing leaks a server error / stack trace (FR-15).

### Send-side wiring (both generators) and DNS sequencing

- `audio_email.py` (fan-out, owner + subscribers) and `welcome-send/handler.py` each import their
  local `feedback_token` copy, fetch the shared secret once (module-level cache, via the same
  `_get_secret` shape the launcher uses; the microVM does so under its IAM role via IMDSv2, the
  Lambda under its execution role), and embed a feedback link
  `<FEEDBACK_BASE_URL>/?t=<generate(secret, recipient_email, brief_date)>` into each recipient's
  HTML (owner uses the `RECIP` constant as identity, AC-7).
- The feedback base URL is a **config value** (env var `FEEDBACK_BASE_URL`), **not** hard-pointed
  at `feedback.mschweier.com` until DNS is live (PRD phase 2/4). Until DNS, it points at the
  validated CloudFront default domain; the **human** performs the DNS cutover (ACM validation CNAME
  + site-alias CNAME) post-merge, then the base URL is flipped to `https://feedback.mschweier.com`.
  This mirrors the subscribers stack's documented DNS deferral — a sequencing step, not a blocker.

### C. Abuse posture — honeypot + stage throttling + validation, no CAPTCHA/WAF

**Confirmed, not relitigated.** The PRD (FR-4, FR-17, §6, §7) fixes v1 abuse mitigation as a
hidden honeypot field + API Gateway stage-level throttling + server-side input validation, the
**same** accepted risk posture as the live subscribe form (ADR-0001, `brief_subscribers/stack.py`'s
honeypot handling + throttled `$default` stage). From an architecture-fit standpoint this is
consistent with existing precedent and appropriate for a low-volume, no-auth public form; we
**agree** and add nothing. The most abusable surface (the no-token walk-up path, no identity to
throttle on) is covered by stage throttling + honeypot only in v1, per the PRD's explicit
"revisit only if abuse is observed" stance — out of scope to pre-solve.

## Alternatives considered

- **A shared internal Python package for the token helper**, installed into all three units.
  Rejected: introduces build/versioning machinery the repo has none of, for ~40 lines of
  stdlib glue, and does not survive the microVM image build / Lambda asset zip cleanly without a
  publish step. The duplicated-module precedent (`MAX_AUDIO_ATTACHMENT_BYTES`) already governs this
  exact pair of files; extending it is lower-risk than a new packaging concept.
- **Put the helper only in the subscribers common layer and have the microVM import it.**
  Rejected: the microVM pipeline is a separate deploy unit that does **not** consume the
  subscribers Lambda layer — the layer is not on its path. There is no non-hacky way for the
  microVM to import from that layer.
- **Nest the feedback stack inside the subscribers app** (one CDK app, two stacks) to share the
  common layer and CI. Rejected outright by the PRD (FR-1, §6): the feedback surface must be a
  standalone deploy lifecycle with its own IAM, CloudFront, and no shared roles/resources.
- **A separate DynamoDB table (or ElastiCache) for the throttle counter.** Rejected: a second
  data store for a v1 non-CAPTCHA throttle is over-engineered; a conditional counter on the same
  one `brief-feedback` table keeps the IAM to a single table ARN and needs no new resource. (Or
  rely on stage throttling alone for v1 — both keep the resource scope to the one table.)
- **A GSI to query feedback by edition/identity for the later "read the raw data" use case.**
  Rejected as out of scope: US-5's later data-consumer need is served by an owner-run
  export/scan; building a query index now is speculative and enlarges the least-privilege surface.
- **`removal_policy=DESTROY` on the feedback table** (like the idempotency table, ADR-0010).
  Rejected: unlike transient dedup state, collected reader feedback is the durable product of this
  epic; RETAIN (matching the subscribers table) is correct.

## Consequences

Positive:
- A clean, standalone deploy unit with its own CloudFront/site/API/table/IAM — no coupling to, and
  no privilege bleed into, the subscribers or managed-agent stacks (FR-1, AC-15).
- Least-privilege submit role: `PutItem` (+ optional same-table `Get`/`Update` for the counter) on
  one table ARN, `GetSecretValue` on one secret ARN, own logs — no SES, no cross-stack access.
- Reuses proven patterns verbatim (HTTP API + locked CORS + throttled stage, private-bucket +
  OAC CloudFront + `BucketDeployment`, `certificateArn`/domain context with a default fallback),
  so the developer builds by mirroring `deploy/subscribers/` rather than designing anew.
- No new runtime dependency anywhere: the helper and handler are stdlib + runtime boto3, so no
  bundling machinery is introduced.

Negative / follow-ups:
- **Three hand-synced copies of `feedback_token.py`** — a real drift risk, mitigated by the sync
  docstrings and a cross-copy compatibility test (§A). The reviewer must verify all three copies
  are byte-identical and that both send paths embed the link (the PRD's two-file-drift risk, now
  three-file).
- **Manual cross-stack secret wiring**: the two send stacks must be redeployed with
  `feedbackTokenSecretArn` context once the feedback stack outputs it, and the secret value
  populated out-of-band — documented steps, not automatic imports (deliberate, to keep the three
  deploy lifecycles independent).
- **DNS cutover is a human-only post-merge step**; production emails point at the validated
  CloudFront default domain (or omit the link) until then — a sequencing constraint, not a build
  blocker.
- **Reversible.** The stack is self-contained; tearing it down (RETAIN table/secret preserved) or
  changing the throttle approach is a contained change with no one-way door.

## Verification note

This mirrors the already-deployed `deploy/subscribers/` stack's construct patterns (HTTP API,
CORS lock, throttled stage, private-bucket + OAC CloudFront + `BucketDeployment`, ARN-scoped IAM,
`certificateArn`/domain context) and the repo's duplicated-module precedent, so no `aws-docs` MCP
lookup gated it. At implementation time the Developer should confirm `cdk synth` emits: exactly the
listed grants on the submit role (one table ARN, one secret ARN, own logs, no SES), a throttled
`$default` stage, CORS locked to the feedback origin, and a distinct CloudFront distribution; and
add the handler tests enumerated in §B plus the cross-copy token-compatibility test from ADR-0011.
