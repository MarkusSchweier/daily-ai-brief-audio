# Production delivery cut-over runbook (ADR-0015)

Owner-gated steps to move the **live** weekday brief's delivery from the in-VM
`audio_email.py` path onto the `deploy/delivery/` boundary (full decouple). Do NOT run
these unattended — each changes the live subscriber-facing path. The build + all API
testing below the line are **already done and committed**; this file is the remaining
staged cut-over the owner executes when ready.

## Already done (delivery-side, safe, live)
- `POST /deliver` contract **v2** (four artifacts) + caller **idempotency key** — built,
  deployed, and **live-validated end to end** (owner-only send, fan-out OFF, sentinel
  date; real Polly `audio_ok`, all four artifacts archived, idempotent replay deduped
  with no second send). A latent self-invoke-ARN bug (slash vs. colon) was found by the
  first real trigger and fixed.
- `delivery_client.py` (the MicroVM-side API client) — built + unit-tested (16 tests),
  **not yet wired into the image** (`audio_email.py` is still the live path).
- The delivery bearer secret was rotated after testing.

## Remaining cut-over steps (owner-gated)

### 1. Populate the delivery stack's subscriber/feedback context
Today the delivery Lambda's `FEEDBACK_*` / `SUBSCRIBERS_*` env are **empty** (owner-only
tests didn't need them). For real subscriber sends to carry feedback + unsubscribe links,
redeploy the delivery stack with the same context the managed-agent stack uses:
```
cd deploy/delivery
cdk deploy BriefDeliveryStack --require-approval never \
  -c feedbackTokenSecretArn=<arn> -c feedbackBaseUrl=https://feedback.mschweier.com \
  -c subscribersApiBaseUrl=https://2il2bs0iq4.execute-api.us-east-1.amazonaws.com
```
(adds the `ReadFeedbackTokenSecret` grant; leaves everything else unchanged).

### 2. Launcher secret injection (D6) — the MicroVM must receive three values
`delivery_client.py` reads `DELIVERY_BASE_URL`, `DELIVERY_BEARER_TOKEN`, and (for step 0)
`RECENT_BRIEFS_TOKEN`. The launcher (`deploy/managed-agent/microvm/launcher/`) must inject
them into the run environment:
- `DELIVERY_BASE_URL = https://6nbe4wsng6.execute-api.us-east-1.amazonaws.com`
- `DELIVERY_BEARER_TOKEN` — read from Secrets Manager `daily-ai-brief/delivery-bearer-secret`
  (add a launcher IAM grant scoped to that secret ARN).
- `RECENT_BRIEFS_TOKEN` — a short-lived signed token minted per run from
  `daily-ai-brief/recent-briefs-read-bearer-secret` using the existing
  `recent_briefs_token.generate(...)` helper (same mechanism `deploy/candidates/trigger.py`
  already uses; add a launcher IAM grant to read that signing-key secret).
This is the same pattern the launcher already uses for the Anthropic environment key.

### 3. IAM strip (D1) — `MicroVmExecutionRole` → env-key + logs only
In `deploy/managed-agent/cdk/managed_agent/stack.py`'s `_build_microvm_execution_role()`,
**remove** the `PollySynthesis`, `S3AudioReadWrite`, `S3ListBriefsPrefix`,
`SesSendFromMschweier`, `DynamoDBSubscribersQuery`, and `ReadFeedbackTokenSecret`
statements. Keep only `ReadEnvironmentKey` + the CloudWatch Logs baseline. After this the
content-generation MicroVM has the **same zero-AWS-delivery posture as a cloud candidate**.
Recommend gating this behind a `deliveryDecoupled` CDK context flag (default off) so the
strip and the launcher injection (step 2) deploy together — deploying the strip alone
breaks the still-in-VM path. The delivery Lambda already holds these exact grants
(unchanged, no broader).

### 4. `deployment.json` swap (D3/D4) — entrypoint + artifact paths
- Step 0: `python3.13 /opt/pipeline/audio_email.py read-recent-briefs` →
  `python3.13 /opt/pipeline/delivery_client.py read-recent-briefs`.
- **Drop step 2** (the agent's ad-hoc Markdown→HTML) entirely — delivery derives HTML.
- Step 3: `python3.13 /opt/pipeline/audio_email.py` →
  `python3.13 /opt/pipeline/delivery_client.py`, exporting the four artifact paths the
  skill writes: `BRIEF_MARKDOWN_PATH` (today's dated brief), `LISTENING_SCRIPT_PATH`,
  `CANDIDATES_PATH=<WORKING_FOLDER>/candidates.json`,
  `SOURCE_USAGE_PATH=<WORKING_FOLDER>/source-usage.json`, plus `EMAIL_SUBJECT`,
  `PIPELINE_TIMEZONE`, and `ENABLE_SUBSCRIBER_FANOUT=1` (production). Remove the in-VM
  delivery env (`BRIEF_HTML_PATH`, `MP3_OUT_PATH`, `SUBSCRIBERS_TABLE_NAME`,
  `FEEDBACK_TOKEN_SECRET_ARN`, `FEEDBACK_BASE_URL`) — those now live on the delivery side.
- Re-push the deployment (Deployments API is immutable: create-new + archive-old).

### 5. Rebuild the microVM image (`delivery_client.py` must be in it)
`deploy/managed-agent/README.md` §5 (`update-microvm-image`). The launcher is unpinned, so
the new image auto-applies on the next run.

### 6. Staged validation (D10) — never a hard swap
1. Off-schedule: trigger a manual run with `ENABLE_SUBSCRIBER_FANOUT` **unset** → confirm
   the owner-only brief lands in `mail@mschweier.com` and the delivery API archived all
   four artifacts under the real date.
2. Confirm the rendered brief (new deterministic template) looks right in the real inbox.
3. Enable fan-out on the live schedule; keep the previous image version + deployment
   recoverable until a full weekday run is confirmed.
4. Verify `MicroVmExecutionRole` shows only `ReadEnvironmentKey` + logs (D1/AC-1).
