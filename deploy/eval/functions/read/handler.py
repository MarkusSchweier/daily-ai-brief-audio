"""GET /runs, GET /runs/{runId}, GET /candidates — read-only endpoints the review site
needs (PRD docs/prd/eval-harness.md FR-18 list+detail, FR-24 comparison/leaderboard).

Runs with the least-privilege role in `brief_eval/stack.py`'s `ReadFunctionRole`
(GetItem/Scan/Query on the eval-records table only -- no write). Gated by the shared
reviewer bearer secret (ADR-0013 §E) since this serves internal eval data (FR-20: not
exposed to the anonymous public).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import boto3

import review_auth

EVAL_TABLE_NAME = os.environ.get("EVAL_TABLE_NAME", "brief-eval-records")


class _DecimalEncoder(json.JSONEncoder):
    """DynamoDB's resource-level Table interface returns numeric attributes as
    `decimal.Decimal`, which the stdlib json module cannot serialize by default --
    render whole numbers as int and fractional ones as float, matching how the value
    was almost certainly written (createdAt epoch seconds, scores, etc.)."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


def _response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _route(event: dict[str, Any]) -> str:
    """API Gateway v2 HTTP API proxy integration carries the matched route under
    `requestContext.routeKey` (e.g. "GET /runs/{runId}")."""
    return (event.get("requestContext", {}) or {}).get("routeKey", "")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if not review_auth.is_authorized(event):
        return review_auth.unauthorized_response()

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(EVAL_TABLE_NAME)
    return _handle(event, table)


def _handle(event: dict[str, Any], table) -> dict[str, Any]:
    route = _route(event)
    path_params = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    if route == "GET /runs/{runId}" or ("runId" in path_params and route == ""):
        run_id = path_params.get("runId")
        item = table.get_item(Key={"runId": run_id}).get("Item")
        if item is None:
            return _response(404, {"ok": False, "error": "no such run"})
        return _response(200, {"ok": True, "run": item})

    if route == "GET /candidates":
        items = table.scan().get("Items", [])
        completed = [i for i in items if i.get("status") == "complete"]
        by_candidate: dict[str, list[dict]] = {}
        for item in completed:
            by_candidate.setdefault(item.get("candidateConfigId", "unknown"), []).append(item)
        return _response(200, {"ok": True, "candidates": by_candidate})

    # Default: GET /runs -- list all rows, optionally filtered by ?status=pending.
    items = table.scan().get("Items", [])
    status_filter = query_params.get("status")
    if status_filter:
        items = [i for i in items if i.get("status") == status_filter]
    return _response(200, {"ok": True, "runs": items})
