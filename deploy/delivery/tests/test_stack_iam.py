"""CDK-synth-based assertions on the delivery stack's IAM shape (PRD
docs/prd/agent-system-redesign.md FR-1/FR-3, ADR-0014 Decision 2a): the delivery
Lambda's role holds EXACTLY today's `MicroVmExecutionRole` delivery grants (moved,
not duplicated, not broadened) plus the new self-invoke/tracking-table grants this
phase adds -- nothing more.

Synthesizes the real `BriefDeliveryStack` (same construct app.py deploys) and
inspects the rendered CloudFormation template, mirroring
deploy/eval/tests/test_stack_iam.py's exact pattern.
"""

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from brief_delivery.stack import DELIVERY_BEARER_SECRET_NAME, BriefDeliveryStack


def _synth_template(context: dict | None = None) -> Template:
    app = cdk.App(context=context or {})
    stack = BriefDeliveryStack(
        app,
        "TestBriefDeliveryStack",
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


# --- The delivery Lambda's role: exactly the intended Sids, nothing broader -----------


def test_deliver_function_role_has_exactly_the_expected_sids():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "DeliverFunctionRole")

    sids = {s.get("Sid") for s in statements}
    assert sids == {
        "DeliveriesTableAccess",
        "ReadDeliveryBearerSecret",
        "PollySynthesis",
        "S3AudioReadWrite",
        "S3ListBriefsPrefix",
        "SesSendFromMschweier",
        "DynamoDBSubscribersQuery",
        "SelfInvokeForAsyncDeliveryWorker",
    }


def test_deliver_function_role_never_gains_read_environment_key_sid():
    """ReadEnvironmentKey is the microVM's OWN worker-auth secret grant -- not
    applicable to this Lambda at all (ADR-0014 Decision 2a's explicit note on what
    is NOT copied from MicroVmExecutionRole). Its presence here would indicate an
    accidental over-copy."""
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "DeliverFunctionRole")
    sids = {s.get("Sid") for s in statements}
    assert "ReadEnvironmentKey" not in sids


def test_ses_send_is_gated_by_from_address_condition_never_broadened():
    """The SINGLE most important least-privilege check in this stack: SES send
    must be conditioned on ses:FromAddress == aibriefing@mschweier.com, exactly as
    today's live grant is -- never a bare, unconditional ses:SendRawEmail."""
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "DeliverFunctionRole")

    ses_stmt = next(s for s in statements if s.get("Sid") == "SesSendFromMschweier")
    actions = ses_stmt["Action"] if isinstance(ses_stmt["Action"], list) else [ses_stmt["Action"]]
    assert set(actions) == {"ses:SendEmail", "ses:SendRawEmail"}
    assert ses_stmt["Condition"]["StringEquals"]["ses:FromAddress"] == "aibriefing@mschweier.com"


def test_dynamodb_subscribers_grant_is_query_only_scoped_to_status_index_gsi():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "DeliverFunctionRole")

    stmt = next(s for s in statements if s.get("Sid") == "DynamoDBSubscribersQuery")
    actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
    assert actions == ["dynamodb:Query"]
    resource_json = json.dumps(stmt["Resource"])
    assert "brief-subscribers" in resource_json
    assert "status-index" in resource_json


def test_s3_grant_scoped_to_prefixes_no_broader_bucket_wildcard_actions():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "DeliverFunctionRole")

    rw_stmt = next(s for s in statements if s.get("Sid") == "S3AudioReadWrite")
    actions = rw_stmt["Action"] if isinstance(rw_stmt["Action"], list) else [rw_stmt["Action"]]
    assert set(actions) == {"s3:PutObject", "s3:GetObject"}
    assert "s3:DeleteObject" not in actions

    list_stmt = next(s for s in statements if s.get("Sid") == "S3ListBriefsPrefix")
    assert list_stmt["Condition"]["StringLike"]["s3:prefix"] == ["briefs/*"]


def test_deliveries_table_grant_has_no_scan_or_delete():
    """This role only ever get/put/updates a SINGLE known deliveryId (no listing
    access pattern exists for this Lambda) -- Scan/DeleteItem would be broader
    than needed."""
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "DeliverFunctionRole")

    stmt = next(s for s in statements if s.get("Sid") == "DeliveriesTableAccess")
    actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
    assert set(actions) == {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"}
    assert "dynamodb:Scan" not in actions
    assert "dynamodb:DeleteItem" not in actions


def test_self_invoke_grant_is_scoped_to_the_functions_own_arn_not_a_wildcard():
    """The self-invoke grant must be scoped to THIS function's own literal-named
    ARN, never a bare `*` across all Lambda functions in the account -- least
    privilege for a capability that, if broadened, would let this role invoke ANY
    Lambda in the account.

    The resource is built from the LITERAL `DELIVER_FUNCTION_NAME` string
    (`brief-delivery-deliver`), not a `Fn::GetAtt`/`Ref` to the Function's CDK
    logical id -- see `_grant_self_invoke()`'s docstring: using a `Ref`/`Fn::GetAtt`
    here creates a genuine CloudFormation Role<->Function dependency cycle,
    confirmed live via this exact test suite before the fix."""
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "DeliverFunctionRole")

    stmt = next(s for s in statements if s.get("Sid") == "SelfInvokeForAsyncDeliveryWorker")
    actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
    assert actions == ["lambda:InvokeFunction"]
    resource_json = json.dumps(stmt["Resource"])
    assert resource_json != '"*"'
    assert "brief-delivery-deliver" in resource_json
    # Confirm this is genuinely NOT a Ref/Fn::GetAtt to the Function's own CDK
    # logical id (which would reintroduce the cycle) -- the resource must be
    # scoped by the literal function NAME only.
    assert "DeliverFunction0" not in resource_json


def test_no_role_in_this_stack_has_dynamodb_scan_on_the_subscribers_table():
    """A stronger, whole-template check: no policy statement anywhere grants Scan
    against the brief-subscribers table -- Query-only is the invariant this whole
    IAM design rests on (docs/adr/0002 §B)."""
    template = _synth_template()
    for resource in template.find_resources("AWS::IAM::Policy").values():
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            resource_json = json.dumps(statement.get("Resource"))
            if "brief-subscribers" in resource_json:
                action = statement.get("Action")
                actions = action if isinstance(action, list) else [action]
                assert "dynamodb:Scan" not in actions


def test_feedback_token_secret_grant_absent_by_default():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "DeliverFunctionRole")
    sids = {s.get("Sid") for s in statements}
    assert "ReadFeedbackTokenSecret" not in sids


def test_feedback_token_secret_grant_present_when_context_supplied():
    template = _synth_template(
        context={"feedbackTokenSecretArn": "arn:aws:secretsmanager:us-east-1:740353583786:secret:feedback-token-xxxxx"}
    )
    statements = _policy_statements_for_role_logical_id(template, "DeliverFunctionRole")
    sids = {s.get("Sid") for s in statements}
    assert "ReadFeedbackTokenSecret" in sids

    stmt = next(s for s in statements if s.get("Sid") == "ReadFeedbackTokenSecret")
    assert stmt["Action"] == "secretsmanager:GetSecretValue"
    resource_json = json.dumps(stmt["Resource"])
    assert "feedback-token-xxxxx" in resource_json


# --- Data resources ---------------------------------------------------------------------


def test_deliveries_table_shape():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "brief-deliveries",
            "KeySchema": [{"AttributeName": "deliveryId", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
    )
    resources = template.find_resources("AWS::DynamoDB::Table")
    (table_props,) = [r["Properties"] for r in resources.values()]
    assert "GlobalSecondaryIndexes" not in table_props


def test_deliveries_table_and_secret_are_retained_not_destroyed():
    template = _synth_template()
    template.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"})
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    assert len(secrets) == 1
    for resource in secrets.values():
        assert resource.get("DeletionPolicy") == "Retain"


def test_no_secret_has_an_explicit_secret_string_value():
    template = _synth_template()
    resources = template.find_resources("AWS::SecretsManager::Secret")
    for resource in resources.values():
        assert "SecretString" not in resource["Properties"]


def test_delivery_bearer_secret_is_the_expected_name():
    template = _synth_template()
    resources = template.find_resources("AWS::SecretsManager::Secret")
    names = {r["Properties"].get("Name") for r in resources.values()}
    assert names == {DELIVERY_BEARER_SECRET_NAME}


def test_no_static_access_keys_anywhere():
    template = _synth_template()
    template_json = json.dumps(template.to_json())
    assert "AWS::IAM::AccessKey" not in template_json


# --- HTTP API ----------------------------------------------------------------------------


def test_expected_routes_exist():
    template = _synth_template()
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {r["Properties"]["RouteKey"] for r in routes.values()}
    assert route_keys == {"POST /deliver", "GET /deliver/{deliveryId}"}


def test_http_api_stage_is_throttled():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {"DefaultRouteSettings": Match.object_like({"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20})},
    )


def test_deliver_function_is_python313_arm64():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"FunctionName": "brief-delivery-deliver", "Runtime": "python3.13", "Architectures": ["arm64"]},
    )


def test_deliver_function_timeout_exceeds_pollys_own_five_minute_allowance():
    """The Lambda's own timeout must comfortably exceed Polly's existing 5-minute
    synthesis allowance PLUS the SES fan-out loop after it (ADR-0014 Decision 2a's
    "Why async" note: set generously above the real runtime, e.g. 10 minutes)."""
    template = _synth_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"FunctionName": "brief-delivery-deliver", "Timeout": Match.any_value()},
    )
    functions = template.find_resources("AWS::Lambda::Function")
    (deliver_fn_props,) = [
        r["Properties"] for r in functions.values() if r["Properties"].get("FunctionName") == "brief-delivery-deliver"
    ]
    assert deliver_fn_props["Timeout"] >= 600  # >= 10 minutes, comfortably over Polly's 300s


def test_deliver_function_env_includes_its_own_function_name_for_self_invoke():
    template = _synth_template()
    functions = template.find_resources("AWS::Lambda::Function")
    (deliver_fn_props,) = [
        r["Properties"] for r in functions.values() if r["Properties"].get("FunctionName") == "brief-delivery-deliver"
    ]
    env_vars = deliver_fn_props["Environment"]["Variables"]
    assert "DELIVERY_FUNCTION_NAME" in env_vars
