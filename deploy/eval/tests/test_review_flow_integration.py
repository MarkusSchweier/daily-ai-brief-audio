"""Integration test spanning trigger -> poll (simulated completion) -> submit-review
-> read (PRD FR-1/FR-16/FR-19, AC-19), using moto for DynamoDB/S3.

This is the end-to-end regression proof for FIX 2: a reviewer's submitted override
must actually be visible in the READ path's response under
`record.human_overrides`, not silently lost to a sibling attribute nothing reads.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import boto3
import pytest

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"


def _import_handler(name: str, dirname: str):
    module_name = f"{name}_handler_under_test_integration"
    if module_name in sys.modules:
        return sys.modules[module_name]
    handler_dir = FUNCTIONS_DIR / dirname
    sys.path.insert(0, str(handler_dir))
    spec = importlib.util.spec_from_file_location(module_name, handler_dir / "handler.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, json_body=None):
        self._json_body = json_body or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_body


class _FakeDeploymentsClient:
    def __init__(self):
        self.posts = []

    def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        if path == "/v1/deployments":
            return _FakeResponse({"id": "depl_temp123"})
        if path.endswith("/run"):
            return _FakeResponse({"id": "drun_new789", "session_id": "sesn_new456"})
        if path.endswith("/archive"):
            return _FakeResponse({"ok": True})
        raise AssertionError(f"unexpected POST {path}")

    def get(self, path, **kwargs):
        return _FakeResponse({"status": "complete"})


class _FakeMessagesResource:
    def create(self, **kwargs):
        class _Block:
            type = "text"
            text = json.dumps({"score": 4, "rationale": "fine", "evidence": "ev", "insufficient_data": False})

        class _Msg:
            content = [_Block()]

        return _Msg()


class _FakeJudgeClient:
    def __init__(self):
        self.messages = _FakeMessagesResource()


def _bearer_event(body=None, path_params=None, query_params=None, route=""):
    event = {"headers": {"Authorization": "Bearer secret123"}}
    if body is not None:
        event["body"] = json.dumps(body)
    if path_params is not None:
        event["pathParameters"] = path_params
    if query_params is not None:
        event["queryStringParameters"] = query_params
    if route:
        event["requestContext"] = {"routeKey": route}
    return event


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
    yield table


@pytest.fixture
def pipeline_bucket(mocked_aws):
    poll_module = _import_handler("poll", "poll")
    bucket_name = "cowork-polly-tts-740353583786-integration-test"
    poll_module.PIPELINE_BUCKET_NAME = bucket_name
    s3_client = boto3.client("s3", region_name="us-east-1")
    s3_client.create_bucket(Bucket=bucket_name)
    s3_client.put_object(Bucket=bucket_name, Key="briefs/2026-07-04/brief.md", Body=b"# Today's brief\n\nSome content.")
    s3_client.put_object(Bucket=bucket_name, Key="briefs/2026-07-04/listening-script.txt", Body=b"Listening script.")
    yield s3_client


@pytest.fixture(autouse=True)
def _fake_cost_miner(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from eval_core import cost_miner

    def _fake_fetch_session_cost(anthropic_api_key, session_id, *, base_url=None):
        usage = cost_miner.TokenUsage(input_tokens=100, output_tokens=50, cache_creation_input_tokens=10, cache_read_input_tokens=1000)
        return cost_miner.SessionCostBreakdown(
            session_id=session_id,
            total_cost_usd=1.23,
            total_usage=usage,
            threads=(cost_miner.ThreadCost(thread_id="thread_1", usage=usage, cost_usd=1.23),),
            phase_totals=(cost_miner.PhaseCost(phase="research", usage=usage, cost_usd=0.5),),
        )

    monkeypatch.setattr(cost_miner, "fetch_session_cost", _fake_fetch_session_cost)
    yield


def test_trigger_then_poll_then_submit_review_then_read_round_trip(eval_table, pipeline_bucket):
    trigger_module = _import_handler("trigger", "trigger")
    poll_module = _import_handler("poll", "poll")
    submit_review_module = _import_handler("submit-review", "submit-review")
    read_module = _import_handler("read", "read")

    # 1) Trigger: creates a pending row (a real trigger call, exercising the real
    #    handler, per the task's "reusing your Fix 1 test helper" instruction).
    deployments_client = _FakeDeploymentsClient()
    trigger_result = trigger_module._handle(_bearer_event({"candidateConfigId": "production"}), eval_table, deployments_client)
    run_id = json.loads(trigger_result["body"])["runId"]

    # 2) Poll: simulates the session completing and processes the run into a
    #    persisted structured record (this is where FIX 1's reserved-keyword bug
    #    would have thrown, and where FIX 3/4's content/calibration are populated).
    poll_result = poll_module._handle(
        eval_table, pipeline_bucket, boto3.resource("dynamodb", region_name="us-east-1"), deployments_client, _FakeJudgeClient(), "fake-api-key"
    )
    assert poll_result["processed"] == 1

    # 3) Submit a reviewer override.
    submit_result = submit_review_module._handle(
        _bearer_event({"runId": run_id, "criterion": "content_selection", "agreed": False, "overriddenScore": 2, "comment": "missed a story"}),
        eval_table,
    )
    assert submit_result["statusCode"] == 200

    # 4) Read: the override must be visible under record.human_overrides -- this is
    #    the exact shape site/app.js's openDetail()/loadCompareView() read from.
    read_result = read_module._handle(_bearer_event(path_params={"runId": run_id}, route="GET /runs/{runId}"), eval_table)
    body = json.loads(read_result["body"])
    stored_record = json.loads(body["run"]["record"])

    override = stored_record["human_overrides"]["content_selection"]
    assert override["overridden_score"] == 2
    assert override["comment"] == "missed a story"
    assert override["agreed"] is False

    # And the sibling humanOverrides attribute the old, broken write path used must
    # never appear at all.
    assert "humanOverrides" not in body["run"]
