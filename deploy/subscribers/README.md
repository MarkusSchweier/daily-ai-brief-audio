# Public subscriber surface — CDK deploy & runbook

> Built 2026-07-02 per `docs/prd/public-subscriptions.md` and `docs/adr/0001`, `docs/adr/0002`,
> `docs/adr/0003`. This is the **new, additive** public subscribe/confirm/unsubscribe surface for
> the daily AI brief — it does not replace or touch the existing owner-only Polly/S3/SES flow
> documented in `deploy/audio-mail-integration.md`; it extends `deploy/audio_email.py`'s
> recipient list. Everything here is provisioned by a single CDK stack, `BriefSubscribersStack`,
> in this directory (`deploy/subscribers/`).

## What CDK deploys (account `740353583786`, region `us-east-1`)

| Resource | Logical ID | Purpose |
|---|---|---|
| DynamoDB table | `SubscribersTable` (`brief-subscribers`) | PK `email`; GSI `status-index`; TTL on `confirmTokenExpiresAt` |
| Lambda layer | `SubscriberCommonLayer` | Shared email/token/DynamoDB helper module |
| Lambda | `SubscribeFunction` (`brief-subscribers-subscribe`) | `POST /subscribe` — create pending row + send confirm email |
| Lambda | `ConfirmFunction` (`brief-subscribers-confirm`) | `GET /confirm` — activate a pending subscription; on the real transition, async-invokes `WelcomeSendFunction` (docs/adr/0009) |
| Lambda | `WelcomeSendFunction` (`brief-subscribers-welcome-send`) | Internal only (no HTTP route) — emails a newly confirmed subscriber the latest archived brief + audio, or a welcome-only email on cold start (docs/prd/instant-welcome-brief.md) |
| Lambda | `UnsubscribeFunction` (`brief-subscribers-unsubscribe`) | `GET /unsubscribe` — mark unsubscribed (idempotent) |
| IAM roles | `SubscribeFunctionRole`, `ConfirmFunctionRole`, `WelcomeSendFunctionRole`, `UnsubscribeFunctionRole` | Function-scoped least privilege, no static keys (ADR-0002 §A). Per ADR-0009, the SES send + scoped `cowork-polly-tts-740353583786` S3 read (`briefs/*`, `audio/*`) live on `WelcomeSendFunctionRole`, NOT `ConfirmFunctionRole` (which gets only `lambda:InvokeFunction` on the welcome-send Lambda's ARN). |
| HTTP API | `SubscribersHttpApi` | Routes for the three Lambdas; throttled stage; CORS locked to the subscribe site origin |
| S3 bucket | `SubscribeSiteBucket` | Private, OAC-only, hosts the static site |
| CloudFront distribution | `SubscribeSiteDistribution` | Serves the site over HTTPS; optional custom domain + ACM cert |

One stack, one lifecycle — see `docs/adr/0001` for why site/API/data are not split into
separate stacks. The pre-existing Polly/S3/SES resources (`deploy/iam-policy.json` etc.) are
**not** managed by this CDK app; they stay imperative, as documented in
`deploy/audio-mail-integration.md`.

## Prerequisites

- Node.js + npm (for the `aws-cdk` CLI; the Python construct library `aws-cdk-lib` alone is not
  enough — it's a jsii-wrapped package that shells out to a Node process). If missing:
  `brew install node && npm install -g aws-cdk`.
- Python 3.13 (matches the Lambda runtime) with a project-local virtualenv:
  ```bash
  cd deploy/subscribers
  python3 -m venv .venv
  .venv/bin/pip install -r requirements-dev.txt
  ```
- AWS credentials for account `740353583786` with permission to create the resources above.
  **Confirm the active AWS account before any deploy** (`/aws-account` / `aws-account-guard`
  convention from this repo's global operating manual). This CDK app is a *separate* deploy
  surface from the `cowork-polly-tts` IAM user — use whatever credentials/profile you deploy
  CDK stacks with in this account, not the `cowork-polly-tts` static key (that key is for the
  Mac scheduled task only, never for CDK deploys).
- **Security gate cleared 2026-07-02:** the `cowork-polly-tts` IAM user's access key was
  rotated (new key issued and verified, old key deactivated) before this fan-out was enabled —
  see `docs/adr/0002`, follow-ups section, and `deploy/validation-handoff.md`. Step 2 below
  (apply the updated IAM policy) is safe to run.

## Context parameters

| Context key | Purpose | Default when unset |
|---|---|---|
| `subscribeDomainName` | The subscribe site's own origin, used to lock down HTTP API CORS and (if `certificateArn` is also set) as the CloudFront alias | `briefing.mschweier.com` (CORS only; no CloudFront alias) |
| `certificateArn` | An existing **us-east-1** ACM certificate ARN, validated for `subscribeDomainName` | unset — distribution serves on its default `*.cloudfront.net` domain only |
| `feedbackTokenSecretArn` | The `deploy/feedback/` stack's `FeedbackTokenSecretArn` output (docs/prd/reader-feedback.md, ADR-0011/ADR-0012). Optional and backward-compatible: when supplied, `WelcomeSendFunctionRole` gains a `ReadFeedbackTokenSecret` statement scoped to exactly that ARN and the welcome-send Lambda's `FEEDBACK_TOKEN_SECRET_ARN` env var is set; when absent, no grant/env var is added and the stack still synths/deploys cleanly. | unset — no grant added |
| `feedbackBaseUrl` | The feedback site's base URL to embed in the welcome email's feedback link (e.g. the feedback stack's `DistributionDomainName` output during validation, or `https://feedback.mschweier.com` after DNS cutover). Deliberately has **no** default pointing at the live custom domain (ADR-0012 §B "DNS sequencing") — if unset while `feedbackTokenSecretArn` is set, the welcome-send Lambda's handler simply skips the link (fail-safe, never blocks the send). | unset — no env var added, link skipped |

Pass via `-c key=value` on any `cdk` command, e.g.:

```bash
cdk deploy -c subscribeDomainName=briefing.mschweier.com \
           -c certificateArn=arn:aws:acm:us-east-1:740353583786:certificate/xxxxxxxx
```

If you don't yet have the ACM cert (see DNS section below), omit `certificateArn` — the stack
still deploys cleanly and the site is reachable at the CloudFront default domain in the
meantime, per ADR-0001 ("attaching DNS is a manual runbook step").

## Deploy

```bash
cd deploy/subscribers
source .venv/bin/activate   # or prefix commands with .venv/bin/
cdk bootstrap                                   # once per account/region, if not already done
cdk synth                                       # static validation, no AWS calls
cdk diff                                        # review what would change
cdk deploy -c subscribeDomainName=briefing.mschweier.com
```

Note the stack outputs after a successful deploy — you will need them for the manual steps
below:

- `HttpApiUrl` — the temporary `execute-api` base URL.
- `DistributionDomainName` — the CloudFront `*.cloudfront.net` domain.
- `SiteBucketName` — the private S3 bucket the site assets are deployed into.
- `SubscribersTableName` / `SubscribersTableArn` — for the manual IAM step below.
- `SubscribersStatusIndexArn` — **must match** the ARN already hardcoded into
  `deploy/iam-policy.json`'s `DynamoDBSubscribersQuery` Sid. If the table name or account/region
  ever changes, update that Sid to match this output.

## Manual steps this stack does NOT do

This sandbox does not have DNS access for `mschweier.com`, and the PRD explicitly keeps SES in
the sandbox for this epic — both are called out here rather than guessed at.

### 1. Point the site's config at the real API URL

`deploy/subscribers/site/config.js` ships with a placeholder:

```js
window.BRIEF_SUBSCRIBERS_API_BASE_URL = "https://REPLACE-WITH-EXECUTE-API-URL.execute-api.us-east-1.amazonaws.com";
```

After `cdk deploy`, replace the placeholder with the real `HttpApiUrl` output, then re-run
`cdk deploy` (the `BucketDeployment` construct re-syncs `site/` and invalidates CloudFront
automatically). Once the custom domain is attached (step 3), you may point this at that domain's
`/subscribe` etc. instead, if the API also gets a custom domain — not required for the epic.

### 2. Apply the updated `deploy/iam-policy.json` to the live `cowork-polly-tts` user

`deploy/iam-policy.json` is source-of-truth but **not auto-applied**. After confirming
`SubscribersStatusIndexArn` matches the Sid already in the file:

```bash
aws iam put-user-policy \
  --user-name cowork-polly-tts \
  --policy-name cowork-polly-tts-least-priv \
  --policy-document file://deploy/iam-policy.json
```

Verify: `aws iam get-user-policy --user-name cowork-polly-tts --policy-name cowork-polly-tts-least-priv`.

### 3. DNS for `briefing.mschweier.com` (requires DNS access this sandbox does not have)

1. Request/validate an ACM certificate in **us-east-1** for `briefing.mschweier.com` (DNS
   validation — add the CNAME ACM gives you to the `mschweier.com` zone, same pattern as the
   existing SES DKIM CNAMEs in `deploy/audio-mail-integration.md`).
2. Re-deploy with `-c certificateArn=<the cert ARN>` so CloudFront gets the alias.
3. Add a DNS record for `briefing.mschweier.com` pointing at the distribution's domain name
   (`DistributionDomainName` output) — a CNAME (or ALIAS/ANAME if the DNS host supports apex-like
   aliasing for subdomains) to that `*.cloudfront.net` name.
4. Wait for propagation (minutes–hours, same caveat as the SES DKIM setup), then confirm
   `https://briefing.mschweier.com` serves the site.

Until this is done, the site is reachable at the CloudFront default domain
(`DistributionDomainName` output) and that is sufficient for the sandbox validation loop.

### 4. Verify test-recipient addresses in SES (sandbox constraint)

Per the PRD, SES stays in the sandbox for this epic — no production access request. Every
address used as a stand-in "subscriber" during testing must be individually verified first:

```bash
aws sesv2 create-email-identity --email-identity your-test-address@example.com --region us-east-1
```

Then click the verification link SES emails to that address. `aibriefing@mschweier.com` itself
needs **no separate verification** — it's a sub-address of the already-DKIM-verified
`mschweier.com` domain identity (ADR-0002); the sandbox only gates the *recipient*.

### 5. Wire `audio_email.py`'s new env vars into the scheduled task

`deploy/audio_email.py` now reads two optional env vars for the fan-out:

- `SUBSCRIBERS_TABLE_NAME` (defaults to `brief-subscribers`; only set it if you renamed the
  table).
- `SUBSCRIBERS_API_BASE_URL` — must be set to the same base URL as `config.js` above (the
  `HttpApiUrl` output, or the custom domain once attached) so subscriber emails get a working
  personalized unsubscribe link. If unset, the unsubscribe link will be malformed (empty base).

Add both to STEP 6 of `~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md` **only after** the
validation loop below passes — do not sync the inline copy prematurely (existing repo
convention, see `deploy/scheduled-task-audio.md`).

## Testing each Lambda via curl (temporary `execute-api` URL)

Replace `$API` with the `HttpApiUrl` stack output.

```bash
API="https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com"

# subscribe — should 200 and (if the address is SES-verified) trigger a confirmation email
curl -s -X POST "$API/subscribe" \
  -H "Content-Type: application/json" \
  -d '{"email":"your-verified-test-address@example.com","firstName":"Test","lastName":"Subscriber"}'

# honeypot filled — should still 200, but no email/record (AC-14)
curl -s -X POST "$API/subscribe" \
  -H "Content-Type: application/json" \
  -d '{"email":"bot@example.com","firstName":"Bot","lastName":"Actor","website":"http://spam.example"}'

# invalid email — should 400, no record/email (AC-13)
curl -s -X POST "$API/subscribe" \
  -H "Content-Type: application/json" \
  -d '{"email":"not-an-email","firstName":"Test","lastName":"Subscriber"}'

# confirm — use the email + token from the confirmation email's link
curl -s "$API/confirm?email=your-verified-test-address%40example.com&token=<token-from-email>"

# unsubscribe — use the email + unsubscribeToken from a delivered brief's footer link
# (or read it directly from the DynamoDB row via `aws dynamodb get-item` while testing)
curl -s "$API/unsubscribe?email=your-verified-test-address%40example.com&token=<unsubscribeToken>"

# unsubscribe again — should still 200 (idempotent, AC-12), no error, no re-subscribe
curl -s "$API/unsubscribe?email=your-verified-test-address%40example.com&token=<unsubscribeToken>"
```

Inspect the row directly while testing:

```bash
aws dynamodb get-item --table-name brief-subscribers \
  --key '{"email":{"S":"your-verified-test-address@example.com"}}' --region us-east-1
```

## End-to-end validation loop (do this before going live)

Mirrors `deploy/validation-handoff.md`'s style for the existing owner-only flow:

1. `curl POST /subscribe` with a verified test address → confirm the row is `pending` and a
   confirmation email arrives from `aibriefing@mschweier.com` (AC-1).
2. Click (or curl) the confirm link within 48h → row becomes `confirmed`, landing page shown
   (AC-2).
3. Run `deploy/audio_email.py` (with `SUBSCRIBERS_TABLE_NAME` / `SUBSCRIBERS_API_BASE_URL` set)
   → confirm the test address receives the brief from `aibriefing@mschweier.com` with an
   unsubscribe footer link, **and** `mail@mschweier.com` still receives its unchanged copy
   (AC-3, AC-6).
4. Click the footer unsubscribe link → confirmation page shown, row becomes `unsubscribed`
   (AC-4).
5. Run `deploy/audio_email.py` again → confirm the now-unsubscribed address does **not** receive
   the brief, and the owner's copy is still unaffected (AC-5).
6. Repeat step 3 with a second test address that is intentionally *not* SES-verified (so its
   send fails) alongside a verified one → confirm the verified address and the owner both still
   receive the brief, and `SES_SEND_FAILED:` / `SES_SENT_SUMMARY sent=N failed=M` are logged
   (AC-8).
7. Re-subscribe the address unsubscribed in step 4 and confirm it again → confirm it receives
   the brief again on the next run (AC-15).

## Teardown

```bash
cd deploy/subscribers
cdk destroy
```

`cdk destroy` will **not** remove:

- The DynamoDB table and S3 site bucket — both have `RemovalPolicy.RETAIN` deliberately (avoid
  silently losing subscriber data or the built site on an accidental `cdk destroy`). Delete them
  manually if you really mean to tear the epic down entirely:
  ```bash
  aws dynamodb delete-table --table-name brief-subscribers --region us-east-1
  aws s3 rb s3://<SiteBucketName> --force --region us-east-1
  ```
- The `deploy/iam-policy.json` changes applied to `cowork-polly-tts` (step 2 above) — revert
  that policy manually if you want the owner's user to drop the second-sender/GSI-query grants.
- Any DNS records added for `briefing.mschweier.com` or the ACM certificate — remove those
  through whatever DNS host manages `mschweier.com`, and `aws acm delete-certificate` for the
  cert if no longer needed.
- CloudFront distributions can take several minutes to fully disable/delete; `cdk destroy` will
  wait but budget time for it.

## Local validation without a real AWS deploy

```bash
cd deploy/subscribers
.venv/bin/python3 -m py_compile app.py brief_subscribers/*.py functions/*/handler.py layers/common/python/*.py
.venv/bin/cdk synth                 # requires Node.js + `npm install -g aws-cdk`
.venv/bin/python3 -m pytest tests/ -v
```
