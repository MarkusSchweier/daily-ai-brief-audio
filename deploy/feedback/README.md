# Reader feedback surface — CDK deploy & runbook

> Built 2026-07-03 per `docs/prd/reader-feedback.md` and `docs/adr/0011`
> (signed feedback-link token scheme) / `docs/adr/0012` (this stack's shape + the
> token-helper packaging). This is a **new, standalone** public feedback form + submit
> API for the daily AI brief — it shares **no** resource or IAM role with
> `deploy/subscribers/` or `deploy/managed-agent/`, and does not touch them. Everything
> here is provisioned by a single CDK stack, `FeedbackStack`, in this directory
> (`deploy/feedback/`).

## What CDK deploys (account `740353583786`, region `us-east-1`)

| Resource | Logical ID | Purpose |
|---|---|---|
| DynamoDB table | `FeedbackTable` (`brief-feedback`) | PK `submissionId`; no sort key, no GSI, no TTL — durable, indefinite storage of every submission |
| Secrets Manager secret | `FeedbackTokenSigningSecret` (`daily-ai-brief/feedback-token-signing-secret`) | The HMAC-SHA256 signing secret feedback links are signed with (ADR-0011) — **created empty**, populated out-of-band (step below) |
| Lambda | `SubmitFunction` (`brief-feedback-submit`) | `POST /submit` — validates, resolves anonymity, and persists one feedback record |
| IAM role | `SubmitFunctionRole` | Function-scoped least privilege: `dynamodb:PutItem` only on the one table (no throttle counter was built, so no Get/Update grant), `secretsmanager:GetSecretValue` on the one signing secret — no SES, no other table/bucket access, no static keys |
| HTTP API | `FeedbackHttpApi` | `POST /submit`; throttled stage; CORS locked to the feedback site origin |
| S3 bucket | `FeedbackSiteBucket` | Private, OAC-only, hosts the static feedback form |
| CloudFront distribution | `FeedbackSiteDistribution` | Serves the site over HTTPS; its **own** distribution, not shared with the subscribe site; optional custom domain + ACM cert |

One stack, one lifecycle — mirrors `deploy/subscribers/`'s single-stack shape
(ADR-0012 §B). The token-signing secret is **owned here** (the feedback stack is the
only genuinely new stack in this epic) and its ARN is handed to the two send-side
stacks (`deploy/managed-agent`, `deploy/subscribers`) as a context value at their own,
independent deploy time — see "Wiring the signing secret into the other two stacks"
below.

## Prerequisites

- Node.js + npm (for the `aws-cdk` CLI — same reason as `deploy/subscribers/`, jsii
  shells out to Node). If missing: `brew install node && npm install -g aws-cdk`.
- Python 3.13 (matches the Lambda runtime) with a project-local virtualenv:
  ```bash
  cd deploy/feedback
  python3 -m venv .venv
  .venv/bin/pip install -r requirements-dev.txt
  ```
- AWS credentials for account `740353583786` with permission to create the resources
  above. **Confirm the active AWS account before any deploy** (`/aws-account` /
  `aws-account-guard` convention from this repo's global operating manual). This is a
  *separate* deploy surface from the `cowork-polly-tts` IAM user and from the
  subscribers/managed-agent stacks' roles — never reuse those credentials/roles here.

## Context parameters

| Context key | Purpose | Default when unset |
|---|---|---|
| `feedbackDomainName` | The feedback site's own origin, used to lock down HTTP API CORS and (if `certificateArn` is also set) as the CloudFront alias | `feedback.mschweier.com` (CORS only; no CloudFront alias) |
| `certificateArn` | An existing **us-east-1** ACM certificate ARN, validated for `feedbackDomainName` | unset — distribution serves on its default `*.cloudfront.net` domain only |

Pass via `-c key=value` on any `cdk` command, e.g.:

```bash
cdk deploy -c feedbackDomainName=feedback.mschweier.com \
           -c certificateArn=arn:aws:acm:us-east-1:740353583786:certificate/xxxxxxxx
```

If you don't yet have the ACM cert (see DNS section below), omit `certificateArn` — the
stack still deploys cleanly and the site is reachable at the CloudFront default domain
in the meantime, matching `deploy/subscribers/`'s identical deferral.

## Deploy

```bash
cd deploy/feedback
source .venv/bin/activate   # or prefix commands with .venv/bin/
cdk bootstrap                                  # once per account/region, if not already done
cdk synth                                      # static validation, no AWS calls
cdk diff                                       # review what would change
cdk deploy -c feedbackDomainName=feedback.mschweier.com
```

Note the stack outputs after a successful deploy — you will need them for the manual
steps below:

- `HttpApiUrl` — the temporary `execute-api` base URL.
- `DistributionDomainName` — the CloudFront `*.cloudfront.net` domain.
- `SiteBucketName` — the private S3 bucket the site assets are deployed into.
- `FeedbackTableName` / `FeedbackTableArn` — the durable feedback store.
- `FeedbackTokenSecretArn` — **needed by both other stacks**, see below.

## Manual steps this stack does NOT do

### 1. Point the site's config at the real API URL

`deploy/feedback/site/config.js` ships with a placeholder:

```js
window.BRIEF_FEEDBACK_API_BASE_URL = "https://REPLACE-WITH-EXECUTE-API-URL.execute-api.us-east-1.amazonaws.com";
```

After `cdk deploy`, replace the placeholder with the real `HttpApiUrl` output, then
re-run `cdk deploy` (the `BucketDeployment` construct re-syncs `site/` and invalidates
CloudFront automatically). Once the custom domain is attached (step 3), you may point
this at that domain's `/submit` instead, if the API also gets a custom domain — not
required for the epic.

### 2. Populate the feedback-token signing secret

The `FeedbackTokenSigningSecret` is created **empty** (CDK/CloudFormation cannot set a
real value here without it landing in a template/state file, ADR-0011). Generate a
256-bit random value and populate it after first deploy, using the
`FeedbackTokenSecretArn` value from the `cdk deploy` output:

```bash
aws secretsmanager put-secret-value \
  --secret-id <FeedbackTokenSecretArn-from-output> \
  --secret-string "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

**Caveat:** the generated secret value is not printed to stdout by this command (only
piped straight into `put-secret-value`), but the `python3 -c '...'` invocation itself
may still be recorded in your shell history depending on your shell's history
settings. If that's a concern, generate the value in a way your shell doesn't log
(e.g. a `HISTCONTROL=ignorespace`-prefixed command, or write it to a temp file outside
shell history and pass `--secret-string file://...`), then delete the temp file.

### 3. DNS for `feedback.mschweier.com` (requires DNS access this sandbox does not have)

**Status as of 2026-07-04: the ACM certificate has already been requested — only the
two DNS records below are left, and they require the human** (this sandbox has no DNS
API access for `mschweier.com`, exactly as documented in `deploy/subscribers/README.md`;
`mschweier.com`'s registrar is external, not Route53).

1. **Add this CNAME now** (ACM DNS validation — the cert will auto-issue once this
   propagates and AWS re-checks it, no further action needed after adding it):
   ```
   Name:  _a40be967512ef90673d6db6f9eba5c65.feedback.mschweier.com.
   Type:  CNAME
   Value: _75e784d25a4d25f60950899b31b74f5a.jkddzztszm.acm-validations.aws.
   ```
   Cert ARN: `arn:aws:acm:us-east-1:740353583786:certificate/1ef8d7cb-179f-439a-aa5e-3003957f59ea`
   (region **us-east-1**, required for CloudFront). Check status with:
   ```bash
   aws acm describe-certificate --certificate-arn arn:aws:acm:us-east-1:740353583786:certificate/1ef8d7cb-179f-439a-aa5e-3003957f59ea --query "Certificate.Status"
   ```
   Wait for `"ISSUED"` (minutes–hours, same caveat as the SES DKIM setup).
2. Once issued, re-deploy this stack with the cert ARN so CloudFront gets the alias:
   ```bash
   cd deploy/feedback && cdk deploy -c certificateArn=arn:aws:acm:us-east-1:740353583786:certificate/1ef8d7cb-179f-439a-aa5e-3003957f59ea
   ```
3. **Add this second CNAME** — the site's own alias, pointing at the CloudFront
   distribution already live at the default domain:
   ```
   Name:  feedback.mschweier.com.
   Type:  CNAME (or ALIAS/ANAME if the host supports apex-like aliasing)
   Value: d3b4f3ie7z7uiz.cloudfront.net.
   ```
4. Wait for propagation, then confirm `https://feedback.mschweier.com` serves the form.
5. **Flip `FEEDBACK_BASE_URL` from the CloudFront default domain to the real subdomain**
   in both send paths (see §4 below, step 5) — until this last step, the feedback links
   in real emails keep working, just via the uglier `https://d3b4f3ie7z7uiz.cloudfront.net`
   URL, which is a functional (if unpolished) link, not a broken one.

Until DNS is live, the site is reachable at the CloudFront default domain
(`https://d3b4f3ie7z7uiz.cloudfront.net`) — this is what the live send paths point at
today (see §4), so the feature is fully working end-to-end right now, just not yet on
its pretty final URL.

### 4. Wiring the signing secret into the other two stacks

**Status as of 2026-07-04: steps 1–4 below are done and live.** The feedback stack is
deployed, the secret is populated, both `deploy/managed-agent` and `deploy/subscribers`
are redeployed with the ARN, the live scheduled deployment's `initial_prompt` carries
`FEEDBACK_TOKEN_SECRET_ARN`/`FEEDBACK_BASE_URL` (pointing at the CloudFront default
domain), and the microVM image was rebuilt/pushed (version `7.0`) with the
`feedback_token` helper + updated `audio_email.py`. Live-validated end-to-end against
the real deployed secret/API/table (see the PR description for the validation
evidence). **Only step 5 (the DNS-dependent URL flip) remains, and it requires the
human** — see §3 above for the concrete DNS records.

The feedback link is **generated** in two other, independent deploy units
(`deploy/managed-agent/pipeline/audio_email.py` and
`deploy/subscribers/functions/welcome-send/handler.py`) and **validated** only here.
Because all three are deliberately independent deploy lifecycles (ADR-0012 §B — no
CDK cross-stack import couples them), wiring the ARN through is a **manual, ordered**
step, done once (and again on secret rotation):

**Deploy order (reference — already performed for the current secret/ARN):**

1. **Deploy this stack first** (`deploy/feedback`) and note its `FeedbackTokenSecretArn`
   output.
2. **Populate the secret** (step 2 above).
3. **Re-deploy `deploy/managed-agent/cdk`** with the ARN as context, so
   `MicroVmExecutionRole` gains the `ReadFeedbackTokenSecret` grant:
   ```bash
   cd deploy/managed-agent/cdk
   cdk deploy -c anthropicEnvironmentId=<real env id> \
              -c feedbackTokenSecretArn=<FeedbackTokenSecretArn-from-step-1>
   ```
   Then add `FEEDBACK_TOKEN_SECRET_ARN=<that ARN>` and
   `FEEDBACK_BASE_URL=<this stack's DistributionDomainName, https://...>` as `export`s
   in `deploy/managed-agent/deployment.json`'s `agent.initial_prompt`, alongside the
   existing `SUBSCRIBERS_TABLE_NAME`/`SUBSCRIBERS_API_BASE_URL`/`PIPELINE_TIMEZONE`
   exports, and apply it via the Deployments API — see `deploy/managed-agent/README.md`
   §6 for the exact create-new/archive-old mechanism (deployments turned out to be
   immutable; there is no in-place update).
4. **Re-deploy `deploy/subscribers`** with the ARN (and, once you have a validated
   feedback site URL, the base URL) as context, so `WelcomeSendFunctionRole` gains the
   same grant and the welcome-send Lambda gets its env vars:
   ```bash
   cd deploy/subscribers
   cdk deploy -c subscribeDomainName=briefing.mschweier.com \
              -c feedbackTokenSecretArn=<FeedbackTokenSecretArn-from-step-1> \
              -c feedbackBaseUrl=<this stack's DistributionDomainName, https://...>
   ```
5. **After DNS cutover (§3 above) — the one step still pending:** flip `feedbackBaseUrl` /
   `FEEDBACK_BASE_URL` from the CloudFront default domain to
   `https://feedback.mschweier.com` in both places above and re-deploy each — mirrors
   the PRD's phase-4 "point at the validated domain first, flip to the live subdomain
   only after DNS is confirmed" sequencing (`docs/prd/reader-feedback.md` §8).

(Before steps 3–4 are done, both send paths simply **omit** the feedback link — their
own fail-safe: missing config never blocks the brief/welcome send. That window has
already passed for the current secret/ARN; noted here for future rotations.)

## Testing the submit endpoint via curl (temporary `execute-api` URL)

Replace `$API` with the `HttpApiUrl` stack output.

```bash
API="https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com"

# a full, non-anonymous submission with a valid token (?t=... from a real feedback link)
curl -s -X POST "$API/submit" \
  -H "Content-Type: application/json" \
  -d '{"overallRating":5,"contentSelection":4,"additionalSources":"arXiv","otherFeedback":"Great!","t":"<token-from-a-real-link>"}'

# anonymous submission (checkbox checked) -- no identity persisted even with a valid token
curl -s -X POST "$API/submit" \
  -H "Content-Type: application/json" \
  -d '{"overallRating":3,"anonymous":true,"t":"<token-from-a-real-link>"}'

# walk-up, no token at all -- stored anonymous with no edition attribution
curl -s -X POST "$API/submit" \
  -H "Content-Type: application/json" \
  -d '{"overallRating":4}'

# honeypot filled -- looks like a normal 200, but no record is written
curl -s -X POST "$API/submit" \
  -H "Content-Type: application/json" \
  -d '{"overallRating":5,"website":"http://spam.example"}'

# out-of-range graded answer -- 400, no record
curl -s -X POST "$API/submit" \
  -H "Content-Type: application/json" \
  -d '{"overallRating":9}'
```

Inspect submissions directly while testing:

```bash
aws dynamodb scan --table-name brief-feedback --region us-east-1
```

## End-to-end validation loop (do this before going live)

Mirrors `deploy/subscribers/README.md`'s style for the existing subscribe surface:

1. Deploy this stack, populate the signing secret, `curl POST /submit` a few
   variations (above) → confirm each produces the expected record shape (or no record,
   for the honeypot case).
2. Wire the ARN into `deploy/managed-agent` (step 4 above), trigger a manual run →
   confirm the owner's and a test subscriber's emails each carry a working feedback
   link, and that clicking it (or curling `/submit?t=<that token>`) attributes the
   submission to the right identity + edition.
3. Wire the ARN into `deploy/subscribers` (step 4 above), confirm a new subscriber →
   confirm the welcome email carries a working feedback link attributed to them and the
   archived edition's date.
4. Force a tampered/malformed token → confirm the submission still succeeds but is
   stored anonymously, never with a forged identity.
5. Force a cold-start welcome email (no brief archived yet) → confirm it has **no**
   feedback link (no edition to attribute to).

## Teardown

```bash
cd deploy/feedback
cdk destroy
```

`cdk destroy` will **not** remove:

- The DynamoDB table and the signing secret — both have `RemovalPolicy.RETAIN`
  deliberately (avoid silently losing collected feedback or forcing every outstanding
  feedback link to invalidate on an accidental `cdk destroy`). Delete them manually if
  you really mean to tear the epic down entirely:
  ```bash
  aws dynamodb delete-table --table-name brief-feedback --region us-east-1
  aws secretsmanager delete-secret --secret-id daily-ai-brief/feedback-token-signing-secret --region us-east-1
  ```
- The S3 site bucket — also `RemovalPolicy.RETAIN`:
  ```bash
  aws s3 rb s3://<SiteBucketName> --force --region us-east-1
  ```
- Any DNS records added for `feedback.mschweier.com` or the ACM certificate — remove
  those through whatever DNS host manages `mschweier.com`, and
  `aws acm delete-certificate` for the cert if no longer needed.
- The `feedbackTokenSecretArn` context values passed to `deploy/managed-agent` and
  `deploy/subscribers` — those stacks' own grants/env vars are only removed by
  re-deploying them without that context.
- CloudFront distributions can take several minutes to fully disable/delete;
  `cdk destroy` will wait but budget time for it.

## Local validation without a real AWS deploy

```bash
cd deploy/feedback
.venv/bin/python3 -m py_compile app.py brief_feedback/*.py functions/*/handler.py functions/*/feedback_token.py
.venv/bin/cdk synth                 # requires Node.js + `npm install -g aws-cdk`
.venv/bin/python3 -m pytest tests/ -v
```
