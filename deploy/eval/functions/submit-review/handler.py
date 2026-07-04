"""POST /reviews — persist a reviewer's per-criterion agree/override/comment (PRD
docs/prd/eval-harness.md FR-19, AC-19).

Runs with the least-privilege role in `brief_eval/stack.py`'s
`SubmitReviewFunctionRole` (GetItem/UpdateItem on the eval-records table only). Gated
by the shared reviewer bearer secret (ADR-0013 §E) -- this is the review-write path
FR-20 requires NOT be an open, unauthenticated public write surface.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import boto3

import review_auth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import os

EVAL_TABLE_NAME = os.environ.get("EVAL_TABLE_NAME", "brief-eval-records")


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if not review_auth.is_authorized(event):
        return review_auth.unauthorized_response()

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(EVAL_TABLE_NAME)
    return _handle(event, table)


def _handle(event: dict[str, Any], table) -> dict[str, Any]:
    payload = _parse_body(event)

    run_id = payload.get("runId")
    criterion = payload.get("criterion")
    agreed = bool(payload.get("agreed"))
    overridden_score = payload.get("overriddenScore")
    comment = str(payload.get("comment") or "")[:2000]
    reviewer = str(payload.get("reviewer") or "")[:200]

    if not run_id or not criterion:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": "runId and criterion are required"}),
        }

    if overridden_score is not None:
        if not isinstance(overridden_score, int) or isinstance(overridden_score, bool) or not (1 <= overridden_score <= 5):
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": "overriddenScore must be an integer 1-5"}),
            }

    existing = table.get_item(Key={"runId": run_id}).get("Item")
    if existing is None:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": "no such run"}),
        }

    override = {
        "criterion": criterion,
        "agreed": agreed,
        "overridden_score": overridden_score,
        "comment": comment,
        "reviewer": reviewer,
        "reviewed_at": int(time.time()),
    }

    try:
        table.update_item(
            Key={"runId": run_id},
            UpdateExpression="SET humanOverrides.#c = :override",
            ExpressionAttributeNames={"#c": criterion},
            ExpressionAttributeValues={":override": override},
        )
    except Exception:
        # humanOverrides map may not exist yet on an older row -- initialize it, then retry.
        table.update_item(
            Key={"runId": run_id},
            UpdateExpression="SET humanOverrides = :m",
            ExpressionAttributeValues={":m": {criterion: override}},
        )

    logger.info("REVIEW_SUBMITTED run_id=%s criterion=%s agreed=%s", run_id, criterion, agreed)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True}),
    }
