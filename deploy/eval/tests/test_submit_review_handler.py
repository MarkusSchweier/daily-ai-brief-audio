"""Unit tests for functions/submit-review/handler.py (PRD FR-19/AC-19), using moto."""

import importlib.util
import sys
from pathlib import Path

import boto3
import pytest

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"
SUBMIT_REVIEW_DIR = FUNCTIONS_DIR / "submit-review"

sys.path.insert(0, str(SUBMIT_REVIEW_DIR))


def _import_handler():
    module_name = "submit_review_handler_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, SUBMIT_REVIEW_DIR / "handler.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def eval_table(mocked_aws):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="brief-eval-records-test",
        KeySchema=[{"AttributeName": "runId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "runId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    table.put_item(Item={"runId": "run_1", "status": "complete", "candidateConfigId": "production"})
    yield table


def _event(body, with_bearer="secret123"):
    headers = {"Authorization": f"Bearer {with_bearer}"} if with_bearer else {}
    import json

    return {"headers": headers, "body": json.dumps(body)}


def test_submit_review_agree_persists_override(eval_table):
    handler_module = _import_handler()

    result = handler_module._handle(
        _event({"runId": "run_1", "criterion": "content_selection", "agreed": True, "comment": "looks right"}),
        eval_table,
    )

    assert result["statusCode"] == 200
    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    assert item["humanOverrides"]["content_selection"]["agreed"] is True
    assert item["humanOverrides"]["content_selection"]["comment"] == "looks right"


def test_submit_review_override_persists_new_score(eval_table):
    handler_module = _import_handler()

    result = handler_module._handle(
        _event({"runId": "run_1", "criterion": "length_format", "agreed": False, "overriddenScore": 2, "comment": "too long"}),
        eval_table,
    )

    assert result["statusCode"] == 200
    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    assert item["humanOverrides"]["length_format"]["overridden_score"] == 2


def test_submit_review_rejects_out_of_range_score(eval_table):
    handler_module = _import_handler()

    result = handler_module._handle(
        _event({"runId": "run_1", "criterion": "length_format", "agreed": False, "overriddenScore": 9}),
        eval_table,
    )

    assert result["statusCode"] == 400


def test_submit_review_rejects_missing_run_id(eval_table):
    handler_module = _import_handler()

    result = handler_module._handle(_event({"criterion": "length_format", "agreed": True}), eval_table)

    assert result["statusCode"] == 400


def test_submit_review_404_for_unknown_run(eval_table):
    handler_module = _import_handler()

    result = handler_module._handle(_event({"runId": "no_such_run", "criterion": "dedup", "agreed": True}), eval_table)

    assert result["statusCode"] == 404


def test_second_criterion_override_does_not_clobber_the_first(eval_table):
    handler_module = _import_handler()

    handler_module._handle(_event({"runId": "run_1", "criterion": "content_selection", "agreed": True}), eval_table)
    handler_module._handle(_event({"runId": "run_1", "criterion": "dedup", "agreed": False, "overriddenScore": 3}), eval_table)

    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    assert "content_selection" in item["humanOverrides"]
    assert "dedup" in item["humanOverrides"]
