# 0017. Audio-as-link delivery: serve `audio/*` through the existing subscriber distribution

- Status: **Accepted** — owner-ratified at the Gate-2 review, 2026-07-27.
- Date: 2026-07-27 (D2 amended the same day at the gate — see the note below)
- Deciders: architect (Claude), owner (Markus)

> **Gate-2 outcome (2026-07-27).** The owner ratified D0 and every decision below with two
> changes, both recorded inline:
>
> 1. **D2 was flipped against the architect's recommendation.** The architect recommended applying
>    the OAC bucket policy **out-of-band**; the owner chose a **CDK-managed `AWS::S3::BucketPolicy`**
>    in `BriefSubscribersStack`. The trade-off is explicit and was accepted with eyes open: the
>    owner buys *reviewability and drift-resistance* — the highest-consequence artifact in this
>    feature (R1) becomes IaC, visible in `cdk diff`, and assertable in the synth test — and pays
>    for it by making `BriefSubscribersStack` the **sole owner of the whole bucket-policy document**
>    on a bucket it does not otherwise own. The architect's objection (clobber/destroy blast radius)
>    is not dismissed; it is converted into two hard obligations: a **phase-1 precondition check**
>    that no bucket policy exists today, and a **standing ownership rule** (below). Given that the
>    bucket genuinely has no bucket policy today (all access is identity-based) and that every other
>    consumer reaches it through identity policies, the blast radius the architect was defending
>    against is currently empty — which is what makes the owner's preference the better call here.
> 2. **D11 was amended:** the welcome email keeps its greeting first, with the audio line directly
>    after it; the daily brief keeps the audio line first.
- PRD: [`docs/prd/audio-link-delivery.md`](../prd/audio-link-delivery.md)
- Touches: `deploy/subscribers/` (owns the distribution), `deploy/delivery/` (owns the daily
  email), the non-CDK-managed pipeline bucket `cowork-polly-tts-740353583786`.
- Related: ADR-0001 (the distribution), ADR-0005 (`briefs/` archive + audio pointer),
  ADR-0009 (welcome-send decoupling), ADR-0015 (delivery boundary is the live send path).

## Context

Every brief email today ships the narrated MP3 as a multi-megabyte MIME attachment, built in
two hand-duplicated places: `delivery_core._build_message()` (owner copy + every subscriber
copy) and `welcome-send/handler.py::_build_message()`. Both carry a defensive
`MAX_AUDIO_ATTACHMENT_BYTES = 15 MB` cut-off that **silently drops the audio** past that size.
The PRD replaces the attachment with a link to the MP3 that already exists in S3.

Four properties of the existing system constrain the design and were verified against the code,
not assumed:

1. **The audio key is Polly's, not ours.** `delivery_core.synthesize_audio()` derives
   `audio_s3_key` from the async task's `OutputUri` (`CLAUDE.md`: *use `OutputUri`, never build
   the S3 key*). Polly names the object `<prefix>.<TaskId>.mp3`, i.e. `audio/.<task-id>.mp3` —
   the leading dot is Polly's, and every fixture in the repo reflects it.
2. **The bucket is not CDK-managed.** `cowork-polly-tts-740353583786` predates all four stacks;
   its lifecycle configuration was created imperatively and is documented as an out-of-band step in
   `deploy/managed-agent/README.md`. Two CDK apps already reference it as a **plain name string**
   (`PIPELINE_BUCKET_NAME`), never as an import.

   > ⚠️ **CORRECTION (security pass, 2026-07-27; verified read-only against live AWS).** An earlier
   > revision of this bullet claimed **two** lifecycle rules, "`audio/` 7-day, `briefs/` 90-day".
   > That is **false**. `get-bucket-lifecycle-configuration` returns exactly **ONE** rule:
   > `expire-audio-7-days`, 7-day expiration, with **`Filter.Prefix = ""`** — i.e. it applies to the
   > **ENTIRE bucket**, including `briefs/`. There is **no** 90-day `briefs/` rule, and the one rule
   > that exists is **not** prefix-scoped despite its name. Corroborating evidence was sitting in
   > this feature's own phase-1 output and initially went unremarked: the `head-object` calls on
   > `briefs/<date>/audio-pointer.json` returned `Expiration` headers 7 days out, which a
   > correctly-`audio/`-scoped rule would never produce (see
   > `docs/notes/audio-link-phase1-evidence.md`).
   >
   > **This is pre-existing infrastructure drift, entirely OUTSIDE this feature's scope, and is NOT
   > fixed here.** It predates this ADR, is unaffected by it, and changing a lifecycle rule that
   > governs the durability of the whole brief archive is an **owner decision**, not a developer
   > tidy-up — so it is recorded, not corrected. Nothing in this feature depends on the distinction:
   > the email reads each object's **actual** `Expiration` header (D1) rather than assuming any rule
   > shape, so it states the truth whatever the lifecycle is. What the drift *does* mean, and what
   > the owner should weigh separately: the `briefs/` archive is being expired after 7 days, so the
   > "durable record" ADR-0005 intended does not currently exist beyond a week.
   >
   > ✅ **RESOLVED (owner-approved, 2026-07-27, in a separate session — not part of this feature).**
   > The lifecycle configuration was replaced imperatively (one
   > `put-bucket-lifecycle-configuration` call — it replaces the whole config) with two
   > prefix-scoped rules, verified live afterwards: `expire-audio-7-days` (`audio/`, 7 days —
   > semantics unchanged, as this ADR requires: the audio-link email shows that expiry date) and
   > `expire-briefs-90-days` (`briefs/`, 90 days — owner-chosen retention, restoring ADR-0005's
   > intent). Briefs already expired before the fix are unrecoverable; retention accrues from now.
   > The out-of-band-steps list in `deploy/managed-agent/README.md` records the same fix.
3. **The distribution belongs to a different deploy unit than the email.** `briefing.mschweier.com`
   is `SubscribeSiteDistribution` in `deploy/subscribers/`; the daily email is composed in
   `deploy/delivery/`. The two cannot import each other, and the repo's established answer to
   that (documented in `feedback_token.py`, `brief_history.py`, `latest_brief.py`,
   `delivery_core.py`) is hand-duplicated modules/constants plus a validating drift test.
4. **Fail-safe is binding.** `CLAUDE.md`: *never lose the brief over an audio/email glitch.*
   Every new step here is best-effort.

**D0 (owner-ratified, not re-opened here):** serve the existing Polly MP3s from the existing
pipeline bucket through the **existing** `briefing.mschweier.com` CloudFront distribution via a
new `/audio/*` behavior with Origin Access Control; public-access block stays on; the email
carries a header link stating the exact expiry date (`🎧 Listen to this brief (link expires
4 August 2026)`, Europe/Berlin, first line of the meta box); simple unguessable URLs, no
signing; direct MP3; the 7-day lifecycle is unchanged; the owner confirmation email is unchanged.

## Decision

### D1 — Expiry date comes from S3's own `x-amz-expiration`, with no computed fallback

We will read the displayed date from `head_object()`'s `Expiration` field (the
`x-amz-expiration` response header S3 returns for any object covered by a lifecycle expiration
rule), shaped `expiry-date="Tue, 04 Aug 2026 00:00:00 GMT", rule-id="..."`. We parse the
`expiry-date` with `email.utils.parsedate_to_datetime` (it is an HTTP-date), convert to
`Europe/Berlin` (`PIPELINE_TIMEZONE`), and render `f"{d.day} {d:%B} {d.year}"` → `4 August 2026`.
We deliberately build the day/month/year by hand rather than with `%-d`, which is a
glibc extension and would behave differently on a developer's macOS than on Lambda.

**We will not compute `creation + 7 days`, and we will not fall back to it.** A computed date
hardcodes the lifecycle's `7` into email-composition code in two deploy units, so a future
lifecycle change would make the email silently lie; the PRD's own framing is that the email
adapts to the lifecycle, never the reverse. If the header is absent or unparseable we degrade to
FR-10's generic note.

**Refinement of FR-10 (a strengthening, not a deviation).** The single `HeadObject` distinguishes
three outcomes, which the PRD's prose collapses:

| HeadObject outcome | Rendered line |
|---|---|
| 200 with a parseable `Expiration` | link + `(link expires 4 August 2026)` |
| 200, but `Expiration` missing/unparseable | link + `(link available for about 7 days)` |
| 404 / `NoSuchKey` / any error | **no link**: `🎧 No audio version available for this edition.` |

A 404 means the object is not there, so showing a link would be showing a *known-broken* link —
worse than the no-audio line. This makes the daily path exactly as robust as the welcome path's
existing `WELCOME_AUDIO_POINTER_STALE` handling.

**No IAM change is required for this.** `HeadObject` is authorized by `s3:GetObject`, which both
roles already hold on `audio/*` (`DeliverFunctionRole`'s `S3AudioReadWrite`,
`WelcomeSendFunctionRole`'s `S3ReadBriefsAndAudio`).

**Phase-1 gate:** the developer must confirm on a real live object that `Expiration` is actually
returned and that `ContentType` is `audio/mpeg` (see D9). If either is not true, that returns to
the human gate rather than being papered over.

### D2 — One OAC bucket-policy statement, scoped to `audio/*`, declared as a CDK resource

*(Owner decision at Gate 2, 2026-07-27, overriding the architect's out-of-band recommendation —
see the gate note at the top of this ADR for the trade-off actually bought and paid for.)*

We will add exactly this statement — and **nothing else** — to the pipeline bucket's policy:

```json
{
  "Sid": "AllowCloudFrontReadAudioPrefixOnly",
  "Effect": "Allow",
  "Principal": { "Service": "cloudfront.amazonaws.com" },
  "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::cowork-polly-tts-740353583786/audio/*",
  "Condition": {
    "StringEquals": {
      "AWS:SourceArn": "arn:aws:cloudfront::740353583786:distribution/<DISTRIBUTION_ID>"
    }
  }
}
```

Properties that are load-bearing, each defending PRD risk **R1**:

- **`Resource` ends in `/audio/*`, never `/*`.** `briefs/` (archived brief HTML/Markdown,
  listening scripts, candidates, source-usage records) stays unreachable through CloudFront.
- **No `s3:ListBucket`.** CloudFront cannot enumerate the bucket. A consequence, deliberately
  accepted: a missing key returns S3 `403 AccessDenied` rather than `404 NoSuchKey` — see D4.
- **`AWS:SourceArn` pins the one distribution** (the standard OAC confused-deputy guard; a
  CloudFront distribution ARN carries no region).
- **`origin_access_levels` stays at CDK's default `[AccessLevel.READ]`** — the OAC signs
  `GetObject`/`HeadObject` only, never writes or lists.
- **The public-access block is untouched, and must stay untouched.** `RestrictPublicBuckets` does
  **not** block this statement: PAB suppresses policies that grant access to `"*"`/anonymous
  principals, and a *service principal* pinned by `AWS:SourceArn` is not "public" by S3's
  definition. All four PAB settings stay on (AC-4) and no PAB change is needed, wanted, or
  permitted. Stated here explicitly so a future reader does not "fix" the PAB to make OAC work.

**Application mechanism: an explicit `AWS::S3::BucketPolicy` in `BriefSubscribersStack`.** Because
`addToResourcePolicy()` on a `Bucket.from_bucket_name()` import is a **silent no-op** (CDK's own
`aws_cloudfront_origins` docs: *"…you will need to update the S3 bucket policy manually. Like most
imported resources, CDK apps cannot modify the configuration of imported buckets"*), the policy
cannot be attached implicitly by the OAC origin helper. It must be the explicit L2 construct:

```python
audio_bucket = s3.Bucket.from_bucket_name(self, "PipelineAudioBucket", PIPELINE_BUCKET_NAME)
audio_bucket_policy = s3.BucketPolicy(
    self, "PipelineAudioBucketPolicy",
    bucket=audio_bucket,
    removal_policy=RemovalPolicy.DESTROY,   # explicit; see "Destroy semantics" below
)
audio_bucket_policy.document.add_statements(iam.PolicyStatement(...))  # the statement above
```

Two operational consequences follow, and both are binding:

**Ownership rule (standing, non-negotiable).** *The `cowork-polly-tts-740353583786` bucket-policy
**document** is now owned by `BriefSubscribersStack`. Every future statement on this bucket MUST be
added to that stack's `PipelineAudioBucketPolicy` construct. A statement added out-of-band with
`aws s3api put-bucket-policy` will be **silently clobbered** by the next `cdk deploy` of that
stack.* This rule goes in `deploy/subscribers/README.md`, `deploy/delivery/README.md`, and
`deploy/managed-agent/README.md`'s out-of-band-steps list — the same three places the out-of-band
runbook note would have gone, now carrying the opposite instruction. Note the asymmetry that makes
this safe today: the bucket's **lifecycle rules** remain imperative/out-of-band (they are a
different API, `PutBucketLifecycleConfiguration`, untouched by `PutBucketPolicy`), so this
decision does *not* pull the lifecycle rules into CDK and does not contradict ADR-0005's precedent.

**Precondition (phase-1, read-only, blocking).** `aws s3api get-bucket-policy --bucket
cowork-polly-tts-740353583786` must return **`NoSuchBucketPolicy`**. That is the expected result —
every current consumer (the delivery Lambda, the welcome Lambda, the microVM, the legacy
`cowork-polly-tts` IAM user) reaches this bucket through **identity-based** policies, and nothing
in the repo ever created a bucket policy. If a policy *does* exist, **STOP and return to the
gate**: its statements would have to be folded into the CDK document first, and creating the
resource without folding them in would silently revoke whatever they grant.

**Destroy semantics — analysed, and harmless.** `RemovalPolicy` defaults to `DESTROY`; we set it
explicitly so the choice is visible rather than inherited. On `cdk destroy` of
`BriefSubscribersStack` the bucket policy is deleted, which removes CloudFront's read access to
`audio/*`. This harms no other subsystem: **no other consumer of this bucket depends on the bucket
policy** — the delivery Lambda (`DeliverFunctionRole`), the welcome Lambda
(`WelcomeSendFunctionRole`), the microVM execution role and the legacy `cowork-polly-tts` IAM user
all read/write it via identity policies, which are unaffected by the absence of a bucket policy on
a same-account bucket. And the loss is moot anyway: destroying that stack also destroys the
distribution the statement is pinned to, so every audio link is dead regardless. `RETAIN` was
considered and rejected — it would strand a statement pinned to a deleted distribution's
`SourceArn` (a permanently unmatchable, therefore dead, grant) that a human would have to remember
to clean up.

**Expected synth noise.** `S3BucketOrigin.with_origin_access_control()` on an imported bucket may
still emit a CDK annotation warning about being unable to update the imported bucket's policy.
That warning is **expected and correct** — it describes the implicit path we are deliberately not
using — and does **not** mean the policy is missing. Document it next to the construct so nobody
chases it.

### D3 — `/audio/*` behavior: dedicated short-TTL cache policy, no origin request policy

| Setting | Value | Why |
|---|---|---|
| Path pattern | `/audio/*` | Matches the URL path verbatim; the origin has **no `originPath`**, so `/audio/.x.mp3` maps to S3 key `audio/.x.mp3`. |
| Origin | `S3BucketOrigin.with_origin_access_control(Bucket.from_bucket_name(...))` | The ratified OAC path (D2). |
| Allowed / cached methods | `GET, HEAD` | Nothing else is meaningful; also removes surface. |
| Viewer protocol | `REDIRECT_TO_HTTPS` | Matches the default behavior. |
| Cache policy | **new `AudioCachePolicy`**: min 0, default **1 h**, max **24 h**; no cookies, no query strings, no headers; `enableAcceptEncodingGzip=false`, `enableAcceptEncodingBrotli=false` | See below. |
| Origin request policy | **none** | S3 needs nothing forwarded, and forwarding the viewer `Host` would break the OAC SigV4 origin signature. |
| Compress | `false` | MP3 is already compressed; keeping `Accept-Encoding` out of the cache key also keeps range handling simple. |
| Response headers policy | `AudioResponseHeadersPolicy` | D5. |

We deliberately do **not** reuse `CACHING_OPTIMIZED` (the default behavior's policy): its default
TTL is 24 h and its max is one year, and Polly's objects carry no `Cache-Control`, so an edge copy
could outlive the deleted S3 object by a full day. That only ever *extends* availability, so it is
benign — but a 1 h default keeps the edge and the stated expiry date within an hour of each other,
which is the honest posture the PRD's "never over-promise" copy rule implies. Disabling the
`Accept-Encoding` cache-key dimensions also avoids the one documented interaction between
CloudFront compression and range GETs.

Range requests (**FR-4/AC-2**) need no configuration: CloudFront handles range GETs to an S3
origin natively and returns `206` with `Content-Range`. `Range` must **not** be added to the cache
key. AC-2 verifies this live in phase 1 rather than trusting the design.

**No CORS.** The link is a direct top-level navigation from a mail client, never an
`XMLHttpRequest`/`fetch` from a page on another origin, so no `Access-Control-Allow-Origin` and no
`OPTIONS` method are needed.

### D4 — Expired audio returns 404 with the subscribe page as the body; accepted, not scoped

CloudFront custom error responses are **distribution-wide** — there is no per-behavior override,
and a viewer-response CloudFront Function can change status and headers but not the body. The
existing distribution maps both `403` and `404` to `/index.html` with response status `404`
(TTL 5 min). Combined with D2's no-`ListBucket` choice, an expired or fabricated audio key
produces S3 `403` → CloudFront `404` + the subscribe page.

We accept this. It satisfies **FR-5/AC-5** (which assert the *status*), the owner already accepted
"a plain 404", and a reader who lands on the product's own page after an expired link is arguably
better served than by S3's raw XML. Scoping it would mean either a second distribution (rejected —
contradicts the ratified D0) or a Lambda@Edge origin-response function (a new runtime, a new
region-replicated resource, and a new failure mode, to change the body of a page almost nobody
will see).

Two consequences worth writing down rather than discovering:

- The 404 body is served as `text/html` from a `.mp3` URL. A download manager that swallowed the
  link may save an HTML page named `.mp3`. Cosmetic; the email already told the reader why.
- The **5-minute error-caching TTL** means a URL probed *before* the D2 bucket policy is applied
  will keep 404-ing at that edge for up to 5 minutes afterwards. Phase 1 must therefore verify
  with a **fresh, not-yet-probed key** (or invalidate), or it will "fail" a correct deployment.

### D5 — A response-headers policy sets a friendly `Content-Disposition` filename

Because Polly's key starts with a dot, "Save as" would otherwise offer a hidden dotfile
(`.7f3a….mp3`). We will attach a small `AudioResponseHeadersPolicy` to the `/audio/*` behavior
adding one custom header, `override=true`:

```
Content-Disposition: inline; filename="daily-ai-brief.mp3"
```

`inline` preserves the browser's native-player behavior (it is the default disposition anyway);
only the *save* filename changes. It reuses the filename the welcome Lambda already uses
(`MP3_ATTACHMENT_FILENAME = "daily-ai-brief.mp3"`), so the saved file is named the same way it is
today. This is ~6 lines of CDK, zero runtime cost, and nothing to maintain — cheaper than living
with the wart, and materially cheaper than the per-edition-filename alternative (a CloudFront
Function, which would additionally have no edition date available: the date is not in the key).

The same policy is the **contingency slot** for D9's Content-Type check: if phase 1 finds Polly
does not set `Content-Type: audio/mpeg` on the object, an overriding `Content-Type` header is
added here rather than mutating the object or introducing a function.

We deliberately keep the policy minimal (no `nosniff`, no HSTS): `X-Content-Type-Options: nosniff`
combined with a wrong `Content-Type` would *break* playback, which is exactly the thing phase 1
has not yet confirmed.

### D6 — The audio base URL is a hand-duplicated module constant, not CDK context

`AUDIO_BASE_URL = "https://briefing.mschweier.com"` will be a named module constant in each of the
two deploy units, sitting immediately beside the `SUBSCRIBE_SITE_URL` constant that is **already**
hardcoded to the identical host in both `delivery_core.py` and `welcome-send/handler.py`. No CDK
context, no new Lambda environment variable, no new stack parameter.

Rationale: (a) it is the same host as an already-duplicated constant, so this adds no new coupling
class; (b) the repo has a *demonstrated* failure mode for the alternative — the 2026-07-07/08
incident, documented at length in `deploy/delivery/cdk.json` and `brief_delivery/stack.py`, where a
`cdk deploy` without `-c` flags silently blanked the chrome env vars and sent real subscribers a
brief with no feedback link and broken unsubscribe links; (c) this value has no per-environment
variance and no rotation story. **FR-18** is satisfied by a single named value per unit, and the
code still treats a blank/None base URL as "no audio line" (**AC-20**) so a future move to env
config degrades safely instead of raising.

### D7 — Cross-stack coupling by bucket-name string, guarded by a synth test and a stated invariant

The subscribers stack will reference the pipeline bucket through its **existing**
`PIPELINE_BUCKET_NAME` string constant and `s3.Bucket.from_bucket_name()` — the same mechanism it
already uses to grant the welcome Lambda scoped reads on that bucket, and the same "plain string,
not a CDK cross-stack import" posture ADR-0012 chose for the feedback secret ARN. No
`Fn::ImportValue`, no stack dependency, no shared deploy lifecycle.

The coupling is documented as a **named invariant** in three places (the ADR, `README.md` of both
affected deploy units) and, more usefully, **enforced by a CDK-synth test** in
`deploy/subscribers/tests/`. Because D2 is now CDK-managed, that test covers the *entire* R1
surface — the routing rule **and** the IAM boundary — rather than only half of it. This is the
concrete upside the owner bought at the gate. It asserts on the rendered template:

*Routing side* — exactly one additional cache behavior; its `PathPattern` is exactly `/audio/*`;
**no** behavior's path pattern is broader; its origin's domain is the pipeline bucket's regional
domain; the default behavior still points at the site bucket; an
`AWS::CloudFront::OriginAccessControl` exists.

*Policy side* — exactly one `AWS::S3::BucketPolicy`; its `Bucket` is `PIPELINE_BUCKET_NAME`; its
document contains **exactly one** statement; that statement's `Resource` ends in **`/audio/*`**
(and equals the full expected ARN); `Action` is exactly `s3:GetObject`; `Effect` is `Allow`;
`Principal.Service` is `cloudfront.amazonaws.com`; and the `Condition` is a `StringEquals` on
`AWS:SourceArn` naming this distribution. A negative assertion pins the thing that actually
matters: **no** statement in the template grants any principal a resource on this bucket outside
`audio/*` (in particular, nothing matching `briefs/`, and no bare `/*`).

**The invariant, stated plainly:** *`briefing.mschweier.com` serves two origins from two owners —
the subscribe site (owned by `deploy/subscribers/`) and `audio/*` on the pipeline bucket (owned by
nobody's CDK). Replacing or recreating the distribution invalidates the D2 bucket policy's
`AWS:SourceArn` and silently breaks every outstanding audio link; the policy must be re-applied
with the new distribution ARN as part of any such change.*

### D8 — Delete the attachment machinery now; defer the IAM narrowing to a follow-up

**In scope now** (it is the code being replaced, not adjacent tidying): `synthesize_audio()` stops
downloading the MP3 to `/tmp` and stops reading it into memory — it returns
`(audio_ok, audio_s3_key)` and loses its `mp3_out_path` parameter; `MAX_AUDIO_ATTACHMENT_BYTES`
and the attachment branch of `_build_message()` are deleted in **both** units; `send_all()` loses
`mp3_bytes`/`mp3_filename` and gains the audio-link inputs; the welcome Lambda's
`_fetch_audio_bytes()` becomes a `HeadObject`-based resolver. Leaving this in place would keep a
multi-megabyte S3 GET, a `/tmp` write and a full in-memory read on the **live production send
path** for data nobody uses, and would keep dead `mp3_bytes` plumbing threaded through two
hand-duplicated chromes — precisely the drift surface **R4** warns about. This also delivers
**FR-11** for free: with no size cut-off left, an oversized MP3 is simply linked.

**Deferred to a follow-up issue:** narrowing `DeliverFunctionRole`'s `S3AudioReadWrite` from
`bucket/*` to `bucket/audio/*` + `bucket/briefs/*`. It delivers no PRD requirement, and it adds a
"did we miss a prefix?" failure mode to the exact phase where production email composition is
changing. It should not ride along.

**Reconciling the PRD's phrasing:** the PRD suggests the deliver Lambda's `s3:GetObject` "could be
narrowed" once the download is gone. It cannot be *removed*: `HeadObject` (D1) is authorized by
`s3:GetObject`, and `read_recent_prior_briefs()` still `GetObject`s `briefs/*`. `s3:PutObject` also
stays — Polly writes the MP3 **as the caller**, using this role's credentials. Net effect of this
feature on IAM in both stacks: **zero changes**, which keeps the security review narrowly focused
on the one new bucket-policy statement and the one new behavior.

### D9 — Key-entropy verification: read-only, off the archived pointer, evidence redacted

Phase 1, before any email change ships, all read-only:

0. **D2's blocking precondition first:** `aws s3api get-bucket-policy --bucket
   cowork-polly-tts-740353583786` must fail with `NoSuchBucketPolicy`. Any existing policy stops
   the phase and returns to the gate (see D2). Run this *before* the steps below, because a
   surprise here changes the design, not just the evidence.
1. **Get a real live key without listing.** `GetObject` the most recent
   `briefs/<date>/audio-pointer.json` and read its `audio_key`. This uses the same pointer the
   welcome Lambda reads, needs only `s3:GetObject`, and avoids granting/using `s3:ListBucket`.
   Repeat for **three different days** so non-sequentiality is observable, not assumed.
2. **`aws s3api head-object`** on that key, recording `ContentType` (**must be `audio/mpeg`** —
   gates AC-1 and the "plays inline" goal), `ContentLength`, `LastModified`, and **`Expiration`**
   (gates D1/FR-8). This single call closes two of the PRD's three phase-1 unknowns.
3. **Entropy assessment, no AWS.** The key is `audio/.<TaskId>.mp3`. **Pass criterion:** the
   `<TaskId>` segment matches the RFC 4122 UUID shape
   `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` (**122 bits** of randomness for
   a v4 UUID), and the three sampled ids share no common prefix and are not ordered. Anything
   sequential, short, or timestamp-derived **fails** and returns to the human gate — per the PRD,
   that would change the architecture, not an implementation detail.
4. **Key hygiene — evidence documents AND test fixtures, AND runtime logs.** A live `audio/*` key
   is a working **public URL** for as long as the object exists (seven days), and this repository is
   **public**, so a real key must never be committed anywhere, in any form:
   - **Evidence docs:** record the key *shape* and the bit count only, with the middle of the real
     task id **redacted**.
   - **Test fixtures (added after a security pass found real keys committed as fixtures):** use a
     **synthetic** UUID — e.g. `audio/.00000000-0000-4000-8000-123456789abc.mp3`, which keeps the
     RFC 4122 v4 *shape* every shape-dependent test needs while being unmistakably fake. Never
     paste a key copied from a real `audio-pointer.json`, a real `head-object` response, or the
     phase-1 evidence into a test, even as a "realistic" fixture: the test outlives the object's
     lifetime in git history, and history on a public repo is permanent and not scrubbable after a
     push.
   - **Runtime logs:** never log a full key. `audio_link._redact_audio_key()` (hand-duplicated
     alongside the rest of that module) is the single helper both deploy units use for this; every
     key-bearing log line in `delivery_core.resolve_audio_metadata()` and the welcome-send
     handler's audio resolver goes through it.
5. **A code test (no AWS) for AC-6:** assert the URL is `AUDIO_BASE_URL + "/" + audio_key` with the
   key passed through verbatim from the `OutputUri`-derived value, plus a negative test that no
   brief date, subject, or other guessable input can reach the URL. The builder uses
   `urllib.parse.quote(audio_key, safe="/")` so an unexpected character cannot yield a malformed
   link while `/` is preserved.

### D10 — Shared audio-link logic is one hand-duplicated module, with a byte-equality drift test

The URL builder, the `x-amz-expiration` parser, the date formatter, **and the rendered HTML line**
live in one small new module. The module owns the **line** — byte-identical in both emails — while
each unit owns only **where** that line goes in its own meta box (D11), so the gate's ordering
amendment costs nothing in duplication: the drift-prone artifact (the ratified copy, the URL, the
date format) is still defined exactly once per unit and cross-checked. The module is
`audio_link.py`, hand-duplicated into `deploy/delivery/functions/deliver/` and
`deploy/subscribers/layers/common/python/` — the repo's established convention
(`feedback_token.py` across three units, `brief_history.py`, `latest_brief.py`), carrying the same
"keep in sync by hand" docstring. A test reads the *other* unit's copy from the repo and asserts
byte-equality, following the existing cross-unit precedent of
`deploy/subscribers/tests/test_send_time_consistency.py`. This turns **R4** from "remember to
mirror the change" into a failing test.

### D11 — Line placement and copy (amended at Gate 2)

The audio line is the **first** line of the daily brief's meta box (**FR-7**). In the **welcome**
email the greeting stays first and the audio line comes **directly after it** (owner amendment at
Gate 2, 2026-07-27, refining the architect's literal reading of FR-13): greeting → 🎧 audio →
unsubscribe → feedback → disclaimer. Greeting-first is the right call — the welcome email's whole
job is to say "you're subscribed," and leading with a listen link buries that. **AC-18** is
unaffected: it already permits the welcome intro as a difference between the two boxes, and the
audio line remains the first line *after* the allowed difference, so the two chromes stay aligned.

Rendered (identical strings in both emails, per D10):

```html
<p style="margin:0 0 6px 0;">🎧 <a href="{url}">Listen to this brief</a> (link expires 4 August 2026)</p>
<p style="margin:0 0 6px 0;">🎧 No audio version available for this edition.</p>
<p style="margin:0 0 6px 0;">🎧 <a href="{url}">Listen to this brief</a> (link available for about 7 days)</p>
```

Cold start (no brief ever archived) shows **no** audio line at all (**AC-17**). Everything in this
decision is a one-line change; it is recorded so the gate can adjust it cheaply, not because it is
architecturally significant.

## Alternatives considered

- **Compute `creation + 7 days` for the expiry (D1).** Simplest, needs no `HeadObject`, and would
  in practice agree with S3 (which rounds creation+N up to the next UTC midnight). Rejected: it
  hardcodes the lifecycle constant into two email-composition modules, so a lifecycle change makes
  the email lie silently, and it cannot express "this specific object's remaining life" for the
  welcome email (FR-14) any more honestly than the header already does.
- **Applying the bucket policy out-of-band via `aws s3api put-bucket-policy` (D2).** The
  architect's original recommendation, and the option the owner **rejected** at Gate 2. It matches
  the precedent already documented for this bucket's lifecycle rules, and it keeps
  `BriefSubscribersStack` from owning a resource on a bucket it does not own — so a stack
  destroy/rollback could never delete a policy other subsystems might one day depend on, and a
  pre-existing statement could never be clobbered on create. Rejected because those risks are
  *hypothetical* (the bucket has no policy today, and every consumer uses identity-based access —
  both verified as D2's phase-1 precondition) while the cost is *real and recurring*: the
  single highest-consequence artifact in this feature (R1) would live outside `cdk diff`, outside
  the synth test, and outside the normal review loop, kept correct only by a runbook step and a
  read-modify-write nobody would re-run. The chosen option converts a standing review-discipline
  problem into two one-time obligations (the precondition check and the ownership rule).
- **`RemovalPolicy.RETAIN` on the bucket policy (D2).** Would survive a stack destroy. Rejected:
  it strands a statement pinned to a deleted distribution's `SourceArn` — a permanently
  unmatchable, therefore dead, grant left for a human to notice and clean up.
- **`Resource: bucket/*` with the prefix enforced only by the behavior's path pattern (D2).**
  Rejected outright: it makes the correctness of `briefs/` confidentiality depend on a CloudFront
  routing rule rather than an IAM boundary. This is R1.
- **Adding `s3:ListBucket` so missing keys return a true 404 (D2/D4).** Rejected: it grants
  CloudFront bucket enumeration to fix a status code the distribution's existing custom error
  response already normalizes to 404.
- **A second, audio-only CloudFront distribution (D4).** Would allow audio-specific error
  responses and cache settings. Rejected: contradicts the ratified D0, adds a distribution, a
  second certificate/alias decision, and a second DNS record, all to improve the body of an
  expired-link page.
- **Lambda@Edge / CloudFront Function for a bespoke expired-audio page or a per-edition filename
  (D4/D5).** Rejected: a new runtime and a new failure mode on the read path for cosmetics; the
  per-edition filename additionally has no data source (the edition date is not in the key).
- **`CACHING_OPTIMIZED` on `/audio/*` (D3).** Zero new resources. Rejected as a *default* rather
  than a decision: 24 h default / 1 year max TTL lets an edge copy outlive the stated expiry by a
  day, which is benign but not deliberate.
- **CDK context / Lambda env var for the audio base URL (D6).** Consistent with
  `FEEDBACK_BASE_URL`. Rejected: this repo has a real, recent production incident caused by
  exactly that mechanism silently resolving empty, and unlike the feedback URL this value has no
  environment variance and is already hardcoded (as `SUBSCRIBE_SITE_URL`) in both units.
- **A shared Python package for `audio_link.py` (D10).** The correct answer in a monorepo with a
  build system. Rejected here for the same reason the repo rejected it four times already: these
  are independently packaged Lambda deploy units with no shared package, and introducing one for
  ~40 lines would be a larger architectural change than the feature.
- **Narrowing `S3AudioReadWrite` in this epic (D8).** Rejected as scope: no PRD requirement, and it
  introduces a production-send failure mode during the phase where the email path is changing.

## Consequences

**Positive**

- Subscriber inboxes stop growing by multiple megabytes per weekday; per-email size drops from
  multi-MB to well under 100 KB (AC-7), and SES's $0.12/GB attachment-data charge goes to zero.
- The 15 MB silent-drop failure mode is deleted, not worked around (FR-11).
- **No *identity*-IAM change in either stack** (D1/D8), so the security review reduces to one
  bucket-policy statement and one cache behavior — and, because of the gate's D2 flip, **both are
  now IaC**: visible in `cdk diff`, asserted by the synth test, and reviewable in the PR diff
  rather than in a runbook. R1 is guarded by a test that fails, not by a checklist item.
- Rollback stays a single small revert per deploy unit: the `/audio/*` behavior is inert on its own
  and can stay deployed while the email templates revert to attachments.
- The delivery Lambda stops doing a multi-MB S3 download + `/tmp` write + full in-memory read on
  every production run.

**Negative / newly constrained**

- **The project gains its first publicly readable path into the pipeline bucket.** Its correctness
  rests on one prefix-scoped bucket-policy statement and one path pattern. This is why D2's
  scoping, D7's synth test, and a mandatory security-engineer pass before deploy are all
  non-negotiable.
- **A cross-deploy-unit dependency now exists**: the email in `deploy/delivery/` links to a
  behavior in `deploy/subscribers/`. Recreating the distribution breaks every outstanding link
  until the policy's `AWS:SourceArn` is updated (D7's invariant) — though with D2 CDK-managed, a
  distribution replacement inside the stack updates the `SourceArn` automatically, so the invariant
  now bites only on an out-of-stack recreation.
- **`BriefSubscribersStack` now owns the pipeline bucket's whole policy document** (D2, owner's
  gate decision). This is the accepted cost of that choice, and it creates a genuinely new failure
  mode that did not exist before: **a statement added to this bucket out-of-band will be silently
  clobbered by the next `cdk deploy` of the subscribers stack.** The ownership rule in D2 is the
  only defence, and it is documentation-only — nothing in AWS or CDK will warn about it. It is
  recorded in three READMEs precisely because it is unenforceable in code.
- **A stack whose subject is "the subscriber site" now reaches into a bucket owned by the content
  pipeline.** The name no longer describes the scope. Worth remembering when someone reasons about
  what `cdk destroy BriefSubscribersStack` touches (analysed in D2: harmless today, because every
  other consumer uses identity-based access — but that is a property of *today's* access pattern,
  not a guarantee).
- **The stated expiry is the *start* of that day.** An object expiring `2026-08-04T00:00:00Z`
  becomes eligible for deletion at 02:00 Europe/Berlin on 4 August, so `link expires 4 August 2026`
  can read as "available during 4 August". In practice S3's deletion lag makes availability longer,
  never shorter — the copy under-promises in the safe direction. Recorded, not changed: the wording
  is owner-ratified.
- **AC-9's "UTC→Berlin changes the calendar day" clause is not reachable with real data.** Berlin
  is always ahead of UTC, so a midnight-UTC expiry always maps to the same Berlin date. The test
  must use a synthetic non-midnight `expiry-date` (e.g. `23:30 GMT`) to prove the conversion is
  applied at all. Worth knowing before someone "fixes" a test that cannot fail.
- **Readers lose offline listening** (PRD R2, accepted) and behaviour becomes email-client
  dependent (R3) — some mobile clients hand `.mp3` URLs to a download manager rather than an inline
  player. The AC set validates the server contract; client behaviour is validated by hand on the
  owner's own clients in phase 2.
- **An audio-less day is less visible to the owner** (R5): a missing attachment used to be obvious;
  a swapped line is subtler. Mitigated by FR-17's structured log line only — the confirmation email
  stays unchanged per D0.
- Four assumptions remain **unverified against live AWS** and are phase-1 gates, not design
  conclusions: the bucket has **no existing bucket policy** (D2 — the one that would change the
  design rather than an implementation detail); `Expiration` is returned on these objects (D1);
  `ContentType` is `audio/mpeg` (D5); the Polly task id is UUID-shaped (D9). Each has a defined
  stop-and-return-to-gate outcome.

**Follow-ups this creates**

- A GitHub issue for the deferred `S3AudioReadWrite` prefix narrowing (D8).
- README notes in `deploy/subscribers/` and `deploy/delivery/` recording D7's dual-purpose-
  distribution invariant and **D2's ownership rule**, plus an entry in
  `deploy/managed-agent/README.md`'s out-of-band-steps list stating that the bucket **policy** is
  now CDK-managed by `BriefSubscribersStack` while the bucket's **lifecycle rules** remain
  imperative — the two are different APIs and must not be conflated.
