"""BriefSubscribersStack — the public subscribe/confirm/unsubscribe surface.

See docs/adr/0001-serverless-subscription-architecture.md for the architecture this
implements: S3 + CloudFront (static site) -> API Gateway HTTP API -> three Lambdas ->
DynamoDB -> SES. See docs/adr/0002 for the IAM design and docs/adr/0003 for the data
model. This module is built incrementally (table + Lambda roles first, then the API,
then the static site) to match the developer task's staged commit plan; each stage is
additive to the previous one.
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
)
from constructs import Construct

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"
LAYERS_DIR = Path(__file__).resolve().parent.parent / "layers"
SITE_DIR = Path(__file__).resolve().parent.parent / "site"

SUBSCRIBER_SENDER = "aibriefing@mschweier.com"

# Fallback CORS origin used only when no `subscribeDomainName` context is supplied (e.g.
# a bare `cdk synth` before DNS is decided). Real deploys should pass
# `-c subscribeDomainName=briefing.mschweier.com` (or set it in cdk.json context) so CORS
# is locked to the actual site origin, per the developer task and ADR-0001.
DEFAULT_SUBSCRIBE_DOMAIN = "briefing.mschweier.com"


class BriefSubscribersStack(Stack):
    """One stack: DynamoDB, three Lambdas + roles, HTTP API, static site."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.subscribe_domain_name = self.node.try_get_context("subscribeDomainName")
        self.certificate_arn = self.node.try_get_context("certificateArn")

        self.table = self._build_table()
        self.common_layer = self._build_common_layer()

        self.subscribe_fn = self._build_subscribe_function()
        self.confirm_fn = self._build_confirm_function()
        self.unsubscribe_fn = self._build_unsubscribe_function()

        self.http_api = self._build_http_api()

        # The subscribe Lambda builds the confirm link it emails out, so it needs its own
        # API's base URL. Wired here (not at function-creation time) because the HTTP API
        # doesn't exist yet when _build_subscribe_function() runs — CloudFormation resolves
        # this Fn::GetAtt reference fine since it's just string interpolation into an env
        # var, not a runtime call cycle. Without this, confirm_link falls back to a relative
        # path and mail clients mangle it (e.g. macOS Mail turns it into an unclickable
        # x-webdoc:// URL).
        self.subscribe_fn.add_environment("API_BASE_URL", self.http_api.api_endpoint)

        self.site_bucket, self.distribution = self._build_static_site()

        cdk.CfnOutput(self, "SubscribersTableName", value=self.table.table_name)
        cdk.CfnOutput(self, "SubscribersTableArn", value=self.table.table_arn)
        cdk.CfnOutput(
            self,
            "SubscribersStatusIndexArn",
            value=f"{self.table.table_arn}/index/status-index",
            description="Use this exact ARN in deploy/iam-policy.json's DynamoDBSubscribersQuery Sid (ADR-0002).",
        )

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _build_table(self) -> dynamodb.Table:
        """DynamoDB table `brief-subscribers` per docs/adr/0003.

        PK `email` (normalized lowercase); GSI `status-index` on `status` for the
        fan-out's Query-only read; TTL on `confirmTokenExpiresAt` to auto-purge
        never-confirmed rows ~48h after creation.
        """
        table = dynamodb.Table(
            self,
            "SubscribersTable",
            table_name="brief-subscribers",
            partition_key=dynamodb.Attribute(name="email", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="confirmTokenExpiresAt",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        table.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.INCLUDE,
            non_key_attributes=["firstName", "unsubscribeToken"],
        )
        return table

    def _build_common_layer(self) -> _lambda.LayerVersion:
        return _lambda.LayerVersion(
            self,
            "SubscriberCommonLayer",
            code=_lambda.Code.from_asset(str(LAYERS_DIR / "common")),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_13],
            compatible_architectures=[_lambda.Architecture.ARM_64],
            description="Shared email-normalization/token/DynamoDB helpers for the subscriber Lambdas.",
        )

    # ------------------------------------------------------------------
    # Compute — one function-scoped least-privilege role per Lambda (ADR-0002 §A)
    # ------------------------------------------------------------------
    def _base_function_kwargs(self, handler_dir: str) -> dict:
        return dict(
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=_lambda.Code.from_asset(str(FUNCTIONS_DIR / handler_dir)),
            layers=[self.common_layer],
            timeout=Duration.seconds(10),
            memory_size=128,
            environment={"SUBSCRIBERS_TABLE_NAME": self.table.table_name},
        )

    def _build_subscribe_function(self) -> _lambda.Function:
        role = iam.Role(
            self,
            "SubscribeFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the subscribe Lambda (ADR-0002 A).",
        )
        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SubscribersTableReadWrite",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
                resources=[self.table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SesSendConfirmationFromAibriefing",
                effect=iam.Effect.ALLOW,
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                # Resource must be "*", not just the sender's domain identity: when a
                # recipient address is itself a verified identity in this account (as SES
                # sandbox mode requires for every test recipient), SES's IAM check also
                # authorizes against that recipient identity's ARN, not only the sender's.
                # The FromAddress condition below remains the real security boundary — it
                # still restricts sending to exactly SUBSCRIBER_SENDER regardless of Resource.
                resources=["*"],
                conditions={"StringEquals": {"ses:FromAddress": SUBSCRIBER_SENDER}},
            )
        )
        fn = _lambda.Function(
            self,
            "SubscribeFunction",
            function_name="brief-subscribers-subscribe",
            role=role,
            **self._base_function_kwargs("subscribe"),
        )
        return fn

    def _build_confirm_function(self) -> _lambda.Function:
        role = iam.Role(
            self,
            "ConfirmFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the confirm Lambda (ADR-0002 A).",
        )
        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SubscribersTableReadUpdate",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[self.table.table_arn],
            )
        )
        fn = _lambda.Function(
            self,
            "ConfirmFunction",
            function_name="brief-subscribers-confirm",
            role=role,
            **self._base_function_kwargs("confirm"),
        )
        return fn

    def _build_unsubscribe_function(self) -> _lambda.Function:
        role = iam.Role(
            self,
            "UnsubscribeFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the unsubscribe Lambda (ADR-0002 A).",
        )
        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SubscribersTableReadUpdate",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[self.table.table_arn],
            )
        )
        fn = _lambda.Function(
            self,
            "UnsubscribeFunction",
            function_name="brief-subscribers-unsubscribe",
            role=role,
            **self._base_function_kwargs("unsubscribe"),
        )
        return fn

    # ------------------------------------------------------------------
    # API — HTTP API front door (ADR-0001): POST /subscribe, GET /confirm, GET /unsubscribe
    # ------------------------------------------------------------------
    def _build_http_api(self) -> apigwv2.HttpApi:
        allowed_origin = f"https://{self.subscribe_domain_name or DEFAULT_SUBSCRIBE_DOMAIN}"

        http_api = apigwv2.HttpApi(
            self,
            "SubscribersHttpApi",
            api_name="brief-subscribers-api",
            description="Public subscribe/confirm/unsubscribe front door for the daily AI brief.",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[allowed_origin],
                allow_methods=[apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.GET],
                allow_headers=["Content-Type"],
                max_age=Duration.hours(1),
            ),
            create_default_stage=False,
        )

        # Explicit stage (not the "$default, auto-deployed" shortcut) so throttling is
        # configured deliberately rather than left at the HTTP API's own defaults.
        apigwv2.HttpStage(
            self,
            "SubscribersHttpApiStage",
            http_api=http_api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigwv2.ThrottleSettings(rate_limit=10, burst_limit=20),
        )

        http_api.add_routes(
            path="/subscribe",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "SubscribeIntegration", handler=self.subscribe_fn
            ),
        )
        http_api.add_routes(
            path="/confirm",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "ConfirmIntegration", handler=self.confirm_fn
            ),
        )
        http_api.add_routes(
            path="/unsubscribe",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "UnsubscribeIntegration", handler=self.unsubscribe_fn
            ),
        )

        cdk.CfnOutput(
            self,
            "HttpApiUrl",
            value=http_api.api_endpoint,
            description="Temporary execute-api base URL; wire the site's API_BASE_URL to this until custom-domain DNS is attached.",
        )
        return http_api

    # ------------------------------------------------------------------
    # Static site — private S3 bucket + CloudFront with Origin Access Control (ADR-0001)
    # ------------------------------------------------------------------
    def _build_static_site(self):
        bucket = s3.Bucket(
            self,
            "SubscribeSiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Custom domain + ACM cert are included here per ADR-0001, but actually attaching
        # DNS is a manual runbook step (deploy/subscribers/README.md) — this sandbox does
        # not have DNS access, so `certificateArn`/`subscribeDomainName` context values
        # are optional and the distribution falls back to its default *.cloudfront.net
        # domain when they are not supplied.
        certificate = None
        domain_names = None
        if self.certificate_arn and self.subscribe_domain_name:
            certificate = acm.Certificate.from_certificate_arn(
                self, "SubscribeSiteCertificate", self.certificate_arn
            )
            domain_names = [self.subscribe_domain_name]

        distribution = cloudfront.Distribution(
            self,
            "SubscribeSiteDistribution",
            default_root_object="index.html",
            domain_names=domain_names,
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            error_responses=[
                # Unknown paths fall back to the subscribe page rather than S3's raw XML
                # 404/403 body (this is a small static site with no client-side router, so
                # there is no dedicated "not found" page to send them to instead).
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
            "SubscribeSiteDeployment",
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
            description="Reachable now at this *.cloudfront.net domain; briefing.mschweier.com requires the manual DNS step in deploy/subscribers/README.md.",
        )
        return bucket, distribution
