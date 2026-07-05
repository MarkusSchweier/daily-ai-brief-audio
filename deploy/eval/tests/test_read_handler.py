"""Unit tests for functions/read/handler.py (PRD FR-18/FR-24), using moto."""

import importlib.util
import sys
from pathlib import Path

import boto3
import pytest

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"
READ_DIR = FUNCTIONS_DIR / "read"

sys.path.insert(0, str(READ_DIR))


def _import_handler():
    module_name = "read_handler_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, READ_DIR / "handler.py")
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
    table.put_item(Item={"runId": "run_1", "status": "pending", "candidateConfigId": "production", "createdAt": 100})
    table.put_item(Item={"runId": "run_2", "status": "complete", "candidateConfigId": "production", "createdAt": 200})
    table.put_item(Item={"runId": "run_3", "status": "complete", "candidateConfigId": "candidate_b", "createdAt": 300})
    yield table


def _event(route, path_params=None, query_params=None):
    return {
        "requestContext": {"routeKey": route},
        "pathParameters": path_params or {},
        "queryStringParameters": query_params or {},
    }


def test_list_all_runs(eval_table):
    handler_module = _import_handler()
    result = handler_module._handle(_event("GET /runs"), eval_table)

    import json

    body = json.loads(result["body"])
    assert body["ok"] is True
    assert len(body["runs"]) == 3


def test_list_runs_filtered_by_status(eval_table):
    handler_module = _import_handler()
    result = handler_module._handle(_event("GET /runs", query_params={"status": "pending"}), eval_table)

    import json

    body = json.loads(result["body"])
    assert len(body["runs"]) == 1
    assert body["runs"][0]["runId"] == "run_1"


def test_get_one_run_detail(eval_table):
    handler_module = _import_handler()
    result = handler_module._handle(_event("GET /runs/{runId}", path_params={"runId": "run_2"}), eval_table)

    import json

    body = json.loads(result["body"])
    assert body["ok"] is True
    assert body["run"]["runId"] == "run_2"


def test_get_unknown_run_returns_404(eval_table):
    handler_module = _import_handler()
    result = handler_module._handle(_event("GET /runs/{runId}", path_params={"runId": "no_such_run"}), eval_table)

    assert result["statusCode"] == 404


def test_candidates_view_groups_completed_runs_by_candidate(eval_table):
    handler_module = _import_handler()
    result = handler_module._handle(_event("GET /candidates"), eval_table)

    import json

    body = json.loads(result["body"])
    assert body["ok"] is True
    assert set(body["candidates"].keys()) == {"production", "candidate_b"}
    # Only the COMPLETE production run should appear (run_1 is pending, excluded).
    assert len(body["candidates"]["production"]) == 1
    assert body["candidates"]["production"][0]["runId"] == "run_2"
