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
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
)
from constructs import Construct

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"
LAYERS_DIR = Path(__file__).resolve().parent.parent / "layers"

SUBSCRIBER_SENDER = "aibriefing@mschweier.com"
SES_IDENTITY_DOMAIN = "mschweier.com"


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
            point_in_time_recovery=True,
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
                resources=[
                    f"arn:{self.partition}:ses:{self.region}:{self.account}:identity/{SES_IDENTITY_DOMAIN}"
                ],
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
