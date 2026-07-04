# Evaluation harness — CDK deploy & runbook

> Built 2026-07-04 per `docs/prd/eval-harness.md` and `docs/adr/0013` (backbone:
> build vs. adopt — Option A, custom AWS-native, human-approved). This is a **new,
> standalone** evaluation harness for the daily AI brief pipeline: measurement
> infrastructure only, never the live daily send. It shares **no** resource or IAM
> role with `deploy/subscribers/`, `deploy/feedback/`, or `deploy/managed-agent/` — it
> only **reads** `deploy/feedback/`'s `brief-feedback` table cross-stack, by ARN,
> same-account, and grants **no** write/delete on it and **no** SES anywhere.
> Everything here is provisioned by a single CDK stack, `BriefEvalStack`, in this
> directory (`deploy/eval/`).

## What CDK deploys (account `740353583786`, region `us-east-1`)

| Resource | Logical ID | Purpose |
|---|---|---|
| DynamoDB table | `EvalTable` (`brief-eval-records`) | PK `runId`; no sort key, no GSI. Durable, versioned per-run evaluation records (PRD FR-16/FR-17) |
| Secrets Manager secret | `ReviewSecret` (`daily-ai-brief/eval-review-bearer-secret`) | Shared reviewer bearer token gating the review UI + its write API (ADR-0013 §E) — **created empty**, populated out-of-band |
| Secrets Manager secret | `AnthropicApiKeySecret` (`daily-ai-brief/eval-anthropic-api-key`) | A **general** Anthropic API key for this stack's own use (Deployments/Sessions API calls to trigger+poll evaluations, Messages API for the judge LLM) — **created empty**, populated out-of-band. Deliberately **separate** from `deploy/managed-agent`'s `daily-brief-agent/anthropic-environment-key` secret — see `brief_eval/stack.py`'s `_build_anthropic_api_key_secret()` docstring for the full reasoning |
| Lambda | `TriggerFunction` (`brief-eval-trigger`) | `POST /trigger` — creates a temporary Deployments-API deployment targeting the current production agent/environment (with `SKIP_SUBSCRIBER_FANOUT=1` baked in), starts a session, records a `pending` row |
| Lambda | `PollFunction` (`brief-eval-poll`) | Invoked every 2 minutes by an EventBridge rule — checks in-progress sessions, and on completion fetches artifacts, runs the cost miner + v1 judges + calibration, writes the structured record, archives the temporary deployment |
| Lambda | `SubmitReviewFunction` (`brief-eval-submit-review`) | `POST /reviews` — persists a reviewer's per-criterion agree/override/comment |
| Lambda | `ReadFunction` (`brief-eval-read`) | `GET /runs`, `GET /runs/{runId}`, `GET /candidates` — the review site's read paths |
| HTTP API | `EvalHttpApi` | The four routes above; throttled stage; CORS locked to the eval site origin |
| EventBridge rule | `PollScheduleRule` | `rate(2 minutes)` — invokes `PollFunction` |
| S3 bucket | `EvalSiteBucket` | Private, OAC-only, hosts the static review site |
| CloudFront distribution | `EvalSiteDistribution` | Serves the site over HTTPS; its own distribution, optional custom domain + ACM cert |

One stack, one lifecycle — mirrors `deploy/subscribers/`/`deploy/feedback/`'s
single-stack shape. IAM is per-function, scoped by ARN, `sid=`-tagged — see
`tests/test_stack_iam.py` for the exact grants each role holds and does **not** hold.

## Prerequisites

- Node.js + npm (for the `aws-cdk` CLI). If missing: `brew install node && npm install -g aws-cdk`.
- Python 3.13 with a project-local virtualenv:
  ```bash
  cd deploy/eval
  python3 -m venv .venv
  .venv/bin/pip install -r requirements-dev.txt
  ```
- AWS credentials for account `740353583786` with permission to create the resources
  above. **Confirm the active AWS account before any deploy** (`/aws-account` /
  `aws-account-guard`). A separate deploy surface from every other stack's
  credentials/roles — never reuse those here.

## Context parameters

| Context key | Purpose | Default when unset |
|---|---|---|
| `evalDomainName` | The review site's own origin, used to lock down HTTP API CORS and (if `certificateArn` is also set) as the CloudFront alias | `eval.mschweier.com` (CORS only; no CloudFront alias) |
| `certificateArn` | An existing **us-east-1** ACM certificate ARN, validated for `evalDomainName` | unset — distribution serves on its default `*.cloudfront.net` domain only |
| `productionAgentId` | The SAME agent id `deploy/managed-agent/deployment.json`'s live scheduled deployment targets (PRD FR-1: reuse the established replay mechanism, never a second parallel pipeline) | `agent_PLACEHOLDER` — fine for `cdk synth`, a real trigger needs the real id |
| `productionEnvironmentId` | The SAME `self_hosted` environment id the live deployment targets | `env_PLACEHOLDER` |
| `feedbackTableArn` / `feedbackTableName` | The `deploy/feedback/` stack's `FeedbackTableArn`/`FeedbackTableName` outputs (PRD FR-15's read-only calibration join) | unset — no cross-stack grant, calibration reports insufficient data |

Pass via `-c key=value` on any `cdk` command, e.g.:

```bash
cdk deploy -c evalDomainName=eval.mschweier.com \
           -c productionAgentId=agent_01EswBTose8dnTAUDbGvzdLq \
           -c productionEnvironmentId=env_01ExWJJqFVT2f75H8BqcmKw8 \
           -c feedbackTableArn=arn:aws:dynamodb:us-east-1:740353583786:table/brief-feedback \
           -c feedbackTableName=brief-feedback
```

## Deploy

```bash
cd deploy/eval
source .venv/bin/activate   # or prefix commands with .venv/bin/
cdk bootstrap                # once per account/region, if not already done
cdk synth                    # static validation, no AWS calls
cdk diff                     # review what would change
cdk deploy -c productionAgentId=<real agent id> -c productionEnvironmentId=<real environment id>
```

Note the stack outputs after a successful deploy:

- `HttpApiUrl` — the temporary `execute-api` base URL.
- `DistributionDomainName` — the CloudFront `*.cloudfront.net` domain.
- `SiteBucketName` — the private S3 bucket the site assets are deployed into.
- `EvalTableName` / `EvalTableArn` — the durable evaluation-record store.
- `ReviewSecretArn` / `AnthropicApiKeySecretArn` — both need out-of-band population, below.

## Manual steps this stack does NOT do

### 1. Point the site's config at the real API URL

`deploy/eval/site/config.js` ships with a placeholder:

```js
window.BRIEF_EVAL_API_BASE_URL = "https://REPLACE-ME.execute-api.us-east-1.amazonaws.com";
```

After `cdk deploy`, replace the placeholder with the real `HttpApiUrl` output, then
re-run `cdk deploy` (the `BucketDeployment` construct re-syncs `site/` and invalidates
CloudFront automatically).

### 2. Populate the two secrets

Both are created **empty** — populate directly, never in git/CDK:

```bash
# The shared reviewer bearer token (ADR-0013 §E) -- give this to the one human reviewer.
aws secretsmanager put-secret-value \
  --secret-id <ReviewSecretArn-from-output> \
  --secret-string "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"

# A general Anthropic API key for this stack's own use.
aws secretsmanager put-secret-value \
  --secret-id <AnthropicApiKeySecretArn-from-output> \
  --secret-string "<your Anthropic API key>"
```

Give the reviewer the bearer token value once; they open the site at
`https://<DistributionDomainName-or-evalDomainName>/?k=<token>` the first time, which
persists it to `sessionStorage` for that browser tab session.

### 3. DNS for `eval.mschweier.com` — not yet done (human-only)

The registrar for `mschweier.com` is external; this sandbox has no DNS API access,
exactly as documented for `briefing.mschweier.com`/`feedback.mschweier.com`. Two
records are needed once `certificateArn` is requested and DNS-validated: the ACM
validation CNAME, and a site-alias CNAME to the CloudFront `*.cloudfront.net` domain.
Until then, the site is reachable at its CloudFront default domain, and CORS defaults
to the `evalDomainName` context value's origin regardless (set it to whatever host the
site is actually validated at — the CloudFront default domain during this phase).

### 4. Wiring `productionAgentId`/`productionEnvironmentId` — confirm against the live values

`deploy/managed-agent/deployment.json`'s `agent.agent_id` and
`environment.environment_id` are the real, current production values (see that file).
Pass them as context on every `deploy/eval` `cdk deploy` so the trigger Lambda targets
the SAME agent/environment the live scheduled send uses (PRD FR-1) — never invent a
second, parallel agent/environment for evaluation.

## Judgment calls flagged for the orchestrating session

- **Why a separate Anthropic API key secret, not a reuse of `deploy/managed-agent`'s
  environment key.** See `brief_eval/stack.py`'s `_build_anthropic_api_key_secret()`
  docstring. Short version: that secret is documented as the self-hosted
  environment's own worker-auth key, read only by the microVM execution role for one
  narrow purpose. This stack needs a general-purpose key usable for the Deployments/
  Sessions/Messages APIs from outside any session — a materially different shape and
  blast radius. Sharing would also couple this stack's key rotation/compromise
  response to the live production pipeline's own auth. Please double-check this
  reasoning before deploying, and confirm whether the Anthropic account actually
  supports/prefers a single general API key vs. per-purpose keys.
- **The on-demand (non-cron) Deployments-API shape used by the trigger Lambda**
  (`POST /v1/deployments` with no `schedule` field) was **not** independently
  re-verified against a live API call while building this — `deploy/managed-agent/README.md`
  §6 confirms the create-new-then-archive mechanism for a **scheduled** (cron)
  deployment; omitting `schedule` for a one-off eval trigger is inferred, not
  confirmed live. Please verify this against a real API call (or Anthropic's current
  docs) before relying on the trigger Lambda in production, and adjust
  `functions/trigger/handler.py`'s `_create_temporary_deployment()` if the real shape
  differs.
- **The Sessions/Threads API endpoint shapes** used by `eval_core/cost_miner.py`'s
  `fetch_session_cost()` (`GET /v1/sessions/{id}/threads`,
  `GET /v1/sessions/{id}/threads/{tid}`) and by `functions/poll/handler.py`'s session
  status check (`GET /v1/sessions/{id}`, tolerant of a few plausible terminal-status
  spellings) were similarly **not** independently re-verified against a live session —
  no live session existed to test against at build time. The cost-mining *logic*
  (phase splitting, pricing arithmetic) is fully unit-tested against a constructed
  fixture and is independent of the exact HTTP shape; only the thin HTTP-call wrapper
  functions would need adjusting if the real API differs.
- **The EventBridge poll rule runs unconditionally every 2 minutes**, not only while a
  run is pending — a small, constant, non-zero cost against PRD §2's "effectively $0
  when idle" framing (each invocation does one cheap DynamoDB Scan when there's
  nothing pending). Flagged as a minor, deliberate simplification over a
  self-disabling rule.
- **The FR-7 factual-accuracy judge checks plausibility/internal consistency, not
  literal fetched-source-traceability.** `eval_core/judges/accuracy.py`'s judge
  reads the brief's claims and judges whether they read like the kind of thing a
  web search would actually confirm (specific, plausible, appropriately hedged,
  internally consistent) — it does **not** itself re-fetch each cited source and
  verify the claim against it. This is a real, disclosed gap against FR-7's literal
  wording ("claims are traceable to a fetched source"), but the PRD explicitly
  allows **LLM-judge-only** treatment for this criterion (§4.B), so it is an
  accepted v1 scope decision, not an oversight.
- **`poll/handler.py` resolves "which run this poll cycle is processing" via "most
  recently created `briefs/<date>/` prefix"**, not an explicit date the trigger
  Lambda hands off. This assumes an evaluation run never coincides same-day with
  the live weekday production send (both archive under the same `briefs/<date>/`
  prefix in the shared pipeline bucket) — true today by convention (evaluations are
  deliberately triggered, not scheduled), but not structurally enforced. A more
  robust fix (the trigger Lambda recording an expected date, or a per-run S3 prefix)
  is straightforward but was judged non-trivial enough to defer; the code has an
  explicit comment flagging this assumption at the exact resolution point in
  `_process_completed_run()`.
- **Subscriber fan-out is now a fail-closed, opt-in gate** (security fix,
  2026-07-04): `deploy/managed-agent/pipeline/audio_email.py` requires an explicit
  `ENABLE_SUBSCRIBER_FANOUT=1` (only the live scheduled deployment's prompt sets
  this); everything else, including every evaluation run this stack triggers,
  defaults to skipping fan-out with zero cooperation required. `functions/trigger/
  handler.py` additionally rejects (400) any trigger request whose assembled prompt
  — including a caller-supplied `basePrompt` — contains the literal string
  `ENABLE_SUBSCRIBER_FANOUT` at all. See that pipeline file's module docstring and
  `_resolve_skip_subscriber_fanout()` for the full rationale.

## Testing the API via curl (temporary `execute-api` URL, with the reviewer bearer token)

```bash
API="https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com"
KEY="<the populated ReviewSecretArn's value>"

# Trigger an evaluation of the current production configuration.
curl -s -X POST "$API/trigger" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"candidateConfigId":"production"}'

# List all runs (or filter by status).
curl -s "$API/runs?status=pending" -H "Authorization: Bearer $KEY"

# Get one run's detail.
curl -s "$API/runs/<runId>" -H "Authorization: Bearer $KEY"

# Comparison/leaderboard data.
curl -s "$API/candidates" -H "Authorization: Bearer $KEY"

# Submit a reviewer override.
curl -s -X POST "$API/reviews" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"runId":"<runId>","criterion":"content_selection","agreed":false,"overriddenScore":3,"comment":"missed a story"}'

# No bearer token at all -- 401.
curl -s -i "$API/runs"
```

## End-to-end validation loop (do this before treating results as real)

1. Deploy this stack, populate both secrets, confirm `cdk synth`/`cdk deploy` succeed.
2. Trigger an evaluation of the **current production configuration** as the baseline
   (PRD §8 Phase 7) — confirm the live scheduled deployment's own schedule/output/send
   is completely unaffected (AC-1/AC-22), and that no email reaches a real subscriber
   (the `SKIP_SUBSCRIBER_FANOUT=1` export).
3. Wait for the poll Lambda to pick up the completed session (~2-10 minutes after the
   run finishes) and confirm a structured record appears via `GET /runs/{runId}`, with
   scores+rationale+evidence for all four v1 criteria, a cost breakdown, the brief
   markdown + listening script (AC-18), and (if `feedbackTableArn` is wired) a
   calibration section including any reader free-text feedback (FR-15).
4. Force a no-audio/no-candidates-artifact run (an older run, or before the live skill
   version push lands) and confirm content-selection degrades to "insufficient data"
   rather than erroring.
5. Submit a reviewer override via `/reviews` and confirm it persists into that run's
   `record.human_overrides` (the single source of truth both the read handler and the
   site's detail view read from -- there is no separate sibling attribute) and is
   reflected in the site's detail view.
6. Trigger 3 replicates of the same candidate and confirm the comparison view's
   aggregate reflects mean/variance across them (once `aggregate_replicates` output is
   surfaced — v1 ships the per-run records and comparison table; replicate
   aggregation is available as a library function, see `eval_core/record.py`).

## Teardown

```bash
cd deploy/eval
cdk destroy
```

`cdk destroy` will **not** remove:

- The DynamoDB table and both secrets — all have `RemovalPolicy.RETAIN` deliberately
  (this is real collected evaluation data). Delete manually if you really mean to tear
  this down entirely:
  ```bash
  aws dynamodb delete-table --table-name brief-eval-records --region us-east-1
  aws secretsmanager delete-secret --secret-id daily-ai-brief/eval-review-bearer-secret --force-delete-without-recovery
  aws secretsmanager delete-secret --secret-id daily-ai-brief/eval-anthropic-api-key --force-delete-without-recovery
  ```
- The S3 site bucket — also `RemovalPolicy.RETAIN`:
  ```bash
  aws s3 rb s3://<SiteBucketName> --force --region us-east-1
  ```
- Any DNS records or ACM certificate for `eval.mschweier.com` (none exist yet — see
  §3 above).
- Any temporary Deployments-API deployments the trigger Lambda created that were
  never archived (e.g. a stuck/failed poll cycle) — check
  `GET /v1/deployments?status=active` against the Deployments API directly and
  archive any orphaned eval deployments by hand if the poll Lambda's own archival
  step didn't run.

## Local validation without a real AWS deploy

```bash
cd deploy/eval
.venv/bin/python3 -m py_compile app.py brief_eval/*.py eval_core/*.py eval_core/judges/*.py functions/*/handler.py functions/*/review_auth.py
.venv/bin/cdk synth                 # requires Node.js + `npm install -g aws-cdk`
.venv/bin/python3 -m pytest tests/ -v
```
