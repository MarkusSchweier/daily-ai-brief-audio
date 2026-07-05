"""Unit tests for functions/submit-review/handler.py (PRD FR-19/AC-19), using moto.

FIX 2 regression coverage: a reviewer's override must land inside the item's
`record` JSON string's `human_overrides` dict -- the SAME place `poll/handler.py`'s
completion write populates and `eval_core/record.py`'s `EvalRecord`/
`aggregate_replicates()`/`effective_score()` already read from -- not a separate,
sibling `humanOverrides` top-level attribute nothing else ever reads.
"""

import importlib.util
import json
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


def _base_record(**overrides) -> dict:
    record = {
        "run_id": "run_1",
        "candidate_config_id": "production",
        "session_id": "sesn_1",
        "created_at": 100,
        "criterion_scores": {
            "content_selection": {"criterion": "content_selection", "score": 4, "rationale": "ok", "evidence": "ev", "insufficient_data": False},
            "length_format": {"criterion": "length_format", "score": 5, "rationale": "ok", "evidence": "ev", "insufficient_data": False},
            "dedup": {"criterion": "dedup", "score": 3, "rationale": "ok", "evidence": "ev", "insufficient_data": False},
        },
        "cost": {"total_cost_usd": 1.0, "phase_costs_usd": {}, "thread_costs_usd": {}},
        "human_overrides": {},
        "research_frozen_id": None,
        "schema_version": 1,
        "brief_markdown": "# Brief",
        "listening_script": "Script.",
    }
    record.update(overrides)
    return record


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
    table.put_item(Item={"runId": "run_1", "status": "complete", "candidateConfigId": "production", "record": json.dumps(_base_record())})
    table.put_item(Item={"runId": "run_pending", "status": "pending", "candidateConfigId": "production"})
    yield table


def _event(body, with_bearer="secret123"):
    headers = {"Authorization": f"Bearer {with_bearer}"} if with_bearer else {}
    return {"headers": headers, "body": json.dumps(body)}


def _stored_record(table, run_id="run_1") -> dict:
    item = table.get_item(Key={"runId": run_id})["Item"]
    return json.loads(item["record"])


def test_submit_review_agree_persists_override_inside_record(eval_table):
    handler_module = _import_handler()

    result = handler_module._handle(
        _event({"runId": "run_1", "criterion": "content_selection", "agreed": True, "comment": "looks right"}),
        eval_table,
    )

    assert result["statusCode"] == 200
    stored = _stored_record(eval_table)
    assert stored["human_overrides"]["content_selection"]["agreed"] is True
    assert stored["human_overrides"]["content_selection"]["comment"] == "looks right"
    # The rest of the record must be preserved untouched.
    assert stored["criterion_scores"]["content_selection"]["score"] == 4
    assert stored["brief_markdown"] == "# Brief"


def test_submit_review_override_persists_new_score_inside_record(eval_table):
    handler_module = _import_handler()

    result = handler_module._handle(
        _event({"runId": "run_1", "criterion": "length_format", "agreed": False, "overriddenScore": 2, "comment": "too long"}),
        eval_table,
    )

    assert result["statusCode"] == 200
    stored = _stored_record(eval_table)
    assert stored["human_overrides"]["length_format"]["overridden_score"] == 2


def test_no_sibling_human_overrides_attribute_is_written(eval_table):
    """Regression: a prior version wrote a separate, sibling `humanOverrides`
    (camelCase) top-level attribute that nothing else ever read. There must now be
    exactly one write path -- inside `record`."""
    handler_module = _import_handler()

    handler_module._handle(
        _event({"runId": "run_1", "criterion": "content_selection", "agreed": True}),
        eval_table,
    )

    item = eval_table.get_item(Key={"runId": "run_1"})["Item"]
    assert "humanOverrides" not in item


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


def test_submit_review_409_for_run_with_no_completed_record_yet(eval_table):
    handler_module = _import_handler()

    result = handler_module._handle(_event({"runId": "run_pending", "criterion": "dedup", "agreed": True}), eval_table)

    assert result["statusCode"] == 409


def test_second_criterion_override_does_not_clobber_the_first(eval_table):
    handler_module = _import_handler()

    handler_module._handle(_event({"runId": "run_1", "criterion": "content_selection", "agreed": True}), eval_table)
    handler_module._handle(_event({"runId": "run_1", "criterion": "dedup", "agreed": False, "overriddenScore": 3}), eval_table)

    stored = _stored_record(eval_table)
    assert "content_selection" in stored["human_overrides"]
    assert "dedup" in stored["human_overrides"]
