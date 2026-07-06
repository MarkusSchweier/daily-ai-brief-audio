"""BriefDeliveryStack -- the decoupled AWS delivery boundary (PRD
docs/prd/agent-system-redesign.md FR-1/FR-2/FR-2a/FR-3/FR-8, ADR-0014 Decisions
2a/2d). A standalone CDK app, sibling to `deploy/subscribers/`, `deploy/feedback/`,
and `deploy/eval/`: its own DynamoDB table, its own Secrets Manager secrets, its own
Lambda + least-privilege role, its own HTTP API. Shares NO resource or IAM role
with any of those stacks or with `deploy/managed-agent/` -- this stack is the
ONE place that holds SES-to-subscriber rights post-redesign; the content-generation
side (a Claude Platform agent) holds none at all (FR-1).

Two independent Secrets Manager secrets gate three routes:
  - `POST /deliver` + `GET /deliver/{deliveryId}` -- gated by the DELIVERY bearer
    secret (ADR-0014 Decision 2b). The only surface that can email real
    subscribers; its auth is the tightest control in this stack.
  - `GET /recent-briefs` -- gated by a SEPARATE, read-only bearer secret (ADR-0014
    Decision 2d), so a `cloud` candidate can read the same recent priors
    production reads from S3 (parity with production's own read-recent-briefs
    step) WITHOUT ever holding an AWS credential or the delivery/send token. The
    two secrets are deliberately non-interchangeable -- see
    `recent_briefs_auth.py`'s module docstring and
    `tests/test_recent_briefs_auth_separation.py`.

What this stack does NOT do (deliberately, per the PRD/ADR):
  - It does not touch `deploy/managed-agent/deployment.json`,
    `deploy/managed-agent/pipeline/audio_email.py`, or `deploy/managed-agent/microvm/`
    -- those remain the LIVE production pipeline, untouched by this phase (PRD Non-
    goals; ADR-0014 Decision 1's staged cut-over has not happened yet). Production's
    own S3-backed read-recent-briefs step is untouched; GET /recent-briefs is
    purely additive, for `cloud` candidates only (ADR-0014 Decision 2d).
  - It does not touch `deploy/subscribers/` or `deploy/feedback/`.
  - It does not perform any real `cdk deploy` -- built, synthesized, and tested
    LOCALLY ONLY per this phase's brief; actual deployment is a separate,
    orchestrator-run step after independent review/security passes.
  - It does not populate either bearer secret with a real value -- both created
    EMPTY (RemovalPolicy.RETAIN, no initial SecretString), populated out-of-band,
    matching `deploy/eval/`'s `_build_review_secret()` convention exactly.
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
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"
DELIVER_DIR = FUNCTIONS_DIR / "deliver"

# The existing pipeline resources this stack's delivery Lambda needs -- NOT owned
# by this stack, exactly the same resources `deploy/managed-agent/`'s
# `MicroVmExecutionRole` grants today (moved here, not duplicated -- see the IAM
# section below).
PIPELINE_BUCKET_NAME = "cowork-polly-tts-740353583786"
SENDER = "aibriefing@mschweier.com"
SUBSCRIBERS_TABLE_STATUS_INDEX_ARN_TEMPLATE = (
    "arn:aws:dynamodb:{region}:{account}:table/brief-subscribers/index/status-index"
)

DELIVERY_BEARER_SECRET_NAME = "daily-ai-brief/delivery-bearer-secret"
# The GET /recent-briefs read-only bearer secret (ADR-0014 Decision 2d) --
# DISTINCT from DELIVERY_BEARER_SECRET_NAME above. This is the central auth-
# separation property Decision 2d requires: a candidate given only this secret's
# token can call GET /recent-briefs but is structurally unable to authenticate to
# POST /deliver (which checks ONLY the delivery secret above) -- see
# `recent_briefs_auth.py`'s module docstring and
# `test_recent_briefs_auth_separation.py` for the non-interchangeability proof.
RECENT_BRIEFS_READ_BEARER_SECRET_NAME = "daily-ai-brief/recent-briefs-read-bearer-secret"
# A literal Python string constant (NOT `fn.function_name`, a CDK token that
# resolves to `{"Ref": "DeliverFunction..."}`) -- used both as the Function's own
# `function_name` prop AND to build the self-invoke ARN. Using this literal
# constant, rather than reading the property back off the constructed function,
# is what breaks the Role<->Function CloudFormation dependency cycle -- see
# `_grant_self_invoke()`'s docstring for the full explanation.
DELIVER_FUNCTION_NAME = "brief-delivery-deliver"


# --- Lambda asset bundling for the deliver function's one third-party dep (markdown) ---
#
# `deliver/handler.py` (via `delivery_core.py`) imports `markdown` -- not in the
# Python 3.13 Lambda runtime, so a plain `Code.from_asset(<dir>)` (no bundling)
# would leave it `ImportError`ing at cold start. This reuses the exact, proven
# bundling pattern from `deploy/eval/brief_eval/stack.py`'s `_LocalPipBundling`
# (itself adapted from `deploy/managed-agent/cdk/managed_agent/stack.py`'s launcher
# bundling, confirmed live 2026-07-03 to be the fix for a real wrong-platform-wheel
# bug): install into the output dir with `--platform manylinux2014_aarch64
# --implementation cp --python-version 3.13 --abi cp313 --only-binary=:all:` so pip
# resolves prebuilt Linux/aarch64 CPython 3.13 wheels regardless of the build
# host's actual platform, no Docker required. `markdown` is a pure-Python package
# published as a universal wheel, so it survives `--only-binary=:all:` cleanly
# (unlike the launcher's `standardwebhooks`, an sdist-only dependency needing an
# unlocked fallback install -- not needed here).
@jsii.implements(ILocalBundling)
class _LocalPipBundling:
    """Bundle by running a cross-platform `pip install -r requirements.txt -t <out>`
    on the host, forcing Linux/aarch64/cp313 wheels regardless of host platform, for
    the one function directory (`deliver/`) that has third-party deps."""

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
    execution environment), falling back to local bundling (`_LocalPipBundling`)
    when Docker is unavailable, mirroring `deploy/eval/brief_eval/stack.py`'s
    `_bundled_function_code()` exactly. `asset_hash_type=OUTPUT` so a
    bundling-*logic*-only change (not a source-file change) still gets redeployed."""
    bundling = BundlingOptions(
        image=DockerImage.from_registry("public.ecr.aws/sam/build-python3.13:latest"),
        command=[
            "bash",
            "-c",
            "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
        ],
        local=_LocalPipBundling(handler_dir),
    )
    return _lambda.Code.from_asset(
        str(handler_dir),
        bundling=bundling,
        asset_hash_type=AssetHashType.OUTPUT,
    )


class BriefDeliveryStack(Stack):
    """One stack: the `brief-deliveries` DynamoDB tracking table, the empty
    delivery bearer secret, the empty recent-briefs read-only bearer secret
    (ADR-0014 Decision 2d), the single deliver Lambda + its least-privilege role,
    and the HTTP API front door (`POST /deliver`, `GET /deliver/{deliveryId}`,
    `GET /recent-briefs`)."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Optional, backward-compatible context values -- absent by default so this
        # stack synthesizes/deploys cleanly before the sibling stacks it reads
        # cross-stack from (by ARN, same pattern as
        # deploy/managed-agent/cdk/managed_agent/stack.py's `feedbackTokenSecretArn`)
        # exist. Never a hard build-time dependency on those stacks' own synthesis.
        self.subscribers_table_name = self.node.try_get_context("subscribersTableName") or "brief-subscribers"
        self.subscribers_api_base_url = self.node.try_get_context("subscribersApiBaseUrl") or ""
        self.feedback_token_secret_arn = self.node.try_get_context("feedbackTokenSecretArn")
        self.feedback_base_url = self.node.try_get_context("feedbackBaseUrl") or ""

        self.deliveries_table = self._build_deliveries_table()
        self.delivery_bearer_secret = self._build_delivery_bearer_secret()
        self.recent_briefs_read_bearer_secret = self._build_recent_briefs_read_bearer_secret()

        self.deliver_fn = self._build_deliver_function()
        self._grant_self_invoke(self.deliver_fn)

        self.http_api = self._build_http_api()

        cdk.CfnOutput(self, "DeliveriesTableName", value=self.deliveries_table.table_name)
        cdk.CfnOutput(self, "DeliveriesTableArn", value=self.deliveries_table.table_arn)
        cdk.CfnOutput(
            self,
            "DeliveryBearerSecretArn",
            value=self.delivery_bearer_secret.secret_arn,
            description=(
                "Populate out-of-band with a random bearer token (see deploy/delivery/README.md), "
                "then give it to the content-generation agent as an environment variable so its "
                "curl to POST /deliver can authenticate."
            ),
        )
        cdk.CfnOutput(
            self,
            "RecentBriefsReadBearerSecretArn",
            value=self.recent_briefs_read_bearer_secret.secret_arn,
            description=(
                "Populate out-of-band with a random bearer token, DISTINCT from the delivery "
                "bearer secret above (ADR-0014 Decision 2d). Give ONLY this token to a `cloud` "
                "candidate -- never the delivery bearer secret -- so it can curl GET "
                "/recent-briefs but can never authenticate to POST /deliver."
            ),
        )
        cdk.CfnOutput(
            self,
            "HttpApiUrl",
            value=self.http_api.api_endpoint,
            description=(
                "Base URL for POST /deliver, GET /deliver/{deliveryId}, and GET /recent-briefs. "
                "Give this to the content-generation agent's environment/init_script config."
            ),
        )

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _build_deliveries_table(self) -> dynamodb.Table:
        """DynamoDB table `brief-deliveries`: PK `deliveryId` (a generated UUID), no
        sort key, no GSI -- single-item get-by-id is the only access pattern
        (mirroring `deploy/eval/brief_eval/stack.py`'s `_build_eval_table()`).
        PAY_PER_REQUEST, RETAIN (this table tracks real delivery outcomes -- useful
        operational history, same posture as `brief-eval-records`, not a purely
        transient idempotency table like the managed-agent stack's dedup table)."""
        return dynamodb.Table(
            self,
            "DeliveriesTable",
            table_name="brief-deliveries",
            partition_key=dynamodb.Attribute(name="deliveryId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _build_delivery_bearer_secret(self) -> secretsmanager.Secret:
        """The shared delivery bearer secret (ADR-0014 Decision 2b) -- created
        EMPTY, populated out-of-band (README), same pattern as
        `deploy/eval/`'s `_build_review_secret()`. A NEW secret/purpose, not a
        reuse of `deploy/eval/`'s reviewer secret or `deploy/managed-agent/`'s
        environment key -- see `delivery_auth.py`'s module docstring for why."""
        return secretsmanager.Secret(
            self,
            "DeliveryBearerSecret",
            secret_name=DELIVERY_BEARER_SECRET_NAME,
            description=(
                "Shared bearer token gating POST /deliver + GET /deliver/{deliveryId} "
                "(ADR-0014 Decision 2b). Populated out-of-band."
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _build_recent_briefs_read_bearer_secret(self) -> secretsmanager.Secret:
        """The read-only bearer secret gating GET /recent-briefs (ADR-0014
        Decision 2d) -- created EMPTY, populated out-of-band, same convention as
        `_build_delivery_bearer_secret()` above. DELIBERATELY a SEPARATE secret,
        not a reuse of DELIVERY_BEARER_SECRET_NAME: the central auth-separation
        property Decision 2d requires is that a `cloud` candidate given ONLY this
        secret's token can read recent priors but is structurally UNABLE to
        authenticate to POST /deliver (which checks only the OTHER secret) -- see
        `recent_briefs_auth.py`'s module docstring for the full rationale."""
        return secretsmanager.Secret(
            self,
            "RecentBriefsReadBearerSecret",
            secret_name=RECENT_BRIEFS_READ_BEARER_SECRET_NAME,
            description=(
                "Read-only bearer token gating GET /recent-briefs ONLY (ADR-0014 Decision 2d) "
                "-- DISTINCT from the delivery bearer secret; never authenticates POST /deliver "
                "or GET /deliver/{deliveryId}. Populated out-of-band."
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

    # ------------------------------------------------------------------
    # Compute -- ONE Lambda handling both the sync trigger/poll legs (API Gateway)
    # and the async self-invoke worker leg (see handler.py's module docstring for
    # why one function, not two).
    # ------------------------------------------------------------------
    def _build_deliver_function(self) -> _lambda.Function:
        """The delivery Lambda's execution role holds EXACTLY today's
        `MicroVmExecutionRole` delivery grants (moved, not duplicated -- ADR-0014
        Decision 2a), copied verbatim from
        `deploy/managed-agent/cdk/managed_agent/stack.py`'s
        `_build_microvm_execution_role()` (roughly lines 371-488): PollySynthesis,
        S3AudioReadWrite, S3ListBriefsPrefix, SesSendFromMschweier,
        DynamoDBSubscribersQuery, and (conditionally) ReadFeedbackTokenSecret.
        Deliberately NOT copied: `ReadEnvironmentKey` (the microVM's own
        worker-auth secret -- not applicable here) and `RuntimeLogs`'s bespoke
        microVM log-group ARN pattern (this Lambda uses the standard
        AWSLambdaBasicExecutionRole managed policy instead, same as every other
        Lambda in this repo).

        NEW grants this role needs that the old one didn't: read the delivery
        bearer secret; read the recent-briefs read-only bearer secret (ADR-0014
        Decision 2d -- a SEPARATE ARN-scoped grant, auth machinery only, NOT a
        broadening of any AWS delivery capability -- GET /recent-briefs needs NO
        new S3/Polly/SES/DynamoDB grant at all, since S3AudioReadWrite +
        S3ListBriefsPrefix below already cover exactly what
        `read_recent_prior_briefs()` reads); GetItem/PutItem/UpdateItem on the
        `brief-deliveries` table (self-invoke tracking); `lambda:InvokeFunction` on
        its own function ARN (added post-construction, see `_grant_self_invoke()`
        -- the role object must exist before the function so it can be attached at
        Function() construction time, but the function's own ARN doesn't exist
        until AFTER that, so the self-invoke grant is a separate, later
        `add_to_policy` call)."""
        role = iam.Role(
            self,
            "DeliverFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Least-privilege execution role for the decoupled delivery Lambda (PRD "
                "docs/prd/agent-system-redesign.md FR-1/FR-2/FR-2a/FR-3). Holds EXACTLY "
                "today's MicroVmExecutionRole delivery grants, moved not duplicated."
            ),
        )
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))

        role.add_to_policy(
            iam.PolicyStatement(
                sid="DeliveriesTableAccess",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
                resources=[self.deliveries_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadDeliveryBearerSecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.delivery_bearer_secret.secret_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadRecentBriefsReadBearerSecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.recent_briefs_read_bearer_secret.secret_arn],
            )
        )

        # -- Moved verbatim from MicroVmExecutionRole (deploy/managed-agent/cdk/managed_agent/stack.py) --
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
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SesSendFromMschweier",
                effect=iam.Effect.ALLOW,
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=["*"],
                conditions={"StringEquals": {"ses:FromAddress": SENDER}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDBSubscribersQuery",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:Query"],
                resources=[
                    SUBSCRIBERS_TABLE_STATUS_INDEX_ARN_TEMPLATE.format(region=self.region, account=self.account)
                ],
            )
        )
        if self.feedback_token_secret_arn:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadFeedbackTokenSecret",
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[self.feedback_token_secret_arn],
                )
            )

        fn = _lambda.Function(
            self,
            "DeliverFunction",
            function_name=DELIVER_FUNCTION_NAME,
            role=role,
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=_bundled_function_code(DELIVER_DIR),
            # Generously above the real runtime (Polly's own existing 5-minute
            # allowance + the SES fan-out loop after it) -- ADR-0014 Decision 2a's
            # "Why async" note.
            timeout=Duration.minutes(10),
            memory_size=256,
            environment={
                "DELIVERIES_TABLE_NAME": self.deliveries_table.table_name,
                "DELIVERY_BEARER_SECRET_ARN": self.delivery_bearer_secret.secret_arn,
                "RECENT_BRIEFS_READ_BEARER_SECRET_ARN": self.recent_briefs_read_bearer_secret.secret_arn,
                "SUBSCRIBERS_TABLE_NAME": self.subscribers_table_name,
                "SUBSCRIBERS_API_BASE_URL": self.subscribers_api_base_url,
                "FEEDBACK_TOKEN_SECRET_ARN": self.feedback_token_secret_arn or "",
                "FEEDBACK_BASE_URL": self.feedback_base_url,
                # A literal string constant, not `fn.function_name` (a CDK token) --
                # the function needs to know its OWN literal name at runtime to
                # self-invoke via `lambda_client.invoke(FunctionName=...)`; since
                # this stack already chose that name explicitly (DELIVER_FUNCTION_NAME,
                # a plain Python string), it can be set here directly at construction
                # time rather than patched on afterward.
                "DELIVERY_FUNCTION_NAME": DELIVER_FUNCTION_NAME,
            },
        )
        return fn

    def _grant_self_invoke(self, fn: _lambda.Function) -> None:
        """`lambda:InvokeFunction` on the function's OWN ARN -- the async
        self-invoke kick-off mechanism (ADR-0014 Decision 2a). Added AFTER
        `_lambda.Function()` construction (the role object exists before the
        function, per `_build_deliver_function()`'s docstring) -- `fn.role` is the
        SAME role object `_build_deliver_function()` built, so this is a genuine
        post-construction addition to that one role, not a second role.

        IMPORTANT: uses `self.format_arn(resource_name=DELIVER_FUNCTION_NAME, ...)`
        -- a manually-constructed ARN STRING built from the module-level
        `DELIVER_FUNCTION_NAME` literal Python string constant -- rather than
        `fn.function_arn` (or `fn.function_name`, which is ALSO a CDK token, not
        the literal string, despite having been passed in as one: CDK does not
        echo back the literal you supplied, it returns a `Fn::GetAtt`/`Ref` token
        so the property stays correct even if CloudFormation ever renamed the
        resource). Using `fn.function_arn` (or `fn.function_name`) here creates a
        genuine CloudFormation dependency CYCLE -- CONFIRMED LIVE (an earlier
        version of this method used `fn.function_name` here and
        `cdk.assertions.Template.from_stack()`, which validates the template's
        full dependency graph rather than just its shape, rejected it with
        "Template is undeployable, these resources have a dependency cycle:
        DeliverFunctionRoleDefaultPolicy... -> DeliverFunction... ->
        DeliverFunctionRoleDefaultPolicy..." -- `cdk synth` alone did not catch
        this, since the CLI's synth step does not run this same cycle-detection
        pass). The cycle: the Function resource already depends on its Role (via
        the `Role` property, `Fn::GetAtt` to the role's ARN); if the Role's OWN
        policy statement then references the Function's `Ref`/`Fn::GetAtt`,
        CloudFormation sees Function -> Role -> Function -> Role..., unresolvable.
        Building the ARN from the literal `DELIVER_FUNCTION_NAME` string instead
        breaks the cycle: the policy statement's Resource becomes a plain
        Fn::Join expression over static strings and pseudo-parameters
        (AWS::Partition/Region/AccountId) that does not reference the Function
        resource AT ALL, so the Role's policy no longer depends on the Function --
        only the Function still depends on the Role, the correct, acyclic
        direction."""
        self_invoke_arn = self.format_arn(
            service="lambda",
            resource="function",
            resource_name=DELIVER_FUNCTION_NAME,
        )
        fn.role.add_to_policy(
            iam.PolicyStatement(
                sid="SelfInvokeForAsyncDeliveryWorker",
                effect=iam.Effect.ALLOW,
                actions=["lambda:InvokeFunction"],
                resources=[self_invoke_arn],
            )
        )

    # ------------------------------------------------------------------
    # API -- HTTP API front door: POST /deliver, GET /deliver/{deliveryId},
    # GET /recent-briefs (ADR-0014 Decision 2d)
    # ------------------------------------------------------------------
    def _build_http_api(self) -> apigwv2.HttpApi:
        """No CORS config: this API has exactly one caller (the content-generation
        agent's own scripted `curl`, never a browser), unlike the sibling stacks'
        public-facing APIs -- so there is no browser origin to allow-list.

        All three routes integrate to the SAME `deliver_fn` (the Lambda branches
        on the request's path/marker, per `handler.py`'s module docstring) --
        `GET /recent-briefs` is a synchronous read route added by ADR-0014
        Decision 2d, gated by its OWN separate `recent_briefs_auth` secret (never
        the delivery bearer secret `POST /deliver` and `GET /deliver/{deliveryId}`
        check) -- see the IAM section above for why this needs no new AWS
        delivery grant, only a new secret-read grant."""
        http_api = apigwv2.HttpApi(
            self,
            "DeliveryHttpApi",
            api_name="brief-delivery-api",
            description=(
                "Bearer-gated delivery boundary (derive HTML, synthesize audio, send, archive; "
                "plus a separately-gated recent-priors read route) for the daily AI brief."
            ),
            create_default_stage=False,
        )

        apigwv2.HttpStage(
            self,
            "DeliveryHttpApiStage",
            http_api=http_api,
            stage_name="$default",
            auto_deploy=True,
            # A modest throttle -- this is a single-caller, bearer-gated surface
            # (one delivery per weekday run, plus occasional candidate/eval runs),
            # not a public form; the bearer-secret gate is the real control.
            throttle=apigwv2.ThrottleSettings(rate_limit=10, burst_limit=20),
        )

        http_api.add_routes(
            path="/deliver",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration("DeliverTriggerIntegration", handler=self.deliver_fn),
        )
        http_api.add_routes(
            path="/deliver/{deliveryId}",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration("DeliverPollIntegration", handler=self.deliver_fn),
        )
        http_api.add_routes(
            path="/recent-briefs",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration("RecentBriefsReadIntegration", handler=self.deliver_fn),
        )

        return http_api
