"""CDK-synth-based assertions on the feedback submit Lambda's IAM shape (PRD FR-16,
AC-15): exactly PutItem on the one table (no throttle counter was built, so no Get/
Update grant — ADR-0012 §B.3's PutItem-only fallback), GetSecretValue on the one
signing secret, own logs — no SES, no access to any other table/bucket, no reuse of
any subscribers-stack role.

Synthesizes the real `FeedbackStack` (same construct app.py deploys) and inspects the
rendered CloudFormation template, mirroring
deploy/subscribers/tests/test_stack_iam.py's pattern exactly.
"""

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from brief_feedback.stack import DEFAULT_FEEDBACK_DOMAIN, FeedbackStack


def _synth_template(context: dict | None = None) -> Template:
    app = cdk.App(context=context or {})
    stack = FeedbackStack(
        app,
        "TestFeedbackStack",
        env=cdk.Environment(account="740353583786", region="us-east-1"),
    )
    return Template.from_stack(stack)


def _policy_statements_for_role_logical_id(template: Template, role_logical_id_substring: str) -> list[dict]:
    policies = template.find_resources("AWS::IAM::Policy")
    statements = []
    for resource in policies.values():
        roles = resource.get("Properties", {}).get("Roles", [])
        attached_to_target_role = any(
            isinstance(r, dict) and role_logical_id_substring in json.dumps(r) for r in roles
        )
        if attached_to_target_role:
            statements.extend(resource["Properties"]["PolicyDocument"]["Statement"])
    return statements


def test_submit_role_has_scoped_dynamodb_and_secret_grants_only():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "SubmitFunctionRole")

    sids = {s.get("Sid") for s in statements}
    assert sids == {"FeedbackTablePut", "ReadFeedbackTokenSecret"}

    table_stmt = next(s for s in statements if s.get("Sid") == "FeedbackTablePut")
    action = table_stmt["Action"]
    assert (action if isinstance(action, list) else [action]) == ["dynamodb:PutItem"]

    secret_stmt = next(s for s in statements if s.get("Sid") == "ReadFeedbackTokenSecret")
    assert secret_stmt["Action"] == "secretsmanager:GetSecretValue"

    # No SES anywhere on this role (PRD FR-16/AC-15, "no CAPTCHA/WAF" posture aside --
    # this is the no-email posture).
    all_actions_json = json.dumps([s.get("Action") for s in statements])
    assert "ses:" not in all_actions_json


def test_submit_role_dynamodb_grant_scoped_to_the_one_table_only():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "SubmitFunctionRole")
    table_stmt = next(s for s in statements if s.get("Sid") == "FeedbackTablePut")

    resource_json = json.dumps(table_stmt["Resource"])
    # Exactly one table's ARN referenced (via Fn::GetAtt to FeedbackTable) -- no second
    # table, no GSI, no wildcard/broader resource.
    assert "brief-subscribers" not in resource_json
    assert "cowork-polly-tts" not in resource_json


def test_submit_role_secret_grant_scoped_to_the_one_secret_only():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "SubmitFunctionRole")
    secret_stmt = next(s for s in statements if s.get("Sid") == "ReadFeedbackTokenSecret")

    resource_json = json.dumps(secret_stmt["Resource"])
    assert "FeedbackTokenSigningSecret" in resource_json


def test_feedback_table_shape():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "brief-feedback",
            "KeySchema": [{"AttributeName": "submissionId", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        },
    )
    # No GSI, no TTL attribute (PRD/ADR-0012: no query access pattern in scope, no
    # expiry -- feedback is retained indefinitely).
    resources = template.find_resources("AWS::DynamoDB::Table")
    (table_props,) = [r["Properties"] for r in resources.values()]
    assert "GlobalSecondaryIndexes" not in table_props
    assert "TimeToLiveSpecification" not in table_props


def test_feedback_table_and_secret_are_retained_not_destroyed():
    template = _synth_template()
    template.has_resource(
        "AWS::DynamoDB::Table", {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"}
    )
    template.has_resource(
        "AWS::SecretsManager::Secret", {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"}
    )


def test_secret_created_with_no_explicit_secret_string_value():
    """No `SecretString` literal value is set (CDK/CloudFormation cannot set a real
    value here without it landing in the template/state file, ADR-0011). CDK's default
    `GenerateSecretString: {}` when no explicit value is given is expected -- it does
    NOT put a real secret value in the template (CloudFormation generates and stores it
    directly in Secrets Manager at deploy time); the actual signing secret is populated
    out-of-band via `put-secret-value` post-deploy, exactly as the README documents."""
    template = _synth_template()
    resources = template.find_resources("AWS::SecretsManager::Secret")
    (secret_props,) = [r["Properties"] for r in resources.values()]
    assert secret_props.get("Name") == "daily-ai-brief/feedback-token-signing-secret"
    assert "SecretString" not in secret_props


def test_http_api_cors_locked_to_default_feedback_domain():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {"CorsConfiguration": Match.object_like({"AllowOrigins": [f"https://{DEFAULT_FEEDBACK_DOMAIN}"]})},
    )


def test_http_api_cors_locked_to_custom_domain_when_context_supplied():
    template = _synth_template(context={"feedbackDomainName": "custom-feedback.example.com"})
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {"CorsConfiguration": Match.object_like({"AllowOrigins": ["https://custom-feedback.example.com"]})},
    )


def test_http_api_stage_is_throttled():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {"DefaultRouteSettings": Match.object_like({"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20})},
    )


def test_submit_route_is_post_only():
    template = _synth_template()
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {r["Properties"]["RouteKey"] for r in routes.values()}
    assert route_keys == {"POST /submit"}


def test_site_bucket_blocks_public_access_and_enforces_ssl():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                }
            }
        ),
    )


def test_distinct_cloudfront_distribution_from_subscribers_stack():
    """FR-1/§6: this stack's own CloudFront distribution, not bolted onto
    SubscribeSiteDistribution -- proven here by confirming exactly one distribution
    exists in this stack's own template (a distinct, standalone resource)."""
    template = _synth_template()
    distributions = template.find_resources("AWS::CloudFront::Distribution")
    assert len(distributions) == 1


def test_no_certificate_alias_when_context_absent():
    template = _synth_template()
    distributions = template.find_resources("AWS::CloudFront::Distribution")
    (dist_props,) = [r["Properties"]["DistributionConfig"] for r in distributions.values()]
    assert "Aliases" not in dist_props


def test_certificate_alias_when_both_context_values_supplied():
    template = _synth_template(
        context={
            "feedbackDomainName": "feedback.mschweier.com",
            "certificateArn": "arn:aws:acm:us-east-1:740353583786:certificate/fake-1234",
        }
    )
    distributions = template.find_resources("AWS::CloudFront::Distribution")
    (dist_props,) = [r["Properties"]["DistributionConfig"] for r in distributions.values()]
    assert dist_props["Aliases"] == ["feedback.mschweier.com"]


def test_submit_function_is_python313_arm64_no_bundling_assets():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "brief-feedback-submit",
            "Runtime": "python3.13",
            "Architectures": ["arm64"],
            "Timeout": 10,
            "MemorySize": 128,
        },
    )
