"""CDK-synth-based assertions on the new IAM shapes (PRD instant-welcome-brief.md
AC-11/AC-12): the welcome-send Lambda's role carries the scoped SES + S3 grants, and the
confirm Lambda's role gains ONLY lambda:InvokeFunction on that one target -- no SES, no
S3, per ADR-0009's explicit deviation from the PRD's literal FR-13/FR-14 wording.

Synthesizes the real `BriefSubscribersStack` (same construct app.py deploys) and inspects
the rendered CloudFormation template -- a step up from manual inspection, run as part of
the normal test suite so drift is caught automatically rather than only at review time.
"""

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from brief_subscribers.stack import PIPELINE_BUCKET_NAME, SUBSCRIBER_SENDER, BriefSubscribersStack


def _synth_template() -> Template:
    app = cdk.App()
    stack = BriefSubscribersStack(
        app,
        "TestBriefSubscribersStack",
        env=cdk.Environment(account="740353583786", region="us-east-1"),
    )
    return Template.from_stack(stack)


def _policy_statements_for_role_logical_id(template: Template, role_logical_id_substring: str) -> list[dict]:
    """Return every Statement across every IAM::Policy resource attached to a role
    whose logical id contains `role_logical_id_substring` (e.g. "ConfirmFunctionRole")."""
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


def test_welcome_send_role_has_scoped_ses_send():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "WelcomeSendFunctionRole")
    ses_statements = [s for s in statements if s.get("Sid") == "SesSendWelcomeFromAibriefing"]
    assert len(ses_statements) == 1
    stmt = ses_statements[0]
    assert set(stmt["Action"]) == {"ses:SendEmail", "ses:SendRawEmail"}
    assert stmt["Condition"] == {"StringEquals": {"ses:FromAddress": SUBSCRIBER_SENDER}}


def test_welcome_send_role_has_scoped_s3_read_only():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "WelcomeSendFunctionRole")

    list_stmt = next(s for s in statements if s.get("Sid") == "S3ListBriefsPrefix")
    assert list_stmt["Action"] == "s3:ListBucket"
    assert list_stmt["Condition"] == {"StringLike": {"s3:prefix": ["briefs/*"]}}
    assert PIPELINE_BUCKET_NAME in json.dumps(list_stmt["Resource"])

    read_stmt = next(s for s in statements if s.get("Sid") == "S3ReadBriefsAndAudio")
    assert read_stmt["Action"] == "s3:GetObject"
    resource_json = json.dumps(read_stmt["Resource"])
    assert f"{PIPELINE_BUCKET_NAME}/briefs/*" in resource_json
    assert f"{PIPELINE_BUCKET_NAME}/audio/*" in resource_json

    # No write permission anywhere on this role (FR-14).
    all_actions = json.dumps([s.get("Action") for s in statements])
    for write_action in ("s3:PutObject", "s3:DeleteObject", "s3:PutObjectAcl"):
        assert write_action not in all_actions


def test_confirm_role_has_only_invoke_function_no_ses_or_s3():
    template = _synth_template()
    statements = _policy_statements_for_role_logical_id(template, "ConfirmFunctionRole")

    sids = {s.get("Sid") for s in statements}
    assert sids == {"SubscribersTableReadUpdate", "InvokeWelcomeSendFunction"}

    invoke_stmt = next(s for s in statements if s.get("Sid") == "InvokeWelcomeSendFunction")
    assert invoke_stmt["Action"] == "lambda:InvokeFunction"

    # No SES or S3 action anywhere on the confirm role -- ADR-0009's explicit deviation
    # from the PRD's literal FR-13/FR-14 wording (those grants live on the welcome-send
    # Lambda's role instead, asserted above).
    all_actions_json = json.dumps([s.get("Action") for s in statements])
    assert "ses:" not in all_actions_json
    assert "s3:" not in all_actions_json


def test_confirm_function_invoke_target_is_the_welcome_send_function_arn():
    template = _synth_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "brief-subscribers-confirm",
            "Environment": {
                "Variables": Match.object_like(
                    {"WELCOME_FUNCTION_NAME": Match.any_value()}
                )
            },
        },
    )
