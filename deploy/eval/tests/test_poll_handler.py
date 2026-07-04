"""Unit tests for functions/poll/handler.py (PRD FR-1..FR-17), using moto for
DynamoDB/S3 and fake Deployments-API / judge clients (no real network call or API
key).

Regression coverage for the reviewer/security findings fixed in this pass:
  - FIX 1: the completion write used `record` as a bare (reserved-keyword)
    UpdateExpression attribute name, which throws `ValidationException` on every
    real DynamoDB call -- moto's own validation catches this the same way, so these
    tests would have failed before the `#r` alias fix.
  - FIX 3: the completion record must carry the brief markdown + listening script
    text so the review UI can render them (AC-18).
  - FIX 4: FR-15's `extract_free_text_feedback()` must be wired into the stored
    `calibration` block, not just unit-tested in isolation.
  - FIX 6: a session-failure poll cycle must still archive the temporary deployment.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import boto3
import pytest

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"
POLL_DIR = FUNCTIONS_DIR / "poll"

sys.path.insert(0, str(POLL_DIR))


def _import_handler():
    module_name = "poll_handler_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, POLL_DIR / "handler.py")
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
    yield table


BUCKET_NAME = "cowork-polly-tts-740353583786-test"


@pytest.fixture
def pipeline_bucket(mocked_aws, monkeypatch):
    handler_module = _import_handler()
    monkeypatch.setattr(handler_module, "PIPELINE_BUCKET_NAME", BUCKET_NAME)
    s3_client = boto3.client("s3", region_name="us-east-1")
    s3_client.create_bucket(Bucket=BUCKET_NAME)
    yield s3_client


def _put_brief_artifacts(s3_client, date="2026-07-04", *, brief_md="# Brief\n\nSome content.", script="Listening script text.", candidates=None):
    s3_client.put_object(Bucket=BUCKET_NAME, Key=f"briefs/{date}/brief.md", Body=brief_md.encode("utf-8"))
    s3_client.put_object(Bucket=BUCKET_NAME, Key=f"briefs/{date}/listening-script.txt", Body=script.encode("utf-8"))
    if candidates is not None:
        s3_client.put_object(Bucket=BUCKET_NAME, Key=f"briefs/{date}/candidates.json", Body=json.dumps(candidates).encode("utf-8"))


class _FakeResponse:
    def __init__(self, json_body=None, status_code=200):
        self._json_body = json_body or {}
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_body


class _FakeDeploymentsClient:
    """Fake Sessions/Deployments API client -- records archive() calls so tests can
    assert FIX 6's "archive on every terminal path" behavior."""

    def __init__(self, session_status="complete"):
        self.session_status = session_status
        self.archived_deployment_ids: list[str] = []
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, path, **kwargs):
        self.gets.append(path)
        return _FakeResponse({"status": self.session_status})

    def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        if path.endswith("/archive"):
            deployment_id = path.split("/")[-2]
            self.archived_deployment_ids.append(deployment_id)
        return _FakeResponse({"ok": True})


def _judge_response(score=4):
    return json.dumps({"score": score, "rationale": "looks fine", "evidence": "evidence text", "insufficient_data": False})


class _FakeMessagesResource:
    def create(self, **kwargs):
        class _Block:
            type = "text"
            text = _judge_response()

        class _Msg:
            content = [_Block()]

        return _Msg()


class _FakeJudgeClient:
    def __init__(self):
        self.messages = _FakeMessagesResource()


@pytest.fixture(autouse=True)
def _fake_cost_miner(monkeypatch):
    """`_process_completed_run` calls `eval_core.cost_miner.fetch_session_cost`,
    which makes a real HTTP call against api.anthropic.com -- stub it out so tests
    never touch the network or need a real API key."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from eval_core import cost_miner

    def _fake_fetch_session_cost(anthropic_api_key, session_id, *, base_url=None):
        usage = cost_miner.TokenUsage(input_tokens=100, output_tokens=50, cache_creation_input_tokens=10, cache_read_input_tokens=1000)
        return cost_miner.SessionCostBreakdown(
            session_id=session_id,
            total_cost_usd=1.23,
            total_usage=usage,
            threads=(cost_miner.ThreadCost(thread_id="thread_1", usage=usage, cost_usd=1.23),),
            phase_totals=(
                cost_miner.PhaseCost(phase="research", usage=usage, cost_usd=0.5),
                cost_miner.PhaseCost(phase="writing", usage=usage, cost_usd=0.73),
            ),
        )

    monkeypatch.setattr(cost_miner, "fetch_session_cost", _fake_fetch_session_cost)
    yield


def _pending_row(run_id="run_1", session_id="sesn_1", deployment_id="depl_1"):
    return {
        "runId": run_id,
        "status": "pending",
        "candidateConfigId": "production",
        "sessionId": session_id,
        "deploymentId": deployment_id,
        "pollCount": 0,
    }


# --- FIX 1: completion write must actually succeed and persist `record` -------------


def test_completed_session_write_succeeds_and_persists_record(eval_table, pipeline_bucket):
    """Before the fix, `record` (a DynamoDB reserved keyword) as a bare
    UpdateExpression attribute name throws ValidationException on every real
    DynamoDB call -- moto enforces the same reserved-keyword validation, so this
    test fails before the `#r` alias fix and passes after."""
    handler_module = _import_handler()
    eval_table.put_item(Item=_pending_row())
    _put_brief_artifacts(pipeline_bucket)

    deployments_client = _FakeDeploymentsClient(session_status="complete")
    result = handler_module._handle(eval_table, pipeline_bucket, boto3.resource("dynamodb", region_name="us-east-1"), deployments_client, _FakeJudgeClient(), "fake-api-key")

    assert result["processed"] == 1
    assert result["failed"] == 0

    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    assert item["status"] == "complete"
    assert "record" in item
    stored_record = json.loads(item["record"])
    assert stored_record["run_id"] == "run_1"
    assert stored_record["candidate_config_id"] == "production"
    assert "criterion_scores" in stored_record
    assert stored_record["cost"]["total_cost_usd"] == 1.23


def test_completed_session_archives_its_temporary_deployment(eval_table, pipeline_bucket):
    handler_module = _import_handler()
    eval_table.put_item(Item=_pending_row(deployment_id="depl_to_archive"))
    _put_brief_artifacts(pipeline_bucket)

    deployments_client = _FakeDeploymentsClient(session_status="complete")
    handler_module._handle(eval_table, pipeline_bucket, boto3.resource("dynamodb", region_name="us-east-1"), deployments_client, _FakeJudgeClient(), "fake-api-key")

    assert "depl_to_archive" in deployments_client.archived_deployment_ids


# --- FIX 3: brief content + listening script must be persisted for the review UI ----


def test_completed_record_includes_brief_markdown_and_listening_script(eval_table, pipeline_bucket):
    handler_module = _import_handler()
    eval_table.put_item(Item=_pending_row())
    _put_brief_artifacts(pipeline_bucket, brief_md="# My Brief\n\nHeadline one.", script="Hello, this is your brief.")

    deployments_client = _FakeDeploymentsClient(session_status="complete")
    handler_module._handle(eval_table, pipeline_bucket, boto3.resource("dynamodb", region_name="us-east-1"), deployments_client, _FakeJudgeClient(), "fake-api-key")

    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    stored_record = json.loads(item["record"])
    assert "My Brief" in stored_record["brief_markdown"]
    assert "Hello, this is your brief." in stored_record["listening_script"]


# --- FIX 4: reader free-text feedback must be wired into the stored calibration -----


def test_completed_record_surfaces_free_text_feedback_without_identity(eval_table, pipeline_bucket, monkeypatch):
    handler_module = _import_handler()
    eval_table.put_item(Item=_pending_row())
    _put_brief_artifacts(pipeline_bucket, date="2026-07-04")

    monkeypatch.setattr(handler_module, "FEEDBACK_TABLE_NAME", "brief-feedback-test")
    dynamodb_resource = boto3.resource("dynamodb", region_name="us-east-1")
    feedback_table = dynamodb_resource.create_table(
        TableName="brief-feedback-test",
        KeySchema=[{"AttributeName": "feedbackId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "feedbackId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    feedback_table.wait_until_exists()
    feedback_table.put_item(
        Item={
            "feedbackId": "fb_1",
            "briefDate": "2026-07-04",
            "contentSelection": 4,
            "length": 3,
            "additionalSources": "Please cover The Information more.",
            "otherFeedback": "Loved today's edition.",
            "identity": "someone@example.com",
        }
    )

    deployments_client = _FakeDeploymentsClient(session_status="complete")
    handler_module._handle(eval_table, pipeline_bucket, dynamodb_resource, deployments_client, _FakeJudgeClient(), "fake-api-key")

    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    stored_record = json.loads(item["record"])
    assert "calibration" in stored_record
    free_text = stored_record["calibration"].get("free_text_feedback")
    assert free_text, "expected FR-15 free-text feedback to be surfaced into the stored record"
    assert any("The Information" in entry.get("additionalSources", "") for entry in free_text)
    assert any("Loved today's edition." in entry.get("otherFeedback", "") for entry in free_text)
    # Anonymity invariant: identity must never be read/surfaced anywhere in the record.
    assert "identity" not in json.dumps(stored_record)
    assert "someone@example.com" not in json.dumps(stored_record)


# --- FIX 6: archive the temporary deployment on every terminal (not just success) path


def test_session_failed_still_archives_temporary_deployment(eval_table, pipeline_bucket):
    handler_module = _import_handler()
    eval_table.put_item(Item=_pending_row(deployment_id="depl_on_failure"))

    deployments_client = _FakeDeploymentsClient(session_status="failed")
    result = handler_module._handle(eval_table, pipeline_bucket, boto3.resource("dynamodb", region_name="us-east-1"), deployments_client, _FakeJudgeClient(), "fake-api-key")

    assert result["failed"] == 1
    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    assert item["status"] == "failed"
    assert "depl_on_failure" in deployments_client.archived_deployment_ids


def test_poll_timeout_still_archives_temporary_deployment(eval_table, pipeline_bucket):
    handler_module = _import_handler()
    row = _pending_row(deployment_id="depl_on_timeout")
    row["pollCount"] = handler_module.MAX_POLL_ATTEMPTS - 1
    eval_table.put_item(Item=row)

    deployments_client = _FakeDeploymentsClient(session_status="running")
    result = handler_module._handle(eval_table, pipeline_bucket, boto3.resource("dynamodb", region_name="us-east-1"), deployments_client, _FakeJudgeClient(), "fake-api-key")

    assert result["failed"] == 1
    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    assert item["status"] == "failed"
    assert "depl_on_timeout" in deployments_client.archived_deployment_ids


def test_processing_exception_still_archives_temporary_deployment(eval_table, pipeline_bucket, monkeypatch):
    """Simulate a processing failure (e.g. a judge call raising) after the session
    completed -- the deployment must still be archived, not leaked."""
    handler_module = _import_handler()
    eval_table.put_item(Item=_pending_row(deployment_id="depl_on_processing_error"))
    _put_brief_artifacts(pipeline_bucket)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated judge/cost-miner failure")

    monkeypatch.setattr(handler_module, "_process_completed_run", _boom)

    deployments_client = _FakeDeploymentsClient(session_status="complete")
    result = handler_module._handle(eval_table, pipeline_bucket, boto3.resource("dynamodb", region_name="us-east-1"), deployments_client, _FakeJudgeClient(), "fake-api-key")

    assert result["failed"] == 1
    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    assert item["status"] == "failed"
    assert "depl_on_processing_error" in deployments_client.archived_deployment_ids


def test_archive_failure_never_uncompletes_a_finished_run(eval_table, pipeline_bucket):
    """Archival is best-effort: if the archive POST itself raises, the run must still
    be marked complete/failed as appropriate, never crash the poll cycle."""
    handler_module = _import_handler()
    eval_table.put_item(Item=_pending_row(deployment_id="depl_flaky"))
    _put_brief_artifacts(pipeline_bucket)

    class _FlakyArchiveClient(_FakeDeploymentsClient):
        def post(self, path, **kwargs):
            if path.endswith("/archive"):
                raise RuntimeError("simulated archive-endpoint outage")
            return super().post(path, **kwargs)

    deployments_client = _FlakyArchiveClient(session_status="complete")
    result = handler_module._handle(eval_table, pipeline_bucket, boto3.resource("dynamodb", region_name="us-east-1"), deployments_client, _FakeJudgeClient(), "fake-api-key")

    assert result["processed"] == 1
    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    assert item["status"] == "complete"
