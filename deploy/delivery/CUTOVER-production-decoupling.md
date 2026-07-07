# Production delivery cut-over runbook (ADR-0015, Option B)

Owner-gated steps to move the **live** weekday brief's delivery from the in-VM
`audio_email.py` path onto the `deploy/delivery/` boundary (full decouple). **Option B**
(owner decision 2026-07-07): the MicroVM reads the delivery bearer + recent-briefs signing
key via its own ARN-scoped role grants and mints the read token itself — **no launcher
change**, no secret value in the run payload. Do NOT run the live-flip steps unattended.

> ## ✅ EXECUTED — 2026-07-07 (owner-approved)
> This cut-over is **done and live**. Phase A (owner-only validation on the stripped role) passed —
> all four artifacts archived under `briefs/2026-07-07/`, `SUBSCRIBER_FANOUT_SKIPPED`,
> `DELIVERY_SUCCEEDED`. Phase B was then executed: the scheduled weekday deployment was swapped to the
> decoupled prompt + `ENABLE_SUBSCRIBER_FANOUT=1` (new **`depl_01VP2gkocBheZF9dybQQ8aUN`**, cron
> `7 6 * * 1-5` Europe/Berlin; old **`depl_01GfuYeqwuDJ3q968CpTUUDe`** archived), `deliveryDecoupled=true`
> was flipped (the IAM strip — `MicroVmExecutionRole` now holds env-key + logs + the two delivery-secret
> reads only), and the welcome-send Lambda was deployed. The **first real subscriber send via the new
> path is Wed 2026-07-08 06:07 Europe/Berlin.** The steps below are retained as the record of what was
> done and as the rollback reference. **Watch that first weekday run** and roll back (see bottom) if it
> regresses.

## Already done + staged (safe; production unchanged)
- **Delivery boundary** (`deploy/delivery/`): contract v2, idempotency, reviewer+security
  passes (PR #34) — deployed, live-validated (owner-only, fan-out off).
- **`delivery_client.py`** (MicroVM-side client, Option B): reads the bearer + mints the
  recent-briefs token from Secrets Manager; fails loud (D8). **Baked into microVM image
  v13** (`audio_email.py`/skill unchanged, so the scheduled run is unaffected).
- **`MicroVmExecutionRole`**: the two ARN-scoped secret-read grants
  (`ReadDeliveryBearerSecret`, `ReadRecentBriefsSigningSecret`) are **deployed** (flag OFF,
  so the legacy delivery grants are still present). Both secrets are populated.
- **`deployment-validation.json`**: the new decoupled `initial_prompt`, fan-out **OFF**.
- **HTML template refinements** (delivery-side `derive_html` + `_html_with_header`, FR-2a): responsive
  card + 17px body (mobile), a single unified 14px top box carrying feedback + subscribe + unsubscribe
  + disclaimer (unsubscribe moved into the top box; footer removed), and the welcome mail
  (`deploy/subscribers/welcome-send`) now renders exactly like a daily subscriber email + a welcome
  intro. **Delivery stack redeployed with the new template**; validated by real emails to the owner.
  The **welcome-send Lambda change is committed but NOT deployed** (subscribers stack — see Phase B).
- **NOT done (the live flip):** the `deliveryDecoupled` IAM strip, swapping the *scheduled*
  deployment to the new prompt, enabling fan-out, and deploying the welcome-send Lambda.

## Phase A — Validation run (owner-only; the next joint step, after the HTML changes)
1. Create a **separate, on-demand** deployment from `deploy/managed-agent/deployment-validation.json`
   (`POST /v1/deployments`, same agent + self_hosted environment, **no production cron** — it must
   never compete with the live 06:07 scheduled deployment `depl_01GfuYeqwuDJ3q968CpTUUDe`).
2. Trigger one run. It uses image v13 → `delivery_client.py` → `POST /deliver`. Fan-out is OFF,
   so only the owner's copy (`mail@mschweier.com`) is sent.
3. Confirm: the brief lands in the owner's inbox (new deterministic HTML template + the HTML
   changes), all four artifacts archived under `briefs/<date>/`, and the run exited cleanly.
   Nobody else is emailed (`subscriber_sent_count=0`).
4. Archive the validation deployment when done.

## Phase B — Production cut-over (only after Phase A passes)
Do these **together** (deploying either half alone is unsafe — see ADR-0015 D1):
1. **Swap the scheduled deployment** to the new prompt: create a new deployment with
   `deployment-validation.json`'s `initial_prompt` **plus `ENABLE_SUBSCRIBER_FANOUT=1`**, the
   production cron (`7 6 * * 1-5`, Europe/Berlin), and archive the old one (Deployments API is
   immutable). Do **not** print the export block into logs (no `set -x`/`env` dump — SEC LOW-1).
2. **Flip the IAM strip:** redeploy the managed-agent CDK with `-c deliveryDecoupled=true`
   (`cd deploy/managed-agent/cdk && cdk deploy ManagedAgentSandboxStack --require-approval never
   -c deliveryDecoupled=true`). This removes Polly/S3/SES/DynamoDB from `MicroVmExecutionRole`,
   leaving env-key + logs + the two auth reads (FR-1). No image rebuild needed (v13 already has
   the client).
3. Optionally populate the delivery stack's subscriber/feedback context so real subscriber sends
   carry feedback + unsubscribe links (`cd deploy/delivery && cdk deploy BriefDeliveryStack
   -c feedbackTokenSecretArn=<arn> -c feedbackBaseUrl=https://feedback.mschweier.com
   -c subscribersApiBaseUrl=https://2il2bs0iq4.execute-api.us-east-1.amazonaws.com`).
3b. **Deploy the welcome-send Lambda** (`cd deploy/subscribers && cdk deploy BriefSubscribersStack
   --require-approval never`) so a newly-confirmed subscriber's welcome email matches the new daily
   chrome (top-box unsubscribe, unified font). Safe to do independently/earlier if you want to
   live-check it with a real subscribe→confirm; it wraps the latest archived brief (old-format until
   the cut-over, via a slot fallback) either way.
4. Confirm the next real weekday run went out via the delivery boundary; keep image v12 and the
   old scheduled deployment recoverable until a full weekday run is confirmed. Verify
   `MicroVmExecutionRole` shows only env-key + logs + the two auth reads (AC-1).

## Rollback
Re-point the scheduled deployment at the old `audio_email.py` prompt and redeploy the CDK
**without** `-c deliveryDecoupled=true` — the legacy delivery grants and in-VM path return. v12/v13
both contain `audio_email.py`, so no image rebuild is needed to roll back.
