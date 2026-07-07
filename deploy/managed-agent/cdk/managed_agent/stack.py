"""ManagedAgentSandboxStack — the self-hosted Claude Managed Agents sandbox on AWS
Lambda MicroVMs, adapted into CDK Python from AWS's reference implementation
(github.com/aws-samples/sample-lambda-microvm-claude-managed-agents).

See docs/adr/0004-aws-credentials-for-boto3-in-managed-agents-sandbox.md for why this
exists at all (self-hosted so the pipeline authenticates via a real IAM execution role,
not a static key), and docs/adr/0006-managed-agents-environment-and-scheduled-deployment.md
for this stack's exact shape. Every resource shape below (the `lambda:RunMicroVm` IAM
action name — note the lowercase "v", confirmed by the reference implementation's own
comment about the service's 403 behavior — the network-connector ARNs, the Secrets
Manager secret layout, the webhook API shape) mirrors the reference `template.yaml`
verbatim, ported from SAM/CloudFormation to CDK constructs.

What this stack does NOT do (deliberately, per the PRD/ADRs):
  - It does not build or push the microVM container image (deploy/managed-agent/microvm/
    is the image source; building/pushing it is an out-of-band CLI step, see README).
  - It does not create the Managed Agents `self_hosted` environment, the agent
    definition, or the scheduled deployment itself — those are Claude Console/API
    steps (README) and the `deploy/managed-agent/deployment.json` Deployments-API
    payload (ADR-0006), not CDK/CloudFormation resources.
  - It does not touch `deploy/subscribers/` or the live `cowork-polly-tts` IAM user/
    policy — this is new, additive infrastructure.
  - It does not populate the two Secrets Manager secrets with real values — they are
    created empty and populated out-of-band (README), consistent with "no secrets in
    git".
"""

import shutil
import subprocess
from pathlib import Path

import jsii
import aws_cdk as cdk
from aws_cdk import (
    AssetHashType,
    BundlingOptions,
    DockerImage,
    Duration,
    ILocalBundling,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_wafv2 as wafv2,
)
from constructs import Construct

LAUNCHER_DIR = Path(__file__).resolve().parent.parent.parent / "microvm" / "launcher"

# The launcher has third-party deps (anthropic, boto3/botocore — see
# deploy/managed-agent/microvm/launcher/requirements.txt) that must be installed
# alongside the source before zipping. CDK's standard pattern for this is asset
# bundling in a container image matching the Lambda runtime. This stack prefers
# LOCAL bundling (`pip install --target`, no Docker) when `pip` is on PATH, so
# `cdk synth`/`cdk deploy` both work in sandboxes without Docker (this repo's dev
# environment included) — falling back to Docker bundling
# (`public.ecr.aws/sam/build-python3.13`) automatically if local bundling is
# unavailable or fails.
#
# CONFIRMED LIVE BUG (2026-07-03), now fixed: a first real deploy from this macOS
# host used local bundling with a bare `pip install -t <dir>` (no --platform), which
# installs whatever wheels pip resolves for the HOST's platform (macOS arm64) — not
# the Lambda's actual runtime (Linux arm64/manylinux). anthropic's compiled
# dependencies (pydantic-core, a Rust extension) built for macOS silently fail to
# import inside the deployed Lambda; the launcher's own `except ImportError:
# anthropic = None` fallback (a deliberate fail-closed guard, see launcher.py)
# caught this and correctly rejected every webhook delivery as unverifiable rather
# than skipping verification — but the ROOT CAUSE was this packaging gap, not a bad
# signature. Fixed by pinning `--platform manylinux2014_aarch64 --python-version
# 3.13 --implementation cp --abi cp313 --only-binary=:all:` so pip downloads
# prebuilt Linux/aarch64 wheels for CPython 3.13 regardless of the host's actual
# platform — this works without Docker because it's a wheel *selection* constraint,
# not a build step; every dependency here (anthropic, httpx, pydantic, pydantic-core,
# boto3, botocore, and their transitive deps) publishes manylinux aarch64 wheels.
# Docker bundling (matching the exact Lambda execution environment) remains the
# stronger guarantee and is still the documented preference when available.
@jsii.implements(ILocalBundling)
class _LocalPipBundling:
    """Bundle by running a cross-platform `pip install -r requirements.txt -t <out>`
    on the host, forcing Linux/aarch64/cp313 wheels regardless of host platform.

    Must be @jsii.implements(ILocalBundling), not a plain Python subclass —
    ILocalBundling is a jsii Protocol; CDK's Node-side bundling logic calls
    tryBundle() across the jsii process boundary, which requires the jsii proxy
    machinery this decorator provides.
    """

    def try_bundle(self, output_dir: str, *, image=None, **_: object) -> bool:
        pip = shutil.which("pip3") or shutil.which("pip")
        if pip is None:
            return False
        requirements = (LAUNCHER_DIR / "requirements.txt").read_text().splitlines()
        # `standardwebhooks` ships no wheel on PyPI (sdist only), so it can't survive
        # --only-binary=:all: below. It's pure Python (no compiled extensions), so
        # install it separately, unlocked to the host platform — the resulting files
        # are byte-identical regardless of build host and run fine on Lambda's
        # Linux/aarch64. See the comment in requirements.txt for the full history.
        unlocked_pkgs = [
            line.strip() for line in requirements
            if line.strip().startswith("standardwebhooks")
        ]
        locked_lines = [
            line for line in requirements
            if line.strip() and not line.strip().startswith("#")
            and not line.strip().startswith("standardwebhooks")
        ]
        try:
            if locked_lines:
                subprocess.run(
                    [
                        pip, "install", "-q",
                        *[arg for line in locked_lines for arg in (line,)],
                        "-t", output_dir,
                        "--platform", "manylinux2014_aarch64",
                        "--implementation", "cp",
                        "--python-version", "3.13",
                        "--abi", "cp313",
                        "--only-binary=:all:",
                    ],
                    check=True,
                )
            if unlocked_pkgs:
                subprocess.run(
                    [pip, "install", "-q", "--no-deps", "-t", output_dir, *unlocked_pkgs],
                    check=True,
                )
        except subprocess.CalledProcessError:
            return False
        shutil.copytree(
            LAUNCHER_DIR,
            output_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("requirements.txt", "__pycache__", "*.pyc"),
        )
        return True


_LAUNCHER_BUNDLING = BundlingOptions(
    image=DockerImage.from_registry("public.ecr.aws/sam/build-python3.13:latest"),
    command=[
        "bash",
        "-c",
        "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
    ],
    local=_LocalPipBundling(),
)

# --- Fixed identifiers this stack reuses verbatim from other repo artifacts --------
#
# Kept as constants (not context) because they name resources this stack does NOT
# create and must never accidentally diverge from: the existing pipeline bucket
# (ADR-0005) and the existing subscriber table's GSI (deploy/iam-policy.json).
PIPELINE_BUCKET_NAME = "cowork-polly-tts-740353583786"
SUBSCRIBERS_TABLE_STATUS_INDEX_ARN_TEMPLATE = (
    "arn:aws:dynamodb:{region}:{account}:table/brief-subscribers/index/status-index"
)
# Single unified sender for both the owner's copy and the subscriber fan-out — see
# CLAUDE.md: "SES From must be exactly aibriefing@mschweier.com". mail@mschweier.com
# is the owner's RECIP (recipient) address only, never a From address; it is not an
# SES identity this role needs permission to send as.
SENDER = "aibriefing@mschweier.com"

# Kept short deliberately: it prefixes several resource names, including the
# image-artifact S3 bucket name below, which has a hard 63-character ceiling
# once the account id + region suffix are appended.
DEFAULT_PROJECT_NAME = "daily-brief-agent"
DEFAULT_IMAGE_IDENTIFIER = "claude-daily-brief-worker"

# AWS-managed network connector ARN templates (confirmed against the reference
# implementation's shared/constants.py — these are fixed AWS-owned ARNs per region,
# not resources this stack creates). RunMicroVm requires the caller to be allowed to
# pass these to the launched microVM so it gets default public ingress/egress
# (ADR-0006 "no VPC/NAT/allowlist required").
_ALL_INGRESS_ARN_TEMPLATE = (
    "arn:{partition}:lambda:{region}:aws:network-connector:aws-network-connector:ALL_INGRESS"
)
_INTERNET_EGRESS_ARN_TEMPLATE = (
    "arn:{partition}:lambda:{region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
)


class ManagedAgentSandboxStack(Stack):
    """One stack: webhook + launcher Lambda, both IAM roles, secrets, image-build bucket."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.project_name = self.node.try_get_context("projectName") or DEFAULT_PROJECT_NAME
        self.anthropic_environment_id = (
            self.node.try_get_context("anthropicEnvironmentId") or "env_PLACEHOLDER_SET_VIA_CONTEXT"
        )
        self.microvm_image_identifier = (
            self.node.try_get_context("microvmImageIdentifier") or DEFAULT_IMAGE_IDENTIFIER
        )
        # Optional, backward-compatible: the deploy/feedback/ stack's token-signing
        # secret ARN, supplied only once that stack has been deployed and output it
        # (docs/prd/reader-feedback.md, ADR-0011 "Owning stack" / ADR-0012 §B "Send-side
        # wiring"). Absent by default so this stack keeps synthesizing/deploying cleanly
        # before the feedback stack exists or before this context is supplied -- no
        # grant, no env var, when unset (see _build_microvm_execution_role() below).
        self.feedback_token_secret_arn = self.node.try_get_context("feedbackTokenSecretArn")

        self.environment_key_secret = self._build_environment_key_secret()
        self.signing_secret = self._build_signing_secret()

        self.image_artifact_bucket = self._build_image_artifact_bucket()
        self.microvm_build_role = self._build_image_build_role()

        self.microvm_execution_role = self._build_microvm_execution_role()

        self.idempotency_table = self._build_idempotency_table()

        self.launcher_fn = self._build_launcher_function()
        self.webhook_api = self._build_webhook_api()
        self._build_waf(self.webhook_api)

        cdk.CfnOutput(
            self,
            "WebhookUrl",
            value=f"{self.webhook_api.url}webhook",
            description="Register this URL as the Anthropic webhook endpoint (session.status_run_started).",
        )
        cdk.CfnOutput(
            self,
            "EnvironmentKeySecretArn",
            value=self.environment_key_secret.secret_arn,
            description="Populate out-of-band with the Anthropic environment key (see README) — never in CDK.",
        )
        cdk.CfnOutput(
            self,
            "SigningSecretArn",
            value=self.signing_secret.secret_arn,
            description="Populate out-of-band with the Anthropic webhook signing secret (whsec_...) — never in CDK.",
        )
        cdk.CfnOutput(
            self,
            "MicroVmExecutionRoleArn",
            value=self.microvm_execution_role.role_arn,
            description="Pipeline identity the microVM assumes via IMDSv2 (ADR-0004); no static AWS key involved.",
        )
        cdk.CfnOutput(
            self,
            "ImageArtifactBucketName",
            value=self.image_artifact_bucket.bucket_name,
            description="Upload the zipped microVM image source here before create-microvm-image (see README).",
        )
        cdk.CfnOutput(
            self,
            "MicroVmBuildRoleArn",
            value=self.microvm_build_role.role_arn,
            description="Pass to `aws lambda-microvms create-microvm-image --execution-role-arn` (build step, see README).",
        )

    # ------------------------------------------------------------------
    # Secrets — created empty; populated out-of-band (never in git/CDK)
    # ------------------------------------------------------------------
    def _build_environment_key_secret(self) -> secretsmanager.Secret:
        """The Anthropic environment key (self-hosted worker auth), read only by the
        microVM execution role (ADR-0004: "the only secrets... are the environment key
        and the webhook signing secret"). Created with no SecretString — CDK/CloudFormation
        cannot set a real value here without it landing in a template/state file, so this
        is intentionally empty; populate with `aws secretsmanager put-secret-value` after
        deploy (README)."""
        return secretsmanager.Secret(
            self,
            "EnvironmentKeySecret",
            secret_name=f"{self.project_name}/anthropic-environment-key",
            description="Anthropic environment key for the self_hosted Managed Agents environment. Populated out-of-band.",
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _build_signing_secret(self) -> secretsmanager.Secret:
        """The Anthropic webhook signing secret (whsec_...), read only by the launcher
        Lambda to verify inbound webhook deliveries. Created empty; populated out-of-band."""
        return secretsmanager.Secret(
            self,
            "SigningSecret",
            secret_name=f"{self.project_name}/anthropic-webhook-signing-secret",
            description="Anthropic webhook signing secret (whsec_...) for session.status_run_started deliveries. Populated out-of-band.",
            removal_policy=RemovalPolicy.RETAIN,
        )

    # ------------------------------------------------------------------
    # MicroVM image build support (a CLI step, not this stack's own deploy —
    # these resources just give that CLI step somewhere least-privilege to run)
    # ------------------------------------------------------------------
    def _build_image_artifact_bucket(self) -> s3.Bucket:
        """Distinct from the pipeline's existing `cowork-polly-tts-740353583786` bucket
        (PRD/ADR-0006: reuse existing resources, do not recreate). This bucket holds only
        the zipped microVM image source (app.zip) that `create-microvm-image` reads from —
        an integration concern of this sandbox, not a pipeline artifact."""
        bucket_name = f"{self.project_name}-image-artifacts-{self.account}-{self.region}"
        # S3 bucket names are capped at 63 characters (AWS hard limit) — fail loudly at
        # synth time if a longer `projectName` context override would violate that,
        # rather than surfacing as an opaque CloudFormation InvalidBucketNameValue error.
        if len(bucket_name) > 63:
            raise ValueError(
                f"projectName {self.project_name!r} produces an image-artifact bucket "
                f"name longer than the S3 63-character limit: {bucket_name!r} "
                f"({len(bucket_name)} chars). Shorten the projectName context value."
            )
        return s3.Bucket(
            self,
            "ImageArtifactBucket",
            bucket_name=bucket_name,
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _build_image_build_role(self) -> iam.Role:
        """Role passed to `create-microvm-image --execution-role-arn` (a build-time role,
        distinct from the runtime `microvm_execution_role` below). Scoped to read/write
        only this stack's own artifact bucket plus its own build logs — mirrors the
        reference implementation's `BuildRole` verbatim."""
        role = iam.Role(
            self,
            "MicroVmBuildRole",
            role_name=f"{self.project_name}-build-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege role for the microVM image build step (image-artifact bucket + its own logs only).",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ArtifactBucketAccess",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[
                    self.image_artifact_bucket.bucket_arn,
                    f"{self.image_artifact_bucket.bucket_arn}/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="BuildLogs",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group:/aws/lambda/microvms/{self.microvm_image_identifier}*",
                    f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group:/aws/lambda/microvms/{self.microvm_image_identifier}*:*",
                ],
            )
        )
        return role

    # ------------------------------------------------------------------
    # MicroVM execution role — the pipeline's runtime identity (ADR-0004/0005)
    # ------------------------------------------------------------------
    def _build_microvm_execution_role(self) -> iam.Role:
        """The role the microVM assumes at run time via IMDSv2 (ADR-0004, Option B — no
        static AWS key anywhere). Two categories of permission:

        1. What the reference implementation's own worker needs regardless of payload:
           read the environment-key secret (to authenticate to Anthropic's work queue)
           and write its own CloudWatch logs. Mirrors the reference `MicroVmExecutionRole`
           verbatim.
        2. What THIS pipeline needs to run inside the session — scoped **verbatim** to
           deploy/iam-policy.json's four Sids, plus the ADR-0005 `s3:ListBucket` addition
           (prefix-scoped to `briefs/*`). Nothing broader — in particular, only one SES
           sender (`aibriefing@mschweier.com`) is granted; `mail@mschweier.com` is the
           owner's recipient address, never a From address (CLAUDE.md).
        """
        role = iam.Role(
            self,
            "MicroVmExecutionRole",
            role_name=f"{self.project_name}-microvm-execution-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Least-privilege runtime identity for the microVM (IMDSv2, no static key). "
                "Scoped verbatim to deploy/iam-policy.json + ADR-0005 (s3:ListBucket) + the "
                "second SES sender (ADR-0004)."
            ),
        )

        # -- Reference-implementation baseline: read the environment key, write own logs --
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadEnvironmentKey",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.environment_key_secret.secret_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RuntimeLogs",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group:/aws/lambda/microvms/{self.microvm_image_identifier}*",
                    f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group:/aws/lambda/microvms/{self.microvm_image_identifier}*:*",
                ],
            )
        )

        # -- Delivery-decoupling (ADR-0015 D6, Option B) — ALWAYS granted ----------------
        # The decoupled delivery_client.py reads the POST /deliver bearer + the
        # recent-briefs signing key from Secrets Manager itself (via THIS role, exactly
        # like ReadEnvironmentKey above), rather than the launcher injecting their values
        # into the run payload — keeping the launcher's "references only, never values"
        # credential boundary intact. Scoped to exactly those two secret names (the `-*`
        # covers Secrets Manager's random ARN suffix). Granted unconditionally: the new
        # path needs them whether or not the OLD in-VM delivery grants below have been
        # stripped yet, and they are harmless to the current audio_email.py path (which
        # never reads them). These are AUTH-token reads, NOT delivery capability — no
        # Polly/S3/SES/DynamoDB — so FR-1 (no direct delivery IAM) holds once the strip
        # below is applied.
        for sid, secret_name in (
            ("ReadDeliveryBearerSecret", "daily-ai-brief/delivery-bearer-secret"),
            ("ReadRecentBriefsSigningSecret", "daily-ai-brief/recent-briefs-read-bearer-secret"),
        ):
            role.add_to_policy(
                iam.PolicyStatement(
                    sid=sid,
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        f"arn:{self.partition}:secretsmanager:{self.region}:{self.account}:secret:{secret_name}-*"
                    ],
                )
            )

        # -- OLD in-VM delivery grants — STRIPPED when `deliveryDecoupled` is set ---------
        # These are the delivery capability audio_email.py uses today (Polly/S3/SES/
        # DynamoDB + the feedback-token read). The ADR-0015 D1 strip removes them so the
        # content-generation MicroVM keeps only env-key + logs + the two AUTH-token reads
        # above. Gated behind the `deliveryDecoupled` CDK context flag (default OFF =
        # today's behavior): flip it ON **together with** the deployment.json swap to
        # delivery_client.py and the image rebuild (deploying the strip while audio_email.py
        # is still the live entrypoint would break the send — it would have no delivery
        # IAM). Scoped verbatim to deploy/iam-policy.json + ADR-0005.
        if not bool(self.node.try_get_context("deliveryDecoupled")):
            # Sid "PollySynthesis" (Resource "*" — Polly synthesis tasks have no
            # resource-level ARN to scope to; identical to the live cowork-polly-tts policy).
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="PollySynthesis",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "polly:StartSpeechSynthesisTask",
                        "polly:GetSpeechSynthesisTask",
                        "polly:ListSpeechSynthesisTasks",
                        "polly:SynthesizeSpeech",
                    ],
                    resources=["*"],
                )
            )

            # Sid "S3AudioReadWrite" plus the ADR-0005 s3:ListBucket addition,
            # prefix-scoped to `briefs/*`.
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="S3AudioReadWrite",
                    effect=iam.Effect.ALLOW,
                    actions=["s3:PutObject", "s3:GetObject"],
                    resources=[f"arn:{self.partition}:s3:::{PIPELINE_BUCKET_NAME}/*"],
                )
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="S3ListBriefsPrefix",
                    effect=iam.Effect.ALLOW,
                    actions=["s3:ListBucket"],
                    resources=[f"arn:{self.partition}:s3:::{PIPELINE_BUCKET_NAME}"],
                    conditions={"StringLike": {"s3:prefix": ["briefs/*"]}},
                )
            )

            # SES send, gated by ses:FromAddress — one sender, aibriefing@mschweier.com.
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="SesSendFromMschweier",
                    effect=iam.Effect.ALLOW,
                    actions=["ses:SendEmail", "ses:SendRawEmail"],
                    resources=["*"],
                    conditions={"StringEquals": {"ses:FromAddress": SENDER}},
                )
            )

            # Sid "DynamoDBSubscribersQuery" — Query only, on the status-index GSI.
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="DynamoDBSubscribersQuery",
                    effect=iam.Effect.ALLOW,
                    actions=["dynamodb:Query"],
                    resources=[
                        SUBSCRIBERS_TABLE_STATUS_INDEX_ARN_TEMPLATE.format(
                            region=self.region, account=self.account
                        )
                    ],
                )
            )

            # Sid "ReadFeedbackTokenSecret" — optional, only when the feedback stack's
            # secret ARN is supplied via the `feedbackTokenSecretArn` context value.
            if self.feedback_token_secret_arn:
                role.add_to_policy(
                    iam.PolicyStatement(
                        sid="ReadFeedbackTokenSecret",
                        effect=iam.Effect.ALLOW,
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[self.feedback_token_secret_arn],
                    )
                )

        return role

    # ------------------------------------------------------------------
    # Idempotency table — dedupes concurrent/retried webhook deliveries by
    # event_id, restoring the reference implementation's guard around
    # RunMicrovm (docs/adr/0010-restore-webhook-idempotency.md).
    # ------------------------------------------------------------------
    def _build_idempotency_table(self) -> dynamodb.Table:
        """Powertools Idempotency's DynamoDB schema: partition key ``id``, TTL
        attribute ``expiration``. Read/written only by the launcher Lambda via
        DynamoDBPersistenceLayer (launcher.py) — no other principal needs access.

        removal_policy=DESTROY deliberately breaks this stack's otherwise-uniform
        RETAIN convention (see the secrets/buckets above): this table holds only
        transient dedup state (each item self-expires via TTL well inside a day,
        ADR-0010's TTL decision), so losing it on stack teardown loses nothing of
        value — unlike the secrets or the image-artifact bucket, there is no
        out-of-band population step to redo.
        """
        return dynamodb.Table(
            self,
            "IdempotencyTable",
            table_name=f"{self.project_name}-idempotency",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expiration",
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )

    # ------------------------------------------------------------------
    # Launcher Lambda — webhook signature verification + RunMicroVm
    # ------------------------------------------------------------------
    def _build_launcher_function(self) -> _lambda.Function:
        role = iam.Role(
            self,
            "LauncherFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege role for the webhook launcher: RunMicroVm, PassRole to the microVM role, its own logs, read only the signing secret.",
        )
        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )

        # Read only the signing secret — never the environment key (ADR-0004: "the
        # launcher reads only the signing secret... and never handles the environment
        # key", mirroring the reference implementation's credential-boundary design).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadSigningSecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.signing_secret.secret_arn],
            )
        )

        # RunMicroVm itself. Confirmed against the reference implementation: the IAM
        # action is `lambda:RunMicroVm` (capital V) even though the API operation is
        # named `RunMicrovm` (lowercase v) — the reference repo's own comment notes this
        # was confirmed by the deployed service's 403 behavior. Resource "*" because
        # RunMicroVm has no resource-level ARN to scope to (matches the reference).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RunMicroVm",
                effect=iam.Effect.ALLOW,
                actions=["lambda:RunMicroVm"],
                resources=["*"],
            )
        )

        # PassRole for the microVM execution role. No iam:PassedToService condition on
        # purpose — the MicroVM service passes the role to a principal that is not
        # lambda.amazonaws.com, which that condition would incorrectly deny (mirrors the
        # reference implementation's own explanatory comment).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassMicroVmExecutionRole",
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.microvm_execution_role.role_arn],
            )
        )

        # RunMicroVm attaches the AWS-managed network connectors that give the microVM
        # its default public ingress/egress (ADR-0006: no VPC/NAT/allowlist needed) — the
        # caller must be allowed to pass them.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassNetworkConnectors",
                effect=iam.Effect.ALLOW,
                actions=["lambda:PassNetworkConnector"],
                resources=[
                    _ALL_INGRESS_ARN_TEMPLATE.format(partition=self.partition, region=self.region),
                    _INTERNET_EGRESS_ARN_TEMPLATE.format(partition=self.partition, region=self.region),
                ],
            )
        )

        # Idempotency store (ADR-0010): item-level access to the one idempotency
        # table only, no new principal/role. Powertools' DynamoDBPersistenceLayer
        # needs all four of these to manage the in-progress/complete record
        # lifecycle (write on launch start, read/update on completion or replay,
        # delete on validation failure).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="IdempotencyStore",
                effect=iam.Effect.ALLOW,
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[self.idempotency_table.table_arn],
            )
        )

        fn = _lambda.Function(
            self,
            "LauncherFunction",
            function_name=f"{self.project_name}-launcher",
            role=role,
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="launcher.handler",
            # asset_hash_type=OUTPUT: hash the bundling OUTPUT (post `pip install`),
            # not just the source directory. CDK's default (SOURCE) hashes only the
            # launcher's own .py files + requirements.txt -- a bundling-*logic* change
            # (e.g. the --platform flags added 2026-07-03, see _LocalPipBundling's
            # docstring) doesn't touch those files, so with the default hash type CDK
            # would treat the asset as unchanged and silently keep serving the
            # previously-published (and in that incident, wrong-platform) zip. This
            # was a real, live-observed bug -- OUTPUT hashing makes it structurally
            # impossible to repeat.
            code=_lambda.Code.from_asset(
                str(LAUNCHER_DIR), bundling=_LAUNCHER_BUNDLING, asset_hash_type=AssetHashType.OUTPUT
            ),
            timeout=Duration.seconds(30),
            # The launcher imports the anthropic SDK (+ pydantic/httpx) for webhook
            # verification; at the default 128 MB the cold-start import alone risks the
            # timeout (mirrors the reference implementation's own MemorySize comment).
            memory_size=1024,
            environment={
                "ANTHROPIC_ENVIRONMENT_ID": self.anthropic_environment_id,
                "MICROVM_IMAGE_IDENTIFIER": (
                    f"arn:{self.partition}:lambda:{self.region}:{self.account}:"
                    f"microvm-image:{self.microvm_image_identifier}"
                ),
                "ENVIRONMENT_KEY_SECRET_ARN": self.environment_key_secret.secret_arn,
                "MICROVM_EXECUTION_ROLE_ARN": self.microvm_execution_role.role_arn,
                "SIGNING_SECRET_ARN": self.signing_secret.secret_arn,
                "IDEMPOTENCY_TABLE": self.idempotency_table.table_name,
            },
        )
        return fn

    # ------------------------------------------------------------------
    # API — the public webhook front door: POST /webhook
    # ------------------------------------------------------------------
    def _build_webhook_api(self) -> apigw.RestApi:
        """A REST API (not HTTP API) to match the reference implementation, which relies
        on API Gateway request VALIDATION (structure only, not authentication — the
        launcher still HMAC-verifies the raw body) ahead of the launcher. AuthorizationType
        is NONE: a REQUEST authorizer never receives the raw body needed to verify the
        HMAC signature, so signature verification happens in-process in the launcher."""
        access_log_group = logs.LogGroup(
            self,
            "WebhookApiAccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        api = apigw.RestApi(
            self,
            "WebhookApi",
            rest_api_name=f"{self.project_name}-webhook-api",
            description="Receives Anthropic's session.status_run_started webhook for the self-hosted sandbox.",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                tracing_enabled=True,
                access_log_destination=apigw.LogGroupLogDestination(access_log_group),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
            ),
            endpoint_types=[apigw.EndpointType.REGIONAL],
        )

        webhook_resource = api.root.add_resource("webhook")
        webhook_resource.add_method(
            "POST",
            apigw.LambdaIntegration(self.launcher_fn),
            authorization_type=apigw.AuthorizationType.NONE,
        )

        return api

    # ------------------------------------------------------------------
    # WAF — defense-in-depth on the public webhook endpoint (not authentication)
    # ------------------------------------------------------------------
    def _build_waf(self, api: apigw.RestApi) -> wafv2.CfnWebACL:
        """Mirrors the reference implementation's WebACL: AWS managed rule groups +
        a per-IP rate limit on the webhook path. This is defense-in-depth, not
        authentication — the launcher's HMAC signature check remains the only thing that
        proves a request genuinely came from Anthropic."""
        web_acl = wafv2.CfnWebACL(
            self,
            "WebhookWebACL",
            name=f"{self.project_name}-webhook-web-acl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name=f"{self.project_name}-webhook-web-acl",
            ),
            rules=[
                self._managed_rule_group("AWSManagedRulesCommonRuleSet", priority=1),
                self._managed_rule_group("AWSManagedRulesKnownBadInputsRuleSet", priority=2),
                self._managed_rule_group("AWSManagedRulesSQLiRuleSet", priority=3),
                self._managed_rule_group("AWSManagedRulesAmazonIpReputationList", priority=4),
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitWebhookPerIP",
                    priority=5,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=100,
                            evaluation_window_sec=300,
                            aggregate_key_type="IP",
                            scope_down_statement=wafv2.CfnWebACL.StatementProperty(
                                byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                                    search_string="/webhook",
                                    field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(uri_path={}),
                                    positional_constraint="STARTS_WITH",
                                    text_transformations=[
                                        wafv2.CfnWebACL.TextTransformationProperty(priority=0, type="NONE")
                                    ],
                                )
                            ),
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        sampled_requests_enabled=True,
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{self.project_name}-webhook-rate-limit",
                    ),
                ),
            ],
        )

        wafv2.CfnWebACLAssociation(
            self,
            "WebhookWebACLAssociation",
            resource_arn=(
                f"arn:{self.partition}:apigateway:{self.region}::/restapis/"
                f"{api.rest_api_id}/stages/{api.deployment_stage.stage_name}"
            ),
            web_acl_arn=web_acl.attr_arn,
        )
        return web_acl

    @staticmethod
    def _managed_rule_group(name: str, *, priority: int) -> wafv2.CfnWebACL.RuleProperty:
        return wafv2.CfnWebACL.RuleProperty(
            name=name,
            priority=priority,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name=name,
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name=f"webhook-{name.lower()}",
            ),
        )
