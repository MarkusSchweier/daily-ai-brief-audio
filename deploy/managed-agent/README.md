# Self-hosted Claude Managed Agents sandbox — CDK deploy & runbook

> Built 2026-07-03 per `docs/prd/managed-agents-migration.md` and `docs/adr/0004`,
> `docs/adr/0005`, `docs/adr/0006`, `docs/adr/0007`. This is the **new, additive**
> self-hosted AWS infrastructure that lets the daily AI brief pipeline run inside Claude
> Managed Agents on AWS Lambda MicroVMs, instead of the owner's Mac-based Desktop
> scheduled task. It does **not** replace or touch `deploy/subscribers/` (the public
> subscribe surface, live and complete) or the local Desktop task, which stays running as
> a monitored fallback for the whole epic (PRD non-goal: do not retire it here).
>
> This adapts AWS's reference implementation
> (`github.com/aws-samples/sample-lambda-microvm-claude-managed-agents`, SAM/CloudFormation)
> into **AWS CDK (Python)**, per `docs/adr/0006`'s IaC decision, so it matches this repo's
> existing `deploy/subscribers/` convention (one CDK Python app per deploy surface).
> Every exact resource shape below (the `lambda:RunMicroVm` IAM action name, the
> network-connector ARNs, the Secrets Manager secret layout, the webhook API/WAF shape)
> was confirmed directly against that reference implementation's `template.yaml` and
> `launcher.py` at build time — see "What was and wasn't confirmed" below.

## What CDK deploys (account `740353583786`, region `us-east-1`)

One stack, `ManagedAgentSandboxStack`, in `deploy/managed-agent/cdk/`:

| Resource | Logical ID | Purpose |
|---|---|---|
| Secrets Manager secret | `EnvironmentKeySecret` | Anthropic environment key (worker auth) — **created empty**, populated out-of-band (step 4 below) |
| Secrets Manager secret | `SigningSecret` | Anthropic webhook signing secret (`whsec_...`) — **created empty**, populated out-of-band |
| S3 bucket | `ImageArtifactBucket` | Holds the zipped microVM image source (`app.zip`) for `create-microvm-image`; distinct from the pipeline's existing `cowork-polly-tts-740353583786` bucket, which this stack does **not** touch |
| IAM role | `MicroVmBuildRole` | Build-time role passed to `create-microvm-image`; scoped to the artifact bucket + its own logs only |
| IAM role | `MicroVmExecutionRole` | **The pipeline's runtime identity** — read the environment-key secret, write its own runtime logs, **plus** the full `deploy/iam-policy.json` permission set, verbatim (Polly synth; S3 rw on `cowork-polly-tts-740353583786/*`; `s3:ListBucket` on `briefs/*` per ADR-0005; SES send gated by `ses:FromAddress: aibriefing@mschweier.com` — the pipeline's single unified sender, per `CLAUDE.md`; `mail@mschweier.com` is the owner's recipient address only, never granted as a From address; DynamoDB `Query` on `brief-subscribers`'s `status-index` GSI). No static AWS key anywhere (ADR-0004) — assumed via IMDSv2 at microVM run time. |
| Lambda | `LauncherFunction` | Webhook handler: verifies the Anthropic signature, then calls `RunMicroVm` to boot a microVM per session |
| IAM role | `LauncherFunctionRole` | Exactly: `lambda:RunMicroVm`, `iam:PassRole` (scoped to `MicroVmExecutionRole` only), `lambda:PassNetworkConnector` (the two AWS-managed connector ARNs), read-only on the signing secret, CloudWatch Logs — nothing broader |
| REST API | `WebhookApi` | `POST /webhook` — the public endpoint registered with Anthropic for `session.status_run_started` |
| WAF WebACL | `WebhookWebACL` | Defense-in-depth on the public webhook (managed rule groups + per-IP rate limit) — **not** authentication; the launcher's HMAC signature check is the only thing that authenticates a delivery |

The microVM **container image itself** (`deploy/managed-agent/microvm/Dockerfile` +
`worker/worker.mjs`) is **not** built or pushed by this CDK stack — that is a separate CLI
step (step 5 below), matching the reference implementation's own separation of "control
plane" (CDK/SAM) from "image build" (CLI script).

## Prerequisites

- Node.js + npm (for the `aws-cdk` CLI — same reason as `deploy/subscribers/`, jsii shells
  out to Node). If missing: `brew install node && npm install -g aws-cdk`.
- Python 3.13 with a project-local virtualenv:
  ```bash
  cd deploy/managed-agent/cdk
  python3 -m venv .venv
  .venv/bin/pip install -r requirements-dev.txt
  ```
- **Docker**, for a real `cdk deploy` of the `LauncherFunction`. The launcher has
  third-party Python dependencies (`anthropic[webhooks]`, `boto3`/`botocore` — see
  `deploy/managed-agent/microvm/launcher/requirements.txt`) that must be installed
  alongside its source before zipping. The stack's asset bundling **prefers local `pip
  install` (no Docker)** so `cdk synth` works in a plain dev sandbox, but **falls back to
  Docker bundling** (`public.ecr.aws/sam/build-python3.13`) automatically if local
  bundling fails or `pip` isn't on `PATH`. For an actual deploy, having Docker available
  is the safer path — it builds the dependency wheels against the exact Lambda execution
  environment rather than whatever the host's `pip` happens to produce. If you don't have
  Docker, `brew install --cask docker` (or run in an environment that has it).
- AWS credentials for account `740353583786` with permission to create the resources
  above. **Confirm the active AWS account before any deploy** (`/aws-account` /
  `aws-account-guard` convention, this repo's global operating manual). This CDK app is a
  separate deploy surface from both `deploy/subscribers/`'s CDK app and the
  `cowork-polly-tts` IAM user — use whatever credentials/profile you deploy CDK stacks
  with, never the `cowork-polly-tts` static key.
- **"Claude Platform on AWS" must be stood up first** (AWS Marketplace subscription +
  IAM-federated console access, PRD FR-1/AC-1) — this is an Anthropic/AWS Marketplace
  step done through the AWS Console, not something this CDK stack or the `aws` CLI can do.
  Do this before step 3 below (creating the `self_hosted` environment needs it).

## Context parameters

| Context key | Purpose | Default when unset |
|---|---|---|
| `projectName` | Lower-case resource-name prefix (Lambda names, role names, the image-artifact bucket name). **Keep short** — the bucket name is `<projectName>-image-artifacts-<account>-<region>`, capped at AWS's 63-character S3 bucket-name limit; the stack raises a synth-time error if a longer value would violate it. | `daily-brief-agent` |
| `anthropicEnvironmentId` | The Claude self_hosted environment id (`env_...`) created in step 3 below. Required for the launcher to reference the right environment; a placeholder is fine for `cdk synth`, but a real deploy needs the real id. | empty placeholder |
| `microvmImageIdentifier` | Name of the microVM image built in step 5. Resolved to a full ARN (`arn:aws:lambda:<region>:<account>:microvm-image:<name>`) inside the stack. | `claude-daily-brief-worker` |

Pass via `-c key=value` on any `cdk` command, e.g.:

```bash
cdk deploy -c anthropicEnvironmentId=env_abc123
```

## Deploy

```bash
cd deploy/managed-agent/cdk
source .venv/bin/activate   # or prefix commands with .venv/bin/
cdk bootstrap                                   # once per account/region, if not already done
cdk synth -c anthropicEnvironmentId=env_abc123  # static validation, no AWS calls
cdk diff  -c anthropicEnvironmentId=env_abc123  # review what would change
cdk deploy -c anthropicEnvironmentId=env_abc123
```

Note the stack outputs after a successful deploy — you will need them for the manual
steps below:

- `WebhookUrl` — register this with Anthropic (step 2).
- `EnvironmentKeySecretArn` / `SigningSecretArn` — populate these (step 4).
- `MicroVmExecutionRoleArn` — the pipeline's runtime identity; also needed if you ever
  want to audit its attached policy directly.
- `ImageArtifactBucketName` / `MicroVmBuildRoleArn` — needed to build the microVM image
  (step 5).

## Manual steps this stack does NOT do

This is a **one IaC step plus several out-of-band steps** deploy, same shape as the
reference implementation. None of the following are CDK/CloudFormation resources —
they are Claude Console/API actions or CLI steps against the platform, done in this order:

### 1. Stand up "Claude Platform on AWS" (prerequisite, PRD FR-1/AC-1)

AWS Marketplace subscription + IAM-federated console access in account `740353583786`,
per Anthropic's own setup docs. Not automatable from this repo; confirms account
`740353583786` is the one you're standing this up in (same account this CDK stack
deploys into).

### 2. Deploy this CDK stack, then register the webhook

After `cdk deploy` succeeds, take the `WebhookUrl` output and register it in the
[Claude Console](https://platform.claude.com/settings/workspaces/default/webhooks) as a
webhook endpoint subscribed to `session.status_run_started`. The Console generates a
**webhook signing secret** (`whsec_...`) at this point — save it for step 4.

### 3. Create the `self_hosted` environment and the agent

Via the Claude Console or the Deployments API (with the beta header
`managed-agents-2026-04-01`, PRD FR-2):

- Create a `self_hosted` Managed Agents environment. Note its `env_...` id — this is the
  `anthropicEnvironmentId` context value for a re-deploy of this stack (the launcher
  needs it at runtime) and the `environment.environment_id` value in
  `deploy/managed-agent/deployment.json`.
- Generate the **environment key** for that environment (worker auth) — save it for
  step 4.
- Create the agent definition (the `agent.agent_id` in `deployment.json`), loading the
  ported research/writing skill (ADR-0007,
  `deploy/managed-agent/skills/daily-ai-brief/SKILL.md` — ported; see that file's own
  provenance note for exactly what it was reconstructed from).

### 4. Populate the two Secrets Manager secrets

Never put these values in code, CDK, or git — populate directly:

```bash
aws secretsmanager put-secret-value \
  --secret-id <EnvironmentKeySecretArn output> \
  --secret-string "<the environment key from step 3>"

aws secretsmanager put-secret-value \
  --secret-id <SigningSecretArn output> \
  --secret-string "<the webhook signing secret from step 2>"
```

Verify (values are redacted by AWS in the CLI's default output unless you explicitly
request them — do not paste secret values into logs or chat):

```bash
aws secretsmanager describe-secret --secret-id <EnvironmentKeySecretArn output>
aws secretsmanager describe-secret --secret-id <SigningSecretArn output>
```

### 5. Build and push the microVM container image

This stack provisions the `ImageArtifactBucket` and `MicroVmBuildRole` the build needs,
but does not run the build itself (matches the reference implementation's own separation
of concerns — `build-image.sh` is a CLI step, not a CloudFormation resource). The image
source is complete as of the ADR-0007 pipeline port: `microvm/Dockerfile` now copies in
the ported research/writing skill (`skills/daily-ai-brief/`) and the microVM-adapted
audio/email pipeline (`pipeline/`).

**AWS's `create-microvm-image` requires the `Dockerfile` at the root of the zipped
archive** (confirmed against the reference implementation's `build-image.sh`). Because
the ported skill and pipeline code live as siblings of `microvm/` in this repo — not
nested inside it, so they stay with the rest of `deploy/managed-agent/`'s
source-of-truth tree — the build **stages a temporary directory** (Dockerfile + worker/
from `microvm/`, plus `skills/` and `pipeline/` copied in alongside) before zipping,
rather than zipping `microvm/` in place:

```bash
cd deploy/managed-agent
STAGE=$(mktemp -d)
cp -R microvm/. "$STAGE"/
cp -R skills "$STAGE"/skills
cp -R pipeline "$STAGE"/pipeline
find "$STAGE" -name __pycache__ -exec rm -rf {} + 2>/dev/null
(cd "$STAGE" && zip -r -q /tmp/app.zip .)   # Dockerfile ends up at the archive root
aws s3 cp /tmp/app.zip s3://<ImageArtifactBucketName output>/app.zip

aws lambda-microvms create-microvm-image \
  --image-name claude-daily-brief-worker \
  --source-s3-bucket <ImageArtifactBucketName output> \
  --source-s3-key app.zip \
  --execution-role-arn <MicroVmBuildRoleArn output> \
  --enable-lifecycle-hooks
```

Monitor the build in CloudWatch under `/aws/lambda/microvms/claude-daily-brief-worker`;
the image transitions `IN_PROGRESS → SUCCESSFUL`. If you changed
`microvmImageIdentifier` from the default, use that name instead and re-deploy the CDK
stack first (the launcher's `MICROVM_IMAGE_IDENTIFIER` env var is derived from this
context value).

**Note on the `aws lambda-microvms` CLI/API:** AWS Lambda MicroVMs is a newer/beta
service surface. If your installed AWS CLI v2 doesn't recognize `lambda-microvms` yet,
you may need `aws configure add-model` with the service model, per the reference
implementation's own prerequisites list. Confirm this against current AWS CLI/SDK docs
at build time — this repo could not independently re-verify the CLI's current support
level (see "What was and wasn't confirmed" below).

### 6. Create/update the scheduled deployment

Using `deploy/managed-agent/deployment.json`'s `agent`, `environment`, and `schedule`
values (via the Deployments API or Console, beta header `managed-agents-2026-04-01`):
create a scheduled deployment targeting the `self_hosted` environment from step 3, with
`schedule.cron = "7 6 * * 1-5"` and `timezone: "America/Los_Angeles"` — matching today's
weekday 6:07 AM run. **Confirm the owner's actual local timezone before this step**; the
value in `deployment.json` is this repo's ADR assumption, not independently re-verified
against the owner's Mac locale settings.

### 7. Verify end-to-end

Mirrors the reference implementation's own `verify.py` operator flow and this repo's
`deploy/validation-handoff.md` style:

1. Trigger a session manually (via the Deployments API `POST .../sessions` or the
   Console's "run now") rather than waiting for the schedule.
2. Confirm the webhook fires: check `LauncherFunction`'s CloudWatch Logs for a
   `"launched microvm"` log line.
3. Confirm the microVM launched: `aws lambda-microvms list-microvms` /
   `get-microvm --microvm-id <id>`.
4. Confirm the microVM's IAM execution role resolves via IMDSv2 with a live boto3 call —
   this is the credential mechanism ADR-0004 depends on end-to-end, not just at the IAM
   policy level (PRD AC-13). The simplest check: have the ported pipeline's first tool
   call log `boto3.client("sts").get_caller_identity()`'s `Arn`, and confirm it resolves
   to `MicroVmExecutionRoleArn`, not an error.
5. Once the pipeline is ported (ADR-0007, separate task): confirm a full run produces
   the brief artifacts, the owner's unchanged copy arrives, and the subscriber fan-out
   works — this is the PRD's full AC-7 through AC-11 checklist, run against this
   infrastructure once the pipeline code exists.
6. **The real proof of the migration's purpose (PRD AC-2):** trigger the *scheduled* run
   (not a manual one) with the owner's Mac genuinely powered off or asleep, and confirm
   it still completes. This cannot be simulated — it must be tested for real.

## What was and wasn't confirmed

Per this task's instructions: `mcp__aws-docs` was **not available** in this session's
toolset. Research instead came from **live internet access** (`curl` against
`raw.githubusercontent.com`), reading AWS's own reference implementation
(`github.com/aws-samples/sample-lambda-microvm-claude-managed-agents`) directly — its
`template.yaml` (SAM/CloudFormation), `src/functions/launcher.py`, `src/functions/shared/`,
and `src/microvm-image/` (`Dockerfile`, `worker/worker.mjs`, `worker/package.json`) were
all read in full and ported/adapted faithfully into this stack, not invented from memory.
This is the same confidence level the ADRs describe as "confirmed with the human this
session" (ADR-0004/0006's verification notes) — concretely:

- **Confirmed directly from the reference implementation's source** (not memory): the IAM
  action `lambda:RunMicroVm` (capital V; the reference's own comment notes this was
  confirmed against the deployed service's 403 behavior even though the API operation is
  `RunMicrovm`, lowercase v); the `lambda:PassNetworkConnector` action and its two
  AWS-managed connector ARN templates (`ALL_INGRESS`, `INTERNET_EGRESS`); the Secrets
  Manager secret-per-purpose split (environment key vs. signing secret) and which role
  reads which; the `RunMicrovm` request field names (`imageIdentifier`,
  `runHookPayload`, `maximumDurationInSeconds`, `executionRoleArn`, `idlePolicy`,
  `logging`, `ingressNetworkConnectors`, `egressNetworkConnectors`); the webhook's
  request-validation + in-process HMAC-signature-verification pattern (`AuthorizationType:
  NONE` is correct here because a REQUEST authorizer never receives the raw body needed
  for HMAC verification); the microVM lifecycle-hook HTTP contract
  (`/aws/lambda-microvms/runtime/v1/{ready,validate,run,resume,suspend,terminate}`).
- **NOT independently re-verified against live AWS Lambda MicroVMs docs or the AWS CLI's
  current `lambda-microvms` service-model support** (no `mcp__aws-docs` access, and this
  task did not attempt an actual `create-microvm-image`/`run-microvm` API call — that
  requires the platform setup steps above to exist first). If the beta API surface has
  moved since the reference implementation was last updated, `cdk synth` will still
  succeed (it doesn't call these AWS APIs), but a real `cdk deploy` + image build could
  surface a mismatch. Re-confirm against current AWS docs (or `mcp__aws-docs` if
  available in a future session) before or during step 5 above.
- **This repo's own port deliberately drops** the reference implementation's DynamoDB-backed
  webhook-idempotency table (see the docstring in
  `deploy/managed-agent/microvm/launcher/launcher.py` for the rationale — this pipeline
  fires one scheduled session per weekday, so webhook volume is negligible and a
  double-launch is observable/annoying, not unsafe). If double-launches are ever actually
  observed in practice, restore that table from the reference implementation's
  `IdempotencyTable` resource as a follow-up hardening step.
- The **CDK-vs-SAM port itself** (translating `template.yaml`'s CloudFormation resources
  into `aws_cdk` Python constructs) was done by hand against the confirmed source above,
  then validated with `cdk synth` (see "Local validation" below) — this catches
  CloudFormation-shape errors (e.g. a bucket-name length violation was caught and fixed
  this way during the build) but does **not** prove the deployed stack behaves correctly
  against the live `lambda-microvms` service; that is only provable by an actual deploy
  and the step 7 verification above.

## Manual/out-of-band steps summary (what the human must still do)

In order, before this can run a real scheduled brief:

1. AWS Marketplace subscription + IAM-federated console access for "Claude Platform on
   AWS" in account `740353583786` (Claude Console/AWS Console, not this repo).
2. `cdk deploy` this stack (gated — requires human confirmation per this repo's DevOps
   conventions; **not run automatically by this build**). Also create the **`briefs/`
   lifecycle rule** on the existing `cowork-polly-tts-740353583786` bucket (ADR-0005,
   90-day expiry) — an imperative step, not a CDK/CloudFormation resource, mirroring how
   the existing `audio/` 7-day rule was created.
3. Register the webhook URL with Anthropic; create the `self_hosted` environment and
   generate the environment key; create the agent definition (Claude Console/API),
   loading the now-ported research/writing skill
   (`deploy/managed-agent/skills/daily-ai-brief/SKILL.md`, ADR-0007).
4. Populate the two Secrets Manager secrets with the real environment key and webhook
   signing secret (`aws secretsmanager put-secret-value`, never in git).
5. Build and push the microVM container image (`create-microvm-image`, step 5 above) —
   the image source is complete (skill + pipeline code both ported and wired into
   `microvm/Dockerfile`).
6. Create the scheduled deployment via the Deployments API/Console using
   `deploy/managed-agent/deployment.json`'s values, after confirming the owner's real
   local timezone.
7. Run the end-to-end verification (step 7 above), **including the Mac-off scheduled-run
   test (PRD AC-2)** — the entire point of this migration.

## Teardown

```bash
cd deploy/managed-agent/cdk
cdk destroy -c anthropicEnvironmentId=env_abc123
```

`cdk destroy` will **not** remove:

- The two Secrets Manager secrets and the `ImageArtifactBucket` — all have
  `RemovalPolicy.RETAIN` deliberately (avoid silently losing a populated secret or the
  built image source on an accidental `cdk destroy`). Delete manually if you really mean
  to tear this down entirely:
  ```bash
  aws secretsmanager delete-secret --secret-id <EnvironmentKeySecretArn> --force-delete-without-recovery
  aws secretsmanager delete-secret --secret-id <SigningSecretArn> --force-delete-without-recovery
  aws s3 rb s3://<ImageArtifactBucketName> --force
  ```
- The built microVM image itself (`aws lambda-microvms delete-microvm-image`) — not a
  CloudFormation resource.
- The webhook registration, the `self_hosted` environment, the agent definition, or the
  scheduled deployment — all Claude Console/API state, not AWS resources this stack owns.
- The existing `cowork-polly-tts-740353583786` bucket, the SES identity/senders, or the
  `brief-subscribers` DynamoDB table — this stack never created them and never touches
  them (PRD AC-12).

## Local validation without a real AWS deploy

```bash
cd deploy/managed-agent/cdk
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m py_compile app.py managed_agent/*.py
source .venv/bin/activate && cdk synth -c anthropicEnvironmentId=env_test_placeholder   # requires Node.js + `npm install -g aws-cdk`; no Docker required for synth alone

cd ../microvm/launcher
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python3 -m py_compile launcher.py shared/*.py
.venv/bin/python3 -m pytest tests/ -v

cd ../..
python3 -m json.tool deployment.json > /dev/null && echo "deployment.json OK"

# Pipeline code (ADR-0007 port): S3 brief-history persistence + the microVM-adapted
# audio_email.py fan-out logic, unit-tested against moto (no real AWS calls).
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m py_compile pipeline/*.py
.venv/bin/python3 -m pytest tests/ -v
```
