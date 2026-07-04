"""BriefEvalStack — the evaluation harness (PRD docs/prd/eval-harness.md, ADR-0013
Option A). A standalone CDK app, sibling to `deploy/subscribers/` and
`deploy/feedback/`: its own DynamoDB table(s), its own Secrets Manager secrets, its own
Lambdas + least-privilege roles, its own HTTP API, its own private-bucket + OAC
CloudFront static review site. Shares NO resource or IAM role with those two stacks or
with `deploy/managed-agent/` -- it only READS the `brief-feedback` table cross-stack,
by ARN, same-account (ADR-0013 §Alternatives, mirroring how the welcome-send role reads
`cowork-polly-tts` cross-stack in ADR-0009), and never gains SES rights anywhere (the
harness never emails anyone, PRD §4.F).

What this stack does NOT do (deliberately, per the PRD/ADR):
  - It does not build or push a new daily-ai-brief skill version -- that's ADR-0008's
    separate, manual, confirmed procedure (deploy/managed-agent/README.md §3a).
  - It does not modify the live scheduled deployment, its schedule, or its fan-out.
  - It does not send email to anyone -- no SES permission exists on any role here.
  - It does not attempt DNS/cert setup for `evalDomainName` -- that is a human-only
    manual follow-up, exactly like `briefing.mschweier.com`/`feedback.mschweier.com`.
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
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"
SITE_DIR = Path(__file__).resolve().parent.parent / "site"

# --- Lambda asset bundling for functions with third-party deps (httpx/anthropic) ----
#
# `trigger/handler.py` imports `httpx`; `poll/handler.py` imports `httpx` AND
# `anthropic` (the judge LLM's Messages API client) -- neither is in the Python 3.13
# Lambda runtime, so a plain `Code.from_asset(<dir>)` on the raw handler directory
# (no bundling) leaves both `ImportError`ing at cold start. This reuses the exact,
# proven bundling pattern from `deploy/managed-agent/cdk/managed_agent/stack.py`'s
# `_LocalPipBundling` (confirmed live 2026-07-03 to be the fix for a real
# wrong-platform-wheel bug): install into the output dir with
# `--platform manylinux2014_aarch64 --implementation cp --python-version 3.13
# --abi cp313 --only-binary=:all:` so pip resolves prebuilt Linux/aarch64 CPython
# 3.13 wheels regardless of the build host's actual platform, no Docker required.
#
# Unlike the launcher's `standardwebhooks` situation (a pure-Python, sdist-only
# dependency that can't survive `--only-binary=:all:`), every dependency `httpx` and
# `anthropic` pull in here (httpcore, certifi, idna, sniffio, anyio, h11, pydantic,
# pydantic-core, jiter, distro, annotated-types, docstring-parser,
# typing-extensions) publishes a real manylinux aarch64 wheel -- confirmed by
# resolving each function's requirements.txt under this exact platform lock before
# wiring this in. So there is no two-pass unlocked/locked split needed here: one
# single locked `pip install` per function is sufficient.
@jsii.implements(ILocalBundling)
class _LocalPipBundling:
    """Bundle by running a cross-platform `pip install -r requirements.txt -t <out>`
    on the host, forcing Linux/aarch64/cp313 wheels regardless of host platform, for
    ONE function directory (`handler_dir`) at a time -- unlike the managed-agent
    stack's launcher (a single fixed directory), this stack has two distinct
    function directories (`trigger/`, `poll/`) each with their own requirements.txt,
    so this class is parameterized by directory rather than hard-coded to one."""

    def __init__(self, handler_dir: Path) -> None:
        self._handler_dir = handler_dir

    def try_bundle(self, output_dir: str, *, image=None, **_: object) -> bool:
        pip = shutil.which("pip3") or shutil.which("pip")
        if pip is None:
            return False
        requirements_path = self._handler_dir / "requirements.txt"
        requirements = [
            line.strip()
            for line in requirements_path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        try:
            if requirements:
                subprocess.run(
                    [
                        pip, "install", "-q",
                        *requirements,
                        "-t", output_dir,
                        "--platform", "manylinux2014_aarch64",
                        "--implementation", "cp",
                        "--python-version", "3.13",
                        "--abi", "cp313",
                        "--only-binary=:all:",
                    ],
                    check=True,
                )
        except subprocess.CalledProcessError:
            return False
        shutil.copytree(
            self._handler_dir,
            output_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("requirements.txt", "__pycache__", "*.pyc"),
        )
        return True


def _bundled_function_code(handler_dir: Path) -> _lambda.Code:
    """`Code.from_asset()` for a function directory that has its own
    `requirements.txt` -- Docker bundling preferred (matches the exact Lambda
    execution environment), falling back to local bundling
    (`_LocalPipBundling`) when Docker is unavailable, mirroring
    `deploy/managed-agent/cdk/managed_agent/stack.py`'s `_LAUNCHER_BUNDLING`
    exactly. `asset_hash_type=OUTPUT` so a bundling-*logic*-only change (not a
    source-file change) still gets redeployed -- see that stack's own comment on
    why `SOURCE` hashing silently missed a real wrong-platform-wheel bug."""
    bundling = BundlingOptions(
        image=DockerImage.from_registry("public.ecr.aws/sam/build-python3.13:latest"),
        command=[
            "bash",
            "-c",
            "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
        ],
        local=_LocalPipBundling(handler_dir),
    )
    return _lambda.Code.from_asset(str(handler_dir), bundling=bundling, asset_hash_type=AssetHashType.OUTPUT)

# Fallback CORS/site origin used only when no `evalDomainName` context is supplied (e.g.
# a bare `cdk synth` before DNS is decided) -- same convention as the sibling stacks'
# DEFAULT_*_DOMAIN constants.
DEFAULT_EVAL_DOMAIN = "eval.mschweier.com"

# The existing pipeline bucket (NOT owned by this stack) that holds the archived brief
# artifacts (briefs/<date>/brief.md, .html, candidates.json) this stack's poll/process
# Lambda reads -- read-only, scoped to the briefs/* prefix only (this stack has no
# business with audio/*, unlike the welcome-send Lambda).
PIPELINE_BUCKET_NAME = "cowork-polly-tts-740353583786"

REVIEW_SECRET_NAME = "daily-ai-brief/eval-review-bearer-secret"
ANTHROPIC_API_KEY_SECRET_NAME = "daily-ai-brief/eval-anthropic-api-key"


class BriefEvalStack(Stack):
    """One stack: two DynamoDB tables' worth of concerns (one eval-records table),
    two Secrets Manager secrets, four Lambdas + roles, an EventBridge poll rule, an
    HTTP API, and the static review site."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.eval_domain_name = self.node.try_get_context("evalDomainName")
        self.certificate_arn = self.node.try_get_context("certificateArn")
        # Optional, backward-compatible cross-stack read grant (PRD FR-15/FR-21):
        # supplied only once deploy/feedback/ has been deployed and its table exists.
        # Absent by default so this stack keeps synthesizing/deploying cleanly before
        # that stack exists -- no grant, no env var, when unset. Mirrors the exact
        # backward-compatible-context pattern `feedbackTokenSecretArn` already
        # established in deploy/subscribers/ and deploy/managed-agent/.
        self.feedback_table_arn = self.node.try_get_context("feedbackTableArn")
        self.feedback_table_name = self.node.try_get_context("feedbackTableName")
        # The current production agent/environment this stack targets for evaluation
        # runs (PRD FR-1: "the same replay/temporary-deployment mechanism already
        # established", i.e. the SAME agent+environment the live deployment uses --
        # never a second, parallel pipeline). Optional placeholders so `cdk synth`
        # succeeds before these are known; a real deploy should pass the real values.
        self.production_agent_id = self.node.try_get_context("productionAgentId") or "agent_PLACEHOLDER"
        self.production_environment_id = self.node.try_get_context("productionEnvironmentId") or "env_PLACEHOLDER"

        self.eval_table = self._build_eval_table()
        self.review_secret = self._build_review_secret()
        self.anthropic_api_key_secret = self._build_anthropic_api_key_secret()

        self.trigger_fn = self._build_trigger_function()
        self.poll_fn = self._build_poll_function()
        self.submit_review_fn = self._build_submit_review_function()
        self.read_fn = self._build_read_function()

        self._build_poll_schedule(self.poll_fn)

        self.http_api = self._build_http_api()

        self.site_bucket, self.distribution = self._build_static_site()

        cdk.CfnOutput(self, "EvalTableName", value=self.eval_table.table_name)
        cdk.CfnOutput(self, "EvalTableArn", value=self.eval_table.table_arn)
        cdk.CfnOutput(
            self,
            "ReviewSecretArn",
            value=self.review_secret.secret_arn,
            description="Populate out-of-band with a random bearer token (see deploy/eval/README.md), then give it to the one human reviewer.",
        )
        cdk.CfnOutput(
            self,
            "AnthropicApiKeySecretArn",
            value=self.anthropic_api_key_secret.secret_arn,
            description="Populate out-of-band with a general Anthropic API key for this stack's own use (triggering evals + judge calls) -- see deploy/eval/README.md on why this is a SEPARATE secret from deploy/managed-agent's environment key.",
        )

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _build_eval_table(self) -> dynamodb.Table:
        """DynamoDB table `brief-eval-records`: PK `runId`, no sort key, no GSI (v1
        access patterns are all get-by-id or full scan+filter -- a small, occasional
        dataset by design, see PRD §2 "effectively $0 when idle"). PAY_PER_REQUEST,
        RETAIN (real collected evaluation data, same posture as `brief-feedback` and
        `brief-subscribers`, not the managed-agent stack's transient idempotency
        table)."""
        return dynamodb.Table(
            self,
            "EvalTable",
            table_name="brief-eval-records",
            partition_key=dynamodb.Attribute(name="runId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _build_review_secret(self) -> secretsmanager.Secret:
        """The shared reviewer bearer secret (ADR-0013 §E) -- created empty, populated
        out-of-band (README), same pattern as the feedback-token signing secret."""
        return secretsmanager.Secret(
            self,
            "ReviewSecret",
            secret_name=REVIEW_SECRET_NAME,
            description="Shared bearer token gating the eval review UI + its write API (ADR-0013 §E). Populated out-of-band.",
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _build_anthropic_api_key_secret(self) -> secretsmanager.Secret:
        """A SEPARATE Anthropic API key for THIS stack's own use (triggering
        evaluation deployments/sessions via the Deployments/Sessions API, and calling
        the judge LLM via the Messages API) -- deliberately NOT a reuse of
        `deploy/managed-agent`'s `daily-brief-agent/anthropic-environment-key` secret.

        Judgment call (flagged for the orchestrating session to double-check): that
        existing secret is documented (deploy/managed-agent/cdk/managed_agent/stack.py,
        README "Populate the two Secrets Manager secrets") as the self-hosted
        ENVIRONMENT's own worker-auth key -- read only by the microVM execution role
        to authenticate the worker to Anthropic's work queue inside a running
        session. It is scoped to that one purpose and that one role. This stack needs
        a general-purpose API key that can call the Deployments API (create/archive
        deployments, start sessions), the Sessions/Threads API (poll status, mine
        cost), and the Messages API (the judge LLM) from OUTSIDE any session -- a
        materially different credential shape and blast radius than a single
        environment's worker-auth key. Sharing the environment key here would also
        create an undesirable coupling: rotating or revoking this stack's key (e.g.
        if the eval Lambda's key were ever compromised) would, if shared, also break
        the live production pipeline's own worker auth. A separate secret keeps the
        two blast radii independent, at the cost of one more out-of-band population
        step (documented in deploy/eval/README.md)."""
        return secretsmanager.Secret(
            self,
            "AnthropicApiKeySecret",
            secret_name=ANTHROPIC_API_KEY_SECRET_NAME,
            description="General Anthropic API key for the eval harness's own use (Deployments/Sessions/Messages API calls). Populated out-of-band. Deliberately separate from deploy/managed-agent's environment key -- see stack.py docstring.",
            removal_policy=RemovalPolicy.RETAIN,
        )

    # ------------------------------------------------------------------
    # Compute — one function-scoped least-privilege role per Lambda
    # ------------------------------------------------------------------
    def _base_function_kwargs(
        self, handler_dir: str, *, timeout: Duration = Duration.seconds(10), memory_size: int = 128, bundled: bool = False
    ) -> dict:
        """`bundled=True` for a function directory with its own `requirements.txt`
        (currently `trigger/` and `poll/`, which import `httpx`/`anthropic` --
        neither is in the Lambda runtime) -- see `_bundled_function_code()` above.
        `bundled=False` (default) for a function with no third-party deps beyond
        `boto3`/`botocore` (already in the runtime), e.g. `read`/`submit-review`,
        which keep the plain, unbundled `Code.from_asset()` they always used."""
        handler_path = FUNCTIONS_DIR / handler_dir
        code = _bundled_function_code(handler_path) if bundled else _lambda.Code.from_asset(str(handler_path))
        kwargs = dict(
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=code,
            timeout=timeout,
            memory_size=memory_size,
        )
        return kwargs

    def _build_trigger_function(self) -> _lambda.Function:
        """POST /trigger: creates a temporary Deployments-API deployment + starts a
        session (PRD FR-1/FR-2). Role grants: PutItem on the eval table (records the
        pending row), GetSecretValue on both this stack's own secrets. No SES, no
        write to any other table/bucket."""
        role = iam.Role(
            self,
            "TriggerFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the eval-trigger Lambda (PRD FR-1/FR-2, FR-21).",
        )
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EvalTablePut",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:PutItem"],
                resources=[self.eval_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadAnthropicApiKeySecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.anthropic_api_key_secret.secret_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadReviewSecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.review_secret.secret_arn],
            )
        )
        fn = _lambda.Function(
            self,
            "TriggerFunction",
            function_name="brief-eval-trigger",
            role=role,
            **self._base_function_kwargs("trigger", timeout=Duration.seconds(30), bundled=True),
            environment={
                "EVAL_TABLE_NAME": self.eval_table.table_name,
                "ANTHROPIC_API_KEY_SECRET_ARN": self.anthropic_api_key_secret.secret_arn,
                "REVIEW_SECRET_ARN": self.review_secret.secret_arn,
                "PRODUCTION_AGENT_ID": self.production_agent_id,
                "PRODUCTION_ENVIRONMENT_ID": self.production_environment_id,
            },
        )
        return fn

    def _build_poll_function(self) -> _lambda.Function:
        """Scheduled poll-and-process Lambda (PRD FR-1..FR-17): checks in-progress
        evaluations' session status, fetches artifacts from the pipeline bucket
        (read-only, briefs/* only), runs the cost miner + judges + calibration, writes
        the structured record, archives the temporary deployment. Role grants:
        GetItem/PutItem/UpdateItem/Scan on the eval table; s3:ListBucket scoped to
        briefs/* and s3:GetObject scoped to briefs/* on the pipeline bucket (read-only,
        no write, no audio/* access -- this Lambda has no business with audio); read
        both of this stack's own secrets; and (optional, backward-compatible) read-only
        Scan/GetItem on the brief-feedback table when its ARN is supplied (FR-15/FR-21).
        """
        role = iam.Role(
            self,
            "PollFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the eval poll/process Lambda (PRD FR-1..FR-17, FR-21).",
        )
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EvalTableReadWrite",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan"],
                resources=[self.eval_table.table_arn],
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
        role.add_to_policy(
            iam.PolicyStatement(
                sid="S3ReadBriefsOnly",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=[f"arn:{self.partition}:s3:::{PIPELINE_BUCKET_NAME}/briefs/*"],
                # No write permission, and deliberately NO audio/* grant -- this
                # Lambda evaluates the written brief/candidates artifacts only; it
                # never needs the MP3 (unlike the welcome-send Lambda in
                # deploy/subscribers/).
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadAnthropicApiKeySecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.anthropic_api_key_secret.secret_arn],
            )
        )

        feedback_env = {}
        if self.feedback_table_arn:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadFeedbackTableOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["dynamodb:Scan", "dynamodb:GetItem"],
                    resources=[self.feedback_table_arn],
                    # Read-only, no write/delete (PRD FR-15/FR-21, AC-15/AC-21): this
                    # is the harness's ONE cross-stack grant, scoped by ARN to
                    # exactly the one table deploy/feedback/ owns.
                )
            )
            if self.feedback_table_name:
                feedback_env["FEEDBACK_TABLE_NAME"] = self.feedback_table_name

        fn = _lambda.Function(
            self,
            "PollFunction",
            function_name="brief-eval-poll",
            role=role,
            **self._base_function_kwargs("poll", timeout=Duration.minutes(5), memory_size=512, bundled=True),
            environment={
                "EVAL_TABLE_NAME": self.eval_table.table_name,
                "PIPELINE_BUCKET_NAME": PIPELINE_BUCKET_NAME,
                "ANTHROPIC_API_KEY_SECRET_ARN": self.anthropic_api_key_secret.secret_arn,
                **feedback_env,
            },
        )
        return fn

    def _build_poll_schedule(self, poll_fn: _lambda.Function) -> events.Rule:
        """EventBridge scheduled rule invoking the poll Lambda every 2 minutes (PRD
        Phase 6's "(b) a 'poll and process' function... use a simple EventBridge
        scheduled rule invoking this Lambda to check any in-progress evaluation's
        session status"). Always-on at a fixed cadence rather than only-while-pending
        -- the trade-off is a small, constant number of near-empty invocations against
        the complexity of a self-scheduling/self-disabling rule; at a 2-minute period
        this is a negligible, effectively-free cost (PRD §2's "$0 when idle" is a
        slight simplification -- this is the one small, constant exception -- flagged
        for the orchestrating session)."""
        rule = events.Rule(
            self,
            "PollScheduleRule",
            schedule=events.Schedule.rate(Duration.minutes(2)),
            description="Polls in-progress eval-harness evaluation runs every 2 minutes (PRD eval-harness.md Phase 6b).",
        )
        rule.add_target(events_targets.LambdaFunction(poll_fn))
        return rule

    def _build_submit_review_function(self) -> _lambda.Function:
        """POST /reviews: persists a reviewer's agree/override/comment (PRD FR-19).
        Role grants: GetItem/UpdateItem on the eval table only, read the review
        secret. No SES, no other table/bucket access."""
        role = iam.Role(
            self,
            "SubmitReviewFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the review-submit Lambda (PRD FR-19, FR-21).",
        )
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EvalTableReadUpdate",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[self.eval_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadReviewSecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.review_secret.secret_arn],
            )
        )
        fn = _lambda.Function(
            self,
            "SubmitReviewFunction",
            function_name="brief-eval-submit-review",
            role=role,
            **self._base_function_kwargs("submit-review"),
            environment={
                "EVAL_TABLE_NAME": self.eval_table.table_name,
                "REVIEW_SECRET_ARN": self.review_secret.secret_arn,
            },
        )
        return fn

    def _build_read_function(self) -> _lambda.Function:
        """GET /runs, /runs/{runId}, /candidates: the review site's read paths (PRD
        FR-18, FR-24). Role grants: GetItem/Scan on the eval table only (no
        write/query on a GSI -- there is none), read the review secret."""
        role = iam.Role(
            self,
            "ReadFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the eval read Lambda (PRD FR-18/FR-24, FR-21).",
        )
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EvalTableReadOnly",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:Scan"],
                resources=[self.eval_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadReviewSecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.review_secret.secret_arn],
            )
        )
        fn = _lambda.Function(
            self,
            "ReadFunction",
            function_name="brief-eval-read",
            role=role,
            **self._base_function_kwargs("read"),
            environment={
                "EVAL_TABLE_NAME": self.eval_table.table_name,
                "REVIEW_SECRET_ARN": self.review_secret.secret_arn,
            },
        )
        return fn

    # ------------------------------------------------------------------
    # API — HTTP API front door: POST /trigger, POST /reviews, GET /runs[, /{runId}], GET /candidates
    # ------------------------------------------------------------------
    def _build_http_api(self) -> apigwv2.HttpApi:
        allowed_origin = f"https://{self.eval_domain_name or DEFAULT_EVAL_DOMAIN}"

        http_api = apigwv2.HttpApi(
            self,
            "EvalHttpApi",
            api_name="brief-eval-api",
            description="Gated evaluation-harness front door (trigger/poll-status/review) for the daily AI brief.",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[allowed_origin],
                allow_methods=[apigwv2.CorsHttpMethod.GET, apigwv2.CorsHttpMethod.POST],
                allow_headers=["Content-Type", "Authorization"],
                max_age=Duration.hours(1),
            ),
            create_default_stage=False,
        )

        apigwv2.HttpStage(
            self,
            "EvalHttpApiStage",
            http_api=http_api,
            stage_name="$default",
            auto_deploy=True,
            # A modestly higher throttle than the public subscribe/feedback stages --
            # this is a gated, single-reviewer surface, not a public form, so a
            # slightly higher ceiling doesn't materially change the abuse posture
            # (the bearer-secret gate is the real control, not throttling).
            throttle=apigwv2.ThrottleSettings(rate_limit=10, burst_limit=20),
        )

        http_api.add_routes(
            path="/trigger",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration("TriggerIntegration", handler=self.trigger_fn),
        )
        http_api.add_routes(
            path="/reviews",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration("SubmitReviewIntegration", handler=self.submit_review_fn),
        )
        http_api.add_routes(
            path="/runs",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration("ReadRunsIntegration", handler=self.read_fn),
        )
        http_api.add_routes(
            path="/runs/{runId}",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration("ReadRunDetailIntegration", handler=self.read_fn),
        )
        http_api.add_routes(
            path="/candidates",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration("ReadCandidatesIntegration", handler=self.read_fn),
        )

        cdk.CfnOutput(
            self,
            "HttpApiUrl",
            value=http_api.api_endpoint,
            description="Temporary execute-api base URL; wire site/config.js's BRIEF_EVAL_API_BASE_URL to this until custom-domain DNS is attached.",
        )
        return http_api

    # ------------------------------------------------------------------
    # Static site — private S3 bucket + CloudFront with Origin Access Control
    # ------------------------------------------------------------------
    def _build_static_site(self):
        bucket = s3.Bucket(
            self,
            "EvalSiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        certificate = None
        domain_names = None
        if self.certificate_arn and self.eval_domain_name:
            certificate = acm.Certificate.from_certificate_arn(self, "EvalSiteCertificate", self.certificate_arn)
            domain_names = [self.eval_domain_name]

        distribution = cloudfront.Distribution(
            self,
            "EvalSiteDistribution",
            default_root_object="index.html",
            domain_names=domain_names,
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404, response_http_status=404, response_page_path="/index.html", ttl=Duration.minutes(5)
                ),
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=404, response_page_path="/index.html", ttl=Duration.minutes(5)
                ),
            ],
        )

        s3_deployment.BucketDeployment(
            self,
            "EvalSiteDeployment",
            sources=[s3_deployment.Source.asset(str(SITE_DIR))],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        cdk.CfnOutput(self, "SiteBucketName", value=bucket.bucket_name)
        cdk.CfnOutput(
            self,
            "DistributionDomainName",
            value=distribution.distribution_domain_name,
            description="Reachable now at this *.cloudfront.net domain; eval.mschweier.com requires the manual DNS step in deploy/eval/README.md.",
        )
        return bucket, distribution
