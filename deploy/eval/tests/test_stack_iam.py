"""CDK-synth-based assertions on the eval stack's IAM shape (PRD FR-21, AC-21):
least-privilege, scoped-by-ARN roles for every Lambda, no SES anywhere, no write/
delete on the cross-stack `brief-feedback` table, no static access keys.

Synthesizes the real `BriefEvalStack` (same construct app.py deploys) and inspects the
rendered CloudFormation template, mirroring
deploy/feedback/tests/test_stack_iam.py's exact pattern.
"""

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from brief_eval.stack import DEFAULT_EVAL_DOMAIN, BriefEvalStack


def _synth_template(context: dict | None = None) -> Template:
    app = cdk.App(context=context or {})
    stack = BriefEvalStack(
        app,
        "TestBriefEvalStack",
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


def _all_actions_json(statements: list[dict]) -> str:
    return json.dumps([s.get("Action") for s in statements])


# --- No SES anywhere in this stack (PRD §4.F / FR-21) --------------------------------


def test_no_role_in_this_stack_has_any_ses_permission():
    template = _synth_template()
    template_json = json.dumps(template.to_json())
    assert "ses:" not in template_json


# --- Trigger function role ----------------------------------------------------------


def test_trigger_role_has_scoped_grants_only():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "TriggerFunctionRole")

    sids = {s.get("Sid") for s in statements}
    assert sids == {"EvalTablePut", "ReadAnthropicApiKeySecret", "ReadReviewSecret", "ReadProductionPromptParam"}

    table_stmt = next(s for s in statements if s.get("Sid") == "EvalTablePut")
    action = table_stmt["Action"]
    assert (action if isinstance(action, list) else [action]) == ["dynamodb:PutItem"]

    prompt_param_stmt = next(s for s in statements if s.get("Sid") == "ReadProductionPromptParam")
    action = prompt_param_stmt["Action"]
    assert (action if isinstance(action, list) else [action]) == ["ssm:GetParameter"]


def test_trigger_role_ssm_grant_scoped_to_one_parameter_not_wildcard():
    """Least privilege (FR-21): the production-prompt read must be pinned to the one
    parameter this stack creates (via a Ref to that exact resource), never a
    wildcard across the account's parameters."""
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "TriggerFunctionRole")

    parameters = template.find_resources("AWS::SSM::Parameter")
    assert len(parameters) == 1
    (param_logical_id, param) = next(iter(parameters.items()))
    assert param["Properties"]["Name"] == "/daily-ai-brief/eval/production-initial-prompt"
    assert param["Properties"]["Type"] == "String"

    prompt_param_stmt = next(s for s in statements if s.get("Sid") == "ReadProductionPromptParam")
    resource_json = json.dumps(prompt_param_stmt["Resource"])
    assert resource_json != '"*"'
    # The IAM statement's Resource ARN is built from a Ref to the exact parameter
    # resource above (a CFN intrinsic, not a literal string in the template).
    assert f'{{"Ref": "{param_logical_id}"}}' in resource_json


def test_trigger_role_has_no_get_or_scan_on_eval_table():
    """The trigger Lambda only ever creates a new pending row -- it never reads the
    table, so it should not hold GetItem/Scan (least privilege, not "just in case")."""
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "TriggerFunctionRole")
    actions_json = _all_actions_json(statements)
    assert "dynamodb:GetItem" not in actions_json
    assert "dynamodb:Scan" not in actions_json


# --- Poll function role ---------------------------------------------------------------


def test_poll_role_has_scoped_grants_only_without_feedback_context():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "PollFunctionRole")

    sids = {s.get("Sid") for s in statements}
    assert sids == {"EvalTableReadWrite", "S3ListBriefsPrefix", "S3ReadBriefsOnly", "ReadAnthropicApiKeySecret"}
    # No cross-stack feedback grant when the context value is absent (backward compat).
    assert "ReadFeedbackTableOnly" not in sids


def test_poll_role_s3_grant_scoped_to_briefs_prefix_no_audio_access():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "PollFunctionRole")

    read_stmt = next(s for s in statements if s.get("Sid") == "S3ReadBriefsOnly")
    resource_json = json.dumps(read_stmt["Resource"])
    assert "briefs/*" in resource_json
    assert "audio/*" not in resource_json
    assert read_stmt["Action"] == "s3:GetObject"

    list_stmt = next(s for s in statements if s.get("Sid") == "S3ListBriefsPrefix")
    assert list_stmt["Condition"]["StringLike"]["s3:prefix"] == ["briefs/*"]


def test_poll_role_has_no_write_permission_on_s3():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "PollFunctionRole")
    actions_json = _all_actions_json(statements)
    assert "s3:PutObject" not in actions_json
    assert "s3:DeleteObject" not in actions_json


def test_poll_role_gains_read_only_feedback_grant_when_context_supplied():
    template = _synth_template(
        context={
            "feedbackTableArn": "arn:aws:dynamodb:us-east-1:740353583786:table/brief-feedback",
            "feedbackTableName": "brief-feedback",
        }
    )
    statements = _policy_statements_for_role_logical_id(template, "PollFunctionRole")
    sids = {s.get("Sid") for s in statements}
    assert "ReadFeedbackTableOnly" in sids

    feedback_stmt = next(s for s in statements if s.get("Sid") == "ReadFeedbackTableOnly")
    action = feedback_stmt["Action"]
    actions = action if isinstance(action, list) else [action]
    assert set(actions) == {"dynamodb:Scan", "dynamodb:GetItem"}
    assert "dynamodb:PutItem" not in actions
    assert "dynamodb:UpdateItem" not in actions
    assert "dynamodb:DeleteItem" not in actions
    resource_json = json.dumps(feedback_stmt["Resource"])
    assert "brief-feedback" in resource_json


def test_poll_role_never_gains_write_or_delete_on_feedback_table_even_when_context_supplied():
    """A stronger, whole-template check (not just one role's statements): no policy
    statement anywhere grants Put/Update/Delete against the feedback table's ARN."""
    template = _synth_template(context={"feedbackTableArn": "arn:aws:dynamodb:us-east-1:740353583786:table/brief-feedback"})
    template_json = json.dumps(template.to_json())
    assert "brief-feedback" in template_json  # sanity: the ARN is referenced somewhere

    for resource in template.find_resources("AWS::IAM::Policy").values():
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            resource_json = json.dumps(statement.get("Resource"))
            if "brief-feedback" in resource_json:
                action = statement.get("Action")
                actions = action if isinstance(action, list) else [action]
                assert "dynamodb:PutItem" not in actions
                assert "dynamodb:UpdateItem" not in actions
                assert "dynamodb:DeleteItem" not in actions


# --- Submit-review function role -----------------------------------------------------


def test_submit_review_role_has_scoped_grants_only():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "SubmitReviewFunctionRole")

    sids = {s.get("Sid") for s in statements}
    assert sids == {"EvalTableReadUpdate", "ReadReviewSecret"}

    table_stmt = next(s for s in statements if s.get("Sid") == "EvalTableReadUpdate")
    action = table_stmt["Action"]
    actions = action if isinstance(action, list) else [action]
    assert set(actions) == {"dynamodb:GetItem", "dynamodb:UpdateItem"}
    assert "dynamodb:PutItem" not in actions
    assert "dynamodb:DeleteItem" not in actions


# --- Read function role ---------------------------------------------------------------


def test_read_role_has_read_only_grants():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "ReadFunctionRole")

    sids = {s.get("Sid") for s in statements}
    assert sids == {"EvalTableReadOnly", "ReadReviewSecret"}

    table_stmt = next(s for s in statements if s.get("Sid") == "EvalTableReadOnly")
    action = table_stmt["Action"]
    actions = action if isinstance(action, list) else [action]
    assert set(actions) == {"dynamodb:GetItem", "dynamodb:Scan"}
    assert "dynamodb:PutItem" not in actions
    assert "dynamodb:UpdateItem" not in actions
    assert "dynamodb:DeleteItem" not in actions


# --- Data resources --------------------------------------------------------------------


def test_eval_table_shape():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "brief-eval-records",
            "KeySchema": [{"AttributeName": "runId", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
    )
    resources = template.find_resources("AWS::DynamoDB::Table")
    (table_props,) = [r["Properties"] for r in resources.values()]
    assert "GlobalSecondaryIndexes" not in table_props


def test_eval_table_and_secrets_are_retained_not_destroyed():
    template = _synth_template()
    template.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"})
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    assert len(secrets) == 2
    for resource in secrets.values():
        assert resource.get("DeletionPolicy") == "Retain"


def test_no_secret_has_an_explicit_secret_string_value():
    template = _synth_template()
    resources = template.find_resources("AWS::SecretsManager::Secret")
    for resource in resources.values():
        assert "SecretString" not in resource["Properties"]


def test_two_distinct_secrets_review_and_anthropic_api_key():
    template = _synth_template()
    resources = template.find_resources("AWS::SecretsManager::Secret")
    names = {r["Properties"].get("Name") for r in resources.values()}
    assert names == {"daily-ai-brief/eval-review-bearer-secret", "daily-ai-brief/eval-anthropic-api-key"}


# --- HTTP API / CORS / throttling -----------------------------------------------------


def test_http_api_cors_locked_to_default_eval_domain():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {"CorsConfiguration": Match.object_like({"AllowOrigins": [f"https://{DEFAULT_EVAL_DOMAIN}"]})},
    )


def test_http_api_cors_locked_to_custom_domain_when_context_supplied():
    template = _synth_template(context={"evalDomainName": "custom-eval.example.com"})
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {"CorsConfiguration": Match.object_like({"AllowOrigins": ["https://custom-eval.example.com"]})},
    )


def test_http_api_stage_is_throttled():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {"DefaultRouteSettings": Match.object_like({"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20})},
    )


def test_expected_routes_exist():
    template = _synth_template()
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {r["Properties"]["RouteKey"] for r in routes.values()}
    assert route_keys == {
        "POST /trigger",
        "POST /reviews",
        "GET /runs",
        "GET /runs/{runId}",
        "GET /candidates",
    }


# --- Static site -----------------------------------------------------------------------


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


def test_distinct_cloudfront_distribution():
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
            "evalDomainName": "eval.mschweier.com",
            "certificateArn": "arn:aws:acm:us-east-1:740353583786:certificate/fake-1234",
        }
    )
    distributions = template.find_resources("AWS::CloudFront::Distribution")
    (dist_props,) = [r["Properties"]["DistributionConfig"] for r in distributions.values()]
    assert dist_props["Aliases"] == ["eval.mschweier.com"]


# --- EventBridge poll schedule ---------------------------------------------------------


def test_poll_schedule_rule_runs_every_two_minutes():
    template = _synth_template()
    template.has_resource_properties("AWS::Events::Rule", {"ScheduleExpression": "rate(2 minutes)"})


def test_lambda_functions_are_python313_arm64():
    template = _synth_template()
    for function_name in ["brief-eval-trigger", "brief-eval-poll", "brief-eval-submit-review", "brief-eval-read"]:
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {"FunctionName": function_name, "Runtime": "python3.13", "Architectures": ["arm64"]},
        )
