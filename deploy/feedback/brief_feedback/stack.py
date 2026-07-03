"""FeedbackStack — the standalone public reader-feedback surface.

See docs/prd/reader-feedback.md and docs/adr/0011 (token scheme) /
docs/adr/0012 (this stack's shape + the token-helper packaging) for the design this
implements. Structurally mirrors `deploy/subscribers/brief_subscribers/stack.py`'s
construct patterns (HTTP API + locked CORS + throttled stage, private S3 bucket + OAC
CloudFront + BucketDeployment, `certificateArn`/domain context with a default
fallback) but shares **no** resource or IAM role with that stack or with
`deploy/managed-agent/` (ADR-0012 §B, PRD FR-1/§6). This is a genuinely standalone
deploy lifecycle: its own DynamoDB table, its own token-signing secret, its own
Lambda + role, its own HTTP API, its own CloudFront distribution.
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"
SITE_DIR = Path(__file__).resolve().parent.parent / "site"

# Fallback CORS/site origin used only when no `feedbackDomainName` context is supplied
# (e.g. a bare `cdk synth` before DNS is decided). Real deploys should pass
# `-c feedbackDomainName=feedback.mschweier.com` (or set it in cdk.json context) so
# CORS is locked to the actual site origin (ADR-0012 §B, PRD §6).
DEFAULT_FEEDBACK_DOMAIN = "feedback.mschweier.com"

FEEDBACK_TOKEN_SECRET_NAME = "daily-ai-brief/feedback-token-signing-secret"

# Free-text answer length cap, server-enforced by the submit handler (PRD FR-7,
# ADR-0012 §B "Submit handler behavior"). Exposed here too so the stack/tests can
# reference the same value the handler enforces without a second hardcoded literal.
FREE_TEXT_MAX_LENGTH = 2000


class FeedbackStack(Stack):
    """One stack: DynamoDB, the signing secret, the submit Lambda + role, HTTP API,
    static site."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.feedback_domain_name = self.node.try_get_context("feedbackDomainName")
        self.certificate_arn = self.node.try_get_context("certificateArn")

        self.table = self._build_table()
        self.signing_secret = self._build_signing_secret()

        self.submit_fn = self._build_submit_function()
        self.http_api = self._build_http_api()

        self.site_bucket, self.distribution = self._build_static_site()

        cdk.CfnOutput(self, "FeedbackTableName", value=self.table.table_name)
        cdk.CfnOutput(self, "FeedbackTableArn", value=self.table.table_arn)
        cdk.CfnOutput(
            self,
            "FeedbackTokenSecretArn",
            value=self.signing_secret.secret_arn,
            description=(
                "Populate out-of-band (see deploy/feedback/README.md), then pass as "
                "-c feedbackTokenSecretArn=<this ARN> to deploy/managed-agent/cdk and "
                "deploy/subscribers so both send paths can generate feedback links."
            ),
        )

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _build_table(self) -> dynamodb.Table:
        """DynamoDB table `brief-feedback` per ADR-0012 §B.1: PK `submissionId`, no sort
        key, no GSI. PAY_PER_REQUEST, SSE (AWS managed), PITR enabled, RETAIN (real
        collected data, unlike the managed-agent stack's transient idempotency table),
        no TTL — feedback is retained indefinitely.
        """
        return dynamodb.Table(
            self,
            "FeedbackTable",
            table_name="brief-feedback",
            partition_key=dynamodb.Attribute(name="submissionId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _build_signing_secret(self) -> secretsmanager.Secret:
        """The shared feedback-token HMAC signing secret (ADR-0011), owned here per
        ADR-0012 §B.2: created empty (no SecretString — CDK/CloudFormation cannot set a
        real value here without it landing in a template/state file), populated
        out-of-band after first deploy (README). RETAIN, matching the other secrets in
        this repo. Its ARN is exposed as a CfnOutput so the two send-side stacks
        (managed-agent, subscribers) can be granted read access by ARN at their own,
        independent deploy time (ADR-0011's "Owning stack" section)."""
        return secretsmanager.Secret(
            self,
            "FeedbackTokenSigningSecret",
            secret_name=FEEDBACK_TOKEN_SECRET_NAME,
            description=(
                "HMAC-SHA256 signing secret for feedback-link tokens (ADR-0011). "
                "Populated out-of-band."
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

    # ------------------------------------------------------------------
    # Compute — the submit Lambda + its own least-privilege role
    # ------------------------------------------------------------------
    def _build_submit_function(self) -> _lambda.Function:
        """`brief-feedback-submit`: stdlib + runtime-provided boto3 only (DynamoDB
        PutItem/GetItem/UpdateItem, Secrets Manager GetSecretValue, and the
        stdlib-only `feedback_token` copy) — no `requirements.txt`, no bundling, exactly
        like the subscribers functions (ADR-0012 §B.3: "the `_LocalPipBundling`
        platform-locked pip machinery... is therefore not needed here"). Timeout 10s /
        128MB, matching the subscribers functions' sub-second-work default sizing.

        Role — exactly these grants, nothing else (PRD FR-16, AC-15):
          - AWSLambdaBasicExecutionRole (own logs only).
          - sid="FeedbackTablePut": PutItem + GetItem + UpdateItem on the one table ARN.
            GetItem/UpdateItem are included (not PutItem-only) to support an in-request,
            same-table conditional-write throttle counter keyed by a hashed identity +
            coarse time bucket (ADR-0012 §B.3's documented option), still scoped to
            exactly this one table ARN — no second table, no GSI, no broader resource.
          - sid="ReadFeedbackTokenSecret": GetSecretValue scoped to the one signing
            secret ARN this stack owns.
          - No SES. No access to brief-subscribers. No access to
            cowork-polly-tts-740353583786. No reuse of any subscribers-stack role. No
            static keys.
        """
        role = iam.Role(
            self,
            "SubmitFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the feedback submit Lambda (ADR-0012 §B.3, PRD FR-16).",
        )
        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="FeedbackTablePut",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[self.table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadFeedbackTokenSecret",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.signing_secret.secret_arn],
            )
        )

        fn = _lambda.Function(
            self,
            "SubmitFunction",
            function_name="brief-feedback-submit",
            role=role,
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=_lambda.Code.from_asset(str(FUNCTIONS_DIR / "submit")),
            timeout=Duration.seconds(10),
            memory_size=128,
            environment={
                "FEEDBACK_TABLE_NAME": self.table.table_name,
                "FEEDBACK_TOKEN_SECRET_ARN": self.signing_secret.secret_arn,
            },
        )
        return fn

    # ------------------------------------------------------------------
    # API — HTTP API front door (ADR-0012 §B.4): POST /submit
    # ------------------------------------------------------------------
    def _build_http_api(self) -> apigwv2.HttpApi:
        allowed_origin = f"https://{self.feedback_domain_name or DEFAULT_FEEDBACK_DOMAIN}"

        http_api = apigwv2.HttpApi(
            self,
            "FeedbackHttpApi",
            api_name="brief-feedback-api",
            description="Public feedback-submission front door for the daily AI brief.",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[allowed_origin],
                allow_methods=[apigwv2.CorsHttpMethod.POST],
                allow_headers=["Content-Type"],
                max_age=Duration.hours(1),
            ),
            create_default_stage=False,
        )

        # Explicit stage with a deliberate throttle (PRD FR-17/AC-16), same posture as
        # the subscribers stack's rate_limit=10, burst_limit=20 — a low value suited to
        # human-paced feedback submission, not a high-volume API.
        apigwv2.HttpStage(
            self,
            "FeedbackHttpApiStage",
            http_api=http_api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigwv2.ThrottleSettings(rate_limit=10, burst_limit=20),
        )

        http_api.add_routes(
            path="/submit",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "SubmitIntegration", handler=self.submit_fn
            ),
        )

        cdk.CfnOutput(
            self,
            "HttpApiUrl",
            value=http_api.api_endpoint,
            description="Temporary execute-api base URL; wire site/config.js's BRIEF_FEEDBACK_API_BASE_URL to this until custom-domain DNS is attached.",
        )
        return http_api

    # ------------------------------------------------------------------
    # Static site — private S3 bucket + its own CloudFront with Origin Access Control
    # ------------------------------------------------------------------
    def _build_static_site(self):
        bucket = s3.Bucket(
            self,
            "FeedbackSiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Custom domain + ACM cert are included here per ADR-0012, but actually
        # attaching DNS is a manual runbook step (deploy/feedback/README.md) — this
        # sandbox does not have DNS access, so `certificateArn`/`feedbackDomainName`
        # context values are optional and the distribution falls back to its default
        # *.cloudfront.net domain when they are not supplied (mirrors deploy/subscribers/).
        certificate = None
        domain_names = None
        if self.certificate_arn and self.feedback_domain_name:
            certificate = acm.Certificate.from_certificate_arn(
                self, "FeedbackSiteCertificate", self.certificate_arn
            )
            domain_names = [self.feedback_domain_name]

        distribution = cloudfront.Distribution(
            self,
            "FeedbackSiteDistribution",
            default_root_object="index.html",
            domain_names=domain_names,
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            error_responses=[
                # Unknown paths fall back to the feedback form rather than S3's raw
                # XML 404/403 body — this is a small static site with no client-side
                # router, mirroring SubscribeSiteDistribution's identical choice.
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=404,
                    response_page_path="/index.html",
                    ttl=Duration.minutes(5),
                ),
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=404,
                    response_page_path="/index.html",
                    ttl=Duration.minutes(5),
                ),
            ],
        )

        s3_deployment.BucketDeployment(
            self,
            "FeedbackSiteDeployment",
            sources=[s3_deployment.Source.asset(str(SITE_DIR))],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        cdk.CfnOutput(
            self,
            "SiteBucketName",
            value=bucket.bucket_name,
        )
        cdk.CfnOutput(
            self,
            "DistributionDomainName",
            value=distribution.distribution_domain_name,
            description="Reachable now at this *.cloudfront.net domain; feedback.mschweier.com requires the manual DNS step in deploy/feedback/README.md.",
        )
        return bucket, distribution
