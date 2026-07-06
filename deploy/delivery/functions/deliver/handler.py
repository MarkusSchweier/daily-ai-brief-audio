"""The decoupled delivery boundary's single Lambda (PRD
docs/prd/agent-system-redesign.md FR-1/FR-2/FR-2a/FR-3, ADR-0014 Decision 2a, as
amended 2026-07-06 for an ASYNC trigger-and-poll transport).

ONE function handles THREE distinct invocation shapes, branched on `event`'s shape
in `handler()` below:

  1. **`POST /deliver`** (API Gateway HTTP API request) -- the SYNCHRONOUS trigger
     leg. Validates the bearer auth + request body, writes a `pending` row to the
     `brief-deliveries` DynamoDB table, asynchronously self-invokes THIS SAME
     function (`InvocationType="Event"`) with the worker-leg payload, and returns
     `202 {"deliveryId": ..., "status": "pending"}` IMMEDIATELY -- it does NOT wait
     for Polly/SES/archival, which take several minutes (an API Gateway HTTP API's
     integration timeout is a hard, non-raisable 30 seconds -- see ADR-0014
     Decision 2a's "Why async" note).
  2. **`GET /deliver/{deliveryId}`** (API Gateway HTTP API request) -- the poll
     route, same bearer-auth gate. Reads the tracking row and reports its current
     `status` (`pending` / `succeeded` / `failed`) plus, once terminal, the summary
     of what was derived/synthesized/sent/archived.
  3. **The async self-invoke worker leg** (NOT an API Gateway event -- a plain
     `{"_delivery_worker": True, "deliveryId": ..., "body": {...}}` payload this
     same function sent itself via `lambda.invoke(InvocationType="Event", ...)`) --
     does the actual multi-minute work: derive HTML (FR-2a), synthesize audio,
     `send_all()`, archive, and writes the outcome back to the tracking row.
  4. **`GET /recent-briefs`** (API Gateway HTTP API request, ADR-0014 Decision 2d) --
     a SYNCHRONOUS read route (no async trigger/poll -- reading a few small S3
     objects is comfortably within API Gateway's 30s integration ceiling), gated by
     its OWN SEPARATE read-only bearer secret (`recent_briefs_auth.py`,
     `RECENT_BRIEFS_READ_BEARER_SECRET_ARN` -- deliberately NOT the same secret
     `delivery_auth.py` checks). This is what lets a `cloud` candidate read the same
     recent priors production reads from S3, reaching eval-vs-production parity,
     WITHOUT ever holding an AWS credential or the delivery/send bearer token --
     see `recent_briefs_auth.py`'s module docstring for the auth-separation
     rationale (a candidate holding only the read token must be structurally
     unable to reach `POST /deliver`).

A single function (not a separate trigger Lambda + worker Lambda) is a deliberate
choice (ADR-0014 Decision 2a): it keeps ALL delivery logic -- and the ONE IAM role
that holds SES-to-subscriber rights post-redesign -- in one reviewable place. Unlike
`deploy/eval/`'s trigger/poll split (which serves an EventBridge-scheduled sweep
over ALL pending eval rows, a genuinely different mechanism), delivery has exactly
one discrete unit of work per call and one caller polling for THAT call's result,
so a per-call self-invoke is the leaner fit -- no EventBridge rule needed.

**Idempotency (real correctness requirement, NOT optional).** Async self-invoke has
at-least-once/retry semantics -- Lambda's own docs state an asynchronously invoked
function may be invoked more than once for the same event. The worker leg
(`_handle_worker_invocation()`) MUST NOT double-send if invoked twice for the same
`deliveryId`. Before doing ANY Polly/SES work, it atomically transitions the
tracking row from `pending` to `in_progress` via a conditional `UpdateItem` (a
condition expression that only succeeds if the row's CURRENT status is still
`pending`) and no-ops (logs and returns) if that transition fails because the row
is already past `pending` -- mirroring the fail-closed, never-double-fire discipline
`docs/adr/0010-restore-webhook-idempotency.md` already established for the
launcher's webhook guard. See `test_idempotency.py` for a real unit test proving a
duplicate worker-leg invocation for the same `deliveryId` does not send twice.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

import delivery_auth
import delivery_core
import recent_briefs_auth
from brief_history import (
    DEFAULT_RECENT_BRIEFS_COUNT,
    archive_candidates_file,
    archive_source_usage_file,
    archive_todays_brief,
    read_recent_prior_briefs,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DELIVERIES_TABLE_NAME = os.environ.get("DELIVERIES_TABLE_NAME", "brief-deliveries")
DELIVERY_FUNCTION_NAME = os.environ.get("DELIVERY_FUNCTION_NAME", "")
SUBSCRIBERS_TABLE_NAME = os.environ.get("SUBSCRIBERS_TABLE_NAME", "brief-subscribers")
SUBSCRIBERS_API_BASE_URL = os.environ.get("SUBSCRIBERS_API_BASE_URL", "")
FEEDBACK_TOKEN_SECRET_ARN = os.environ.get("FEEDBACK_TOKEN_SECRET_ARN", "")
FEEDBACK_BASE_URL = os.environ.get("FEEDBACK_BASE_URL", "")
WORKING_FOLDER = os.environ.get("WORKING_FOLDER", "/tmp")
# Date basis for the briefs/ archive when the caller doesn't supply its own
# metadata.brief_date -- the run's local calendar date in the pipeline's timezone
# (docs/adr/0005 / docs/adr/0006), matching audio_email.py's own
# `_today_local_date()` / `PIPELINE_TIMEZONE` convention exactly.
PIPELINE_TIMEZONE = os.environ.get("PIPELINE_TIMEZONE", "Europe/Berlin")


def _today_local_date() -> str:
    return datetime.now(ZoneInfo(PIPELINE_TIMEZONE)).strftime("%Y-%m-%d")

# The ONE supported request-body contract version (PRD FR-2: "the contract's
# version shall be recorded so a change to it is explicit and reviewable"). A
# caller supplying anything else is rejected with 400 rather than silently
# accepted -- a version bump here is a deliberate, reviewed code change, mirroring
# feedback_token.py's own `_SCHEME_VERSION` discipline.
SUPPORTED_CONTRACT_VERSION = 1

# GET /recent-briefs's own response-contract version (ADR-0014 Decision 2d's
# "contractVersion discipline, consistent with Decision 2a" -- a future change to
# THIS read contract is a reviewable code change, not invisible drift). Independent
# of SUPPORTED_CONTRACT_VERSION above, which versions the DIFFERENT POST /deliver
# request contract.
RECENT_BRIEFS_CONTRACT_VERSION = 1
# A caller-supplied `count` is clamped to this ceiling (ADR-0014 Decision 2d: "so a
# caller cannot request an unbounded listing") -- never a hard 400, since an
# over-large or malformed count is a caller mistake this endpoint should degrade
# gracefully from, not crash on (CLAUDE.md: never lose the brief -- or, here, the
# read -- over a glitch).
MAX_RECENT_BRIEFS_COUNT = 7

# The private marker distinguishing a self-invoke worker-leg payload from an API
# Gateway request event -- API Gateway HTTP API events never carry this key.
_WORKER_INVOCATION_MARKER = "_delivery_worker"


def _is_api_gateway_event(event: dict[str, Any]) -> bool:
    """API Gateway HTTP API (payload format 2.0) events always carry a
    `requestContext.http.method` -- used to distinguish a real HTTP request from
    the worker-leg self-invoke payload, which has neither `requestContext` nor
    `_delivery_worker` confused with each other."""
    return "requestContext" in event


def _is_worker_invocation(event: dict[str, Any]) -> bool:
    """True only for a genuine self-invoke worker-leg payload -- REQUIRES both
    the private `_delivery_worker` marker AND the absence of `requestContext`
    (reviewer-found gap, fixed: `_is_api_gateway_event()` was defined but never
    actually called anywhere, leaving `handler()`'s dispatch relying IMPLICITLY
    on "a real API Gateway payload-format-2.0 event can never carry a top-level
    `_delivery_worker` key" -- true and safe today, but not defended against a
    future refactor or API Gateway integration change silently breaking that
    invariant). This is defense-in-depth, not a behavior change for any request
    this Lambda can receive today: a genuine self-invoke payload never has
    `requestContext`, and a genuine API Gateway event never has
    `_delivery_worker`, so requiring BOTH conditions here changes nothing for
    real traffic, but closes the gap if that ever stops being true."""
    return event.get(_WORKER_INVOCATION_MARKER) is True and not _is_api_gateway_event(event)


def _is_recent_briefs_request(event: dict[str, Any]) -> bool:
    """True only for a genuine `GET /recent-briefs` API Gateway request --
    distinguished from `GET /deliver/{deliveryId}` (the OTHER route sharing the GET
    method) by path, since method alone can no longer disambiguate the two GET
    routes this Lambda now serves (ADR-0014 Decision 2d). Checked ONLY on a real
    API Gateway event -- the worker-invocation dispatch in `handler()` already runs
    first, so this never sees a self-invoke payload."""
    raw_path = event.get("rawPath", "")
    route_key = (event.get("requestContext", {}) or {}).get("routeKey", "")
    return raw_path == "/recent-briefs" or route_key.endswith(" /recent-briefs")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if _is_worker_invocation(event):
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(DELIVERIES_TABLE_NAME)
        polly_client = boto3.client("polly")
        s3_client = boto3.client("s3")
        ses_client = boto3.client("ses")
        dynamodb_client = boto3.client("dynamodb")
        secretsmanager_client = boto3.client("secretsmanager")
        return _handle_worker_invocation(
            event,
            table=table,
            polly_client=polly_client,
            s3_client=s3_client,
            ses_client=ses_client,
            dynamodb_client=dynamodb_client,
            secretsmanager_client=secretsmanager_client,
        )

    # GET /recent-briefs is checked BEFORE the delivery bearer-auth gate below --
    # it is authenticated by its OWN, SEPARATE read-only secret
    # (`recent_briefs_auth.py`), never by `delivery_auth`'s delivery/send secret.
    # This is the auth-separation property ADR-0014 Decision 2d requires: a
    # candidate holding only the read token must be structurally unable to reach
    # `POST /deliver` / `GET /deliver/{deliveryId}` -- and, symmetrically, the
    # delivery/send token must not authenticate here either. See
    # `test_recent_briefs_auth_separation.py` for the non-interchangeability proof.
    if _is_recent_briefs_request(event):
        if not recent_briefs_auth.is_authorized(event):
            return recent_briefs_auth.unauthorized_response()
        # /recent-briefs is GET-only. Detection above is path-based (so ANY method
        # to this path is captured by the read branch and stays under the read
        # secret -- it can never fall through to the delivery-auth/trigger path
        # below), and a non-GET is then explicitly rejected here rather than served
        # as a read. In practice the API Gateway only registers `GET /recent-briefs`
        # (a non-GET 404s at the gateway before reaching this Lambda), so this 405 is
        # defense-in-depth for any future/alternate fronting -- checked AFTER auth so
        # an unauthenticated caller learns nothing about the route.
        method = (event.get("requestContext", {}).get("http", {}) or {}).get("method", "")
        if method != "GET":
            return {
                "statusCode": 405,
                "headers": {"Content-Type": "application/json", "Allow": "GET"},
                "body": json.dumps({"error": "method_not_allowed"}),
            }
        s3_client = boto3.client("s3")
        return _handle_recent_briefs(event, s3_client)

    if not delivery_auth.is_authorized(event):
        return delivery_auth.unauthorized_response()

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(DELIVERIES_TABLE_NAME)
    lambda_client = boto3.client("lambda")

    method = (event.get("requestContext", {}).get("http", {}) or {}).get("method", "")
    if method == "GET":
        return _handle_poll(event, table)
    return _handle_trigger(event, table, lambda_client)


# ---------------------------------------------------------------------------
# Leg 1: POST /deliver -- the synchronous trigger.
# ---------------------------------------------------------------------------


def _validate_request_body(payload: dict[str, Any]) -> str | None:
    """Returns an error message string if `payload` is invalid, else None.

    Validates the `contractVersion` field (FR-2) and the two required content
    fields. Deliberately does NOT accept a `brief_html` field at all -- FR-2a:
    content generation never produces brief HTML; delivery derives it. A caller
    that still sends one is not rejected for it (a forward-compatible field the
    delivery side simply ignores would be a needless footgun to punish), but it is
    never read anywhere in this module."""
    if payload.get("contractVersion") != SUPPORTED_CONTRACT_VERSION:
        return f"contractVersion must be {SUPPORTED_CONTRACT_VERSION}"
    if not payload.get("brief_markdown"):
        return "brief_markdown is required"
    if not payload.get("listening_script"):
        return "listening_script is required"
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return "metadata must be an object"
    return None


def _handle_trigger(event: dict[str, Any], table, lambda_client) -> dict[str, Any]:
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _bad_request("body must be valid JSON")

    if not isinstance(payload, dict):
        return _bad_request("body must be a JSON object")

    validation_error = _validate_request_body(payload)
    if validation_error:
        return _bad_request(validation_error)

    delivery_id = uuid.uuid4().hex

    table.put_item(
        Item={
            "deliveryId": delivery_id,
            "status": "pending",
        }
    )

    worker_payload = {
        _WORKER_INVOCATION_MARKER: True,
        "deliveryId": delivery_id,
        "body": payload,
    }

    try:
        lambda_client.invoke(
            FunctionName=DELIVERY_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(worker_payload).encode("utf-8"),
        )
    except Exception as e:  # noqa: BLE001 - surface a clean 502, never leak internals
        logger.error("DELIVERY_SELF_INVOKE_FAILED delivery_id=%s error=%r", delivery_id, e)
        # The pending row is now orphaned (nothing will ever process it) -- mark it
        # failed immediately rather than leaving a caller polling forever.
        table.update_item(
            Key={"deliveryId": delivery_id},
            UpdateExpression="SET #s = :failed, #e = :error",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={":failed": "failed", ":error": "failed to start delivery worker"},
        )
        return {
            "statusCode": 502,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "trigger_failed"}),
        }

    logger.info("DELIVERY_TRIGGERED delivery_id=%s", delivery_id)
    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"deliveryId": delivery_id, "status": "pending"}),
    }


def _bad_request(message: str) -> dict[str, Any]:
    return {
        "statusCode": 400,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


# ---------------------------------------------------------------------------
# Leg 2: GET /deliver/{deliveryId} -- the poll route.
# ---------------------------------------------------------------------------


def _handle_poll(event: dict[str, Any], table) -> dict[str, Any]:
    delivery_id = (event.get("pathParameters") or {}).get("deliveryId")
    if not delivery_id:
        return _bad_request("deliveryId path parameter is required")

    response = table.get_item(Key={"deliveryId": delivery_id})
    item = response.get("Item")
    if item is None:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "not_found"}),
        }

    status = item.get("status", "pending")
    body: dict[str, Any] = {"status": status}
    if status == "succeeded" and "summary" in item:
        body["summary"] = json.loads(item["summary"]) if isinstance(item["summary"], str) else item["summary"]
    elif status == "failed" and "error" in item:
        body["error"] = item["error"]

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Leg 4: GET /recent-briefs -- the SYNCHRONOUS recent-priors read route
# (ADR-0014 Decision 2d). Gated (in handler()) by recent_briefs_auth's OWN,
# SEPARATE secret -- never delivery_auth's. No async trigger/poll: reading a
# handful of small brief.md objects from S3 is cheap and comfortably within API
# Gateway's 30s integration ceiling, unlike POST /deliver's minutes-long Polly/SES
# work.
# ---------------------------------------------------------------------------


def _parse_recent_briefs_count(raw_count: str | None) -> int:
    """Resolve the `count` query parameter to a safe, in-range int: missing or
    non-integer -> the default (`DEFAULT_RECENT_BRIEFS_COUNT`, matching
    production's own default); anything <= 0 -> the default; anything above
    `MAX_RECENT_BRIEFS_COUNT` -> clamped to the max. Never raises and never
    causes a 500 -- a malformed/oversized count is a caller mistake this
    endpoint degrades gracefully from (ADR-0014 Decision 2d)."""
    if raw_count is None:
        return DEFAULT_RECENT_BRIEFS_COUNT
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        return DEFAULT_RECENT_BRIEFS_COUNT
    if count <= 0:
        return DEFAULT_RECENT_BRIEFS_COUNT
    return min(count, MAX_RECENT_BRIEFS_COUNT)


def _handle_recent_briefs(event: dict[str, Any], s3_client) -> dict[str, Any]:
    """`GET /recent-briefs?count=<n>` -- reads the last `count` prior briefs via
    the ALREADY-PRESENT `brief_history.read_recent_prior_briefs()` (this app's own
    hand-duplicated copy, same one `POST /deliver`'s archival leg already uses) and
    returns them most-recent-first.

    `today` is computed the same way the rest of this pipeline derives its local
    calendar date -- `_today_local_date()` (PIPELINE_TIMEZONE, default
    Europe/Berlin) -- so a candidate reading recent priors gets exactly the same
    "strictly before today" window production's own read-recent-briefs step would
    on the same calendar day.

    An empty result (no priors exist yet -- a cold-start store, or the very first
    run) is still a `200` with `"briefs": []`, never a 404 -- mirroring
    `read_recent_prior_briefs()`'s own graceful-degradation contract exactly (a
    missing/young store is the normal case, not an error; ADR-0014 Decision 2d /
    ADR-0005's "the read must tolerate an empty listing"). A transient S3
    listing/read failure degrades the same way, since
    `read_recent_prior_briefs()` itself already logs-and-skips rather than
    raising -- this route can never 500 because of a prior-briefs read glitch."""
    query_params = event.get("queryStringParameters") or {}
    count = _parse_recent_briefs_count(query_params.get("count"))

    prior_briefs = read_recent_prior_briefs(s3_client, _today_local_date(), count=count)

    body = {
        "contractVersion": RECENT_BRIEFS_CONTRACT_VERSION,
        "briefs": [{"date": prior.date, "markdown": prior.markdown} for prior in prior_briefs],
    }
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Leg 3: the async self-invoke worker -- does the actual derive/synthesize/send/
# archive work. IDEMPOTENT: a duplicate invocation for an already-claimed
# deliveryId no-ops (see module docstring).
# ---------------------------------------------------------------------------


def _claim_delivery(table, delivery_id: str) -> bool:
    """Atomically transition `delivery_id`'s row from `pending` to `in_progress`.
    Returns True if THIS invocation won the claim, False if the row was already
    past `pending` (a duplicate/retried self-invoke) -- in which case the caller
    must no-op, never re-send.

    A conditional UpdateItem (ConditionExpression gated on the row's CURRENT
    status still being `pending`) is the DynamoDB idiom for exactly this
    compare-and-swap -- mirrors the fail-closed, never-double-fire discipline
    docs/adr/0010-restore-webhook-idempotency.md already established for the
    launcher's webhook guard, applied here via a conditional write instead of
    Powertools Idempotency (this table has a different, simpler access pattern:
    one row per delivery, written once at trigger time, updated at most twice --
    claim, then terminal status -- so a bespoke conditional UpdateItem is
    proportionate; Powertools Idempotency is built for de-duplicating a much wider
    variety of payload shapes than this single, already-uniquely-keyed table
    needs)."""
    try:
        table.update_item(
            Key={"deliveryId": delivery_id},
            UpdateExpression="SET #s = :in_progress",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":in_progress": "in_progress", ":pending": "pending"},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _mark_succeeded(table, delivery_id: str, summary: dict[str, Any]) -> None:
    table.update_item(
        Key={"deliveryId": delivery_id},
        UpdateExpression="SET #s = :succeeded, #summary = :summary",
        ExpressionAttributeNames={"#s": "status", "#summary": "summary"},
        ExpressionAttributeValues={":succeeded": "succeeded", ":summary": json.dumps(summary)},
    )


def _mark_failed(table, delivery_id: str, error_message: str) -> None:
    table.update_item(
        Key={"deliveryId": delivery_id},
        UpdateExpression="SET #s = :failed, #e = :error",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={":failed": "failed", ":error": error_message},
    )


def _handle_worker_invocation(
    event: dict[str, Any],
    *,
    table,
    polly_client,
    s3_client,
    ses_client,
    dynamodb_client,
    secretsmanager_client,
) -> dict[str, Any]:
    delivery_id = event["deliveryId"]
    body = event["body"]

    if not _claim_delivery(table, delivery_id):
        # Duplicate/retried self-invoke for an already-claimed delivery -- log and
        # no-op. This is the exact scenario the idempotency requirement exists for:
        # Lambda's async invocation is at-least-once, so this branch WILL be hit in
        # production occasionally, and it must never re-run send_all().
        logger.info("DELIVERY_WORKER_DUPLICATE_INVOCATION_SKIPPED delivery_id=%s", delivery_id)
        return {"deliveryId": delivery_id, "status": "duplicate_skipped"}

    try:
        summary = _run_delivery(
            body,
            polly_client=polly_client,
            s3_client=s3_client,
            ses_client=ses_client,
            dynamodb_client=dynamodb_client,
            secretsmanager_client=secretsmanager_client,
        )
        _mark_succeeded(table, delivery_id, summary)
        logger.info("DELIVERY_SUCCEEDED delivery_id=%s", delivery_id)
        return {"deliveryId": delivery_id, "status": "succeeded"}
    except Exception as e:  # noqa: BLE001 - a delivery failure must be recorded, never crash silently
        logger.error("DELIVERY_FAILED delivery_id=%s error=%r", delivery_id, e)
        _mark_failed(table, delivery_id, "delivery_failed")
        return {"deliveryId": delivery_id, "status": "failed"}


def _run_delivery(
    body: dict[str, Any],
    *,
    polly_client,
    s3_client,
    ses_client,
    dynamodb_client,
    secretsmanager_client,
) -> dict[str, Any]:
    """Do the actual derive -> synthesize -> send -> archive work, returning the
    response summary dict (stored on the tracking row, returned by the poll route
    on success). Covers, at minimum: whether HTML was derived, whether audio
    synthesis succeeded (`audio_ok`, matching today's fail-safe -- a Polly failure
    must NOT fail the whole delivery), send counts, and whether archival
    succeeded."""
    brief_markdown = body["brief_markdown"]
    listening_script = body["listening_script"]
    metadata = body.get("metadata") or {}
    email_subject = metadata.get("email_subject") or "Daily AI Brief"
    brief_date = metadata.get("brief_date") or _today_local_date()
    enable_subscriber_fanout = bool(metadata.get("enable_subscriber_fanout", False))

    # 1) Derive HTML deterministically, no LLM (FR-2a) -- always attempted; if this
    # raises, the whole delivery is correctly marked failed (there is no brief to
    # send without it).
    brief_html = delivery_core.derive_html(brief_markdown)

    # 2) Synthesize audio -- fail-safe: never blocks the send (CLAUDE.md).
    mp3_out_path = os.path.join(WORKING_FOLDER, "brief.mp3")
    audio_ok, _audio_s3_key, mp3_bytes = delivery_core.synthesize_audio(
        polly_client, s3_client, listening_script, mp3_out_path
    )

    # 3) Send -- owner copy + subscriber fan-out (FR-1: this is the ONLY place that
    # can email a real subscriber post-redesign).
    (
        sent_count,
        failed_count,
        subscriber_sent_count,
        subscriber_failed_count,
        subscriber_query_failed,
    ) = delivery_core.send_all(
        ses_client,
        dynamodb_client,
        secretsmanager_client,
        email_subject,
        brief_html,
        mp3_bytes,
        "brief.mp3",
        SUBSCRIBERS_TABLE_NAME,
        brief_date,
        subscribers_api_base_url=SUBSCRIBERS_API_BASE_URL,
        feedback_base_url=FEEDBACK_BASE_URL,
        feedback_token_secret_arn=FEEDBACK_TOKEN_SECRET_ARN,
        skip_subscriber_fanout=not enable_subscriber_fanout,
    )

    delivery_core.send_confirmation_email(
        ses_client,
        brief_date,
        subscriber_sent_count,
        subscriber_failed_count,
        skipped=not enable_subscriber_fanout,
        subscriber_query_failed=subscriber_query_failed,
    )

    # 4) Archive -- best-effort, never gates the send (already completed above).
    archive_ok = True
    try:
        archive_todays_brief(
            s3_client,
            brief_date,
            markdown=brief_markdown,
            html=brief_html,
            listening_script=listening_script,
            audio_key=_audio_s3_key if audio_ok else None,
        )
    except Exception as e:  # noqa: BLE001 - archival must never fail the delivery outcome
        logger.warning("DELIVERY_ARCHIVE_FAILED brief_date=%s error=%r", brief_date, e)
        archive_ok = False

    candidates_archived = archive_candidates_file(s3_client, brief_date, working_folder=WORKING_FOLDER)
    source_usage_archived = archive_source_usage_file(s3_client, brief_date, working_folder=WORKING_FOLDER)

    return {
        "html_derived": True,
        "audio_ok": audio_ok,
        "sent_count": sent_count,
        "failed_count": failed_count,
        "subscriber_sent_count": subscriber_sent_count,
        "subscriber_failed_count": subscriber_failed_count,
        "subscriber_query_failed": subscriber_query_failed,
        "archive_ok": archive_ok,
        "candidates_archived": candidates_archived,
        "source_usage_archived": source_usage_archived,
    }
