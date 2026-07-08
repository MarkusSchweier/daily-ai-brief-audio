"""Regression tests for the email-chrome config guard (fix/feedback-header-loss,
2026-07-08 incident): a `cdk deploy` run without the -c context flags silently
reset FEEDBACK_TOKEN_SECRET_ARN / FEEDBACK_BASE_URL / SUBSCRIBERS_API_BASE_URL to
"" on the live delivery Lambda -- the first decoupled production send then went
out with no feedback link on any copy and BROKEN (relative-URL) unsubscribe links
on every subscriber copy. Two defenses under test here:

1. `deploy/delivery/cdk.json` carries COMMITTED, non-empty, well-shaped defaults
   (the CDK CLI reads them on every deploy; a -c flag still overrides).
2. `BriefDeliveryStack` FAILS LOUD at synth when any chrome value resolves empty,
   unless `allowEmptyChromeConfig` is explicitly set (tests/bootstrap only).
"""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from brief_delivery.stack import BriefDeliveryStack

_CDK_JSON = Path(__file__).resolve().parent.parent / "cdk.json"

_CHROME_CONTEXT = {
    "subscribersApiBaseUrl": "https://2il2bs0iq4.execute-api.us-east-1.amazonaws.com",
    "feedbackTokenSecretArn": "arn:aws:secretsmanager:us-east-1:740353583786:secret:daily-ai-brief/feedback-token-signing-secret-EsDW6s",
    "feedbackBaseUrl": "https://feedback.mschweier.com",
}


def _synth(context: dict) -> Template:
    app = cdk.App(context=context)
    stack = BriefDeliveryStack(
        app,
        "TestBriefDeliveryStack",
        env=cdk.Environment(account="740353583786", region="us-east-1"),
    )
    return Template.from_stack(stack)


# --- Defense 1: the committed cdk.json defaults -------------------------------------


def test_cdk_json_carries_nonempty_chrome_defaults():
    """The CLI-side defense: cdk.json must carry all three chrome values, non-empty
    and well-shaped, so a plain `cdk deploy` (no -c flags) can never again reset
    the live Lambda's chrome env to empty strings."""
    context = json.loads(_CDK_JSON.read_text())["context"]
    assert context["feedbackTokenSecretArn"].startswith(
        "arn:aws:secretsmanager:us-east-1:740353583786:secret:daily-ai-brief/feedback-token-signing-secret"
    )
    assert context["feedbackBaseUrl"].startswith("https://")
    assert context["subscribersApiBaseUrl"].startswith("https://")


def test_chrome_context_lands_in_the_lambda_environment():
    """The three context values must flow through to the deliver Lambda's env --
    the exact wiring whose silent reset caused the incident."""
    template = _synth(_CHROME_CONTEXT)
    functions = template.find_resources("AWS::Lambda::Function")
    deliver_envs = [
        r["Properties"]["Environment"]["Variables"]
        for r in functions.values()
        if "deliver" in json.dumps(r.get("Properties", {}).get("FunctionName", "")).lower()
        or "DELIVERIES_TABLE_NAME" in json.dumps(r.get("Properties", {}).get("Environment", {}))
    ]
    assert deliver_envs, "deliver Lambda not found in synthesized template"
    env = deliver_envs[0]
    assert env["FEEDBACK_TOKEN_SECRET_ARN"] == _CHROME_CONTEXT["feedbackTokenSecretArn"]
    assert env["FEEDBACK_BASE_URL"] == _CHROME_CONTEXT["feedbackBaseUrl"]
    assert env["SUBSCRIBERS_API_BASE_URL"] == _CHROME_CONTEXT["subscribersApiBaseUrl"]


# --- Defense 2: the fail-loud synth guard --------------------------------------------


def test_synth_fails_loud_when_all_chrome_context_is_absent():
    with pytest.raises(ValueError) as excinfo:
        _synth({})
    msg = str(excinfo.value)
    # Names every missing key and points at the fix.
    assert "subscribersApiBaseUrl" in msg
    assert "feedbackTokenSecretArn" in msg
    assert "feedbackBaseUrl" in msg
    assert "cdk.json" in msg


def test_synth_fails_loud_when_one_chrome_value_is_empty():
    context = {**_CHROME_CONTEXT, "feedbackBaseUrl": ""}
    with pytest.raises(ValueError) as excinfo:
        _synth(context)
    msg = str(excinfo.value)
    assert "feedbackBaseUrl" in msg
    assert "subscribersApiBaseUrl" not in msg  # only the ACTUALLY missing key is named


def test_escape_hatch_allows_empty_chrome_for_tests_and_bootstrap():
    template = _synth({"allowEmptyChromeConfig": True})
    # Synthesizes -- and the env carries the (deliberately) empty strings.
    functions = template.find_resources("AWS::Lambda::Function")
    envs = [
        r["Properties"]["Environment"]["Variables"]
        for r in functions.values()
        if "DELIVERIES_TABLE_NAME" in json.dumps(r.get("Properties", {}).get("Environment", {}))
    ]
    assert envs and envs[0]["FEEDBACK_BASE_URL"] == ""
