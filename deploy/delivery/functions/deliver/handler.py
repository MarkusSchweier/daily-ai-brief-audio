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
import re
import time
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
    archive_candidates_content,
    archive_source_usage_content,
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
#
# VERSION 2 (ADR-0015 D2): the four-artifact contract for full production decouple.
# In addition to v1's `brief_markdown` + `listening_script`, the body now carries the
# other two content artifacts -- `candidates` and `source_usage` (each the raw JSON
# string the skill produced) -- because in the decoupled model those files live in the
# MicroVM, not this Lambda's filesystem, so their content must travel in the body to be
# archived (v1 archived them by reading this Lambda's own WORKING_FOLDER, which is empty
# in the decoupled model). `brief_markdown` + `listening_script` stay REQUIRED (no brief
# can be sent without them); `candidates` + `source_usage` are archived best-effort and
# NEVER block the send (an additive artifact must never cost the subscriber the brief --
# see `_validate_request_body`). There is no live v1 caller (POST /deliver was never
# reached in production), so v1 is retired rather than dual-supported.
SUPPORTED_CONTRACT_VERSION = 2

# TTL (seconds from now) written on an idempotency-dedup row so the dedup table does not
# grow without bound (ADR-0015 D7). 48h comfortably covers a single run's retry window
# (the idempotency key is the run's brief_date); real delivery rows are NOT given a TTL
# and are retained as operational history (matching the table's RETAIN posture).
_IDEMPOTENCY_DEDUP_TTL_SECONDS = 48 * 60 * 60

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

    Validates the `contractVersion` field (FR-2) and the content fields (ADR-0015 D2,
    the four-artifact contract v2). Deliberately does NOT accept a `brief_html` field at
    all -- FR-2a: content generation never produces brief HTML; delivery derives it. A
    caller that still sends one is not rejected for it (a forward-compatible field the
    delivery side simply ignores would be a needless footgun to punish), but it is never
    read anywhere in this module.

    REQUIRED (a 400 here happens at trigger time, BEFORE any send -- so rejecting a
    malformed request loses no brief): `contractVersion`, `brief_markdown`,
    `listening_script`. The brief cannot be sent without these.

    BEST-EFFORT, NOT required (ADR-0015 D2's fail-safe): `candidates` and `source_usage`
    are additive archival artifacts. The real client always sends them, but the server
    must NOT 400 the whole delivery when one is absent -- that would let an additive
    artifact cost the subscriber the actual brief, violating "never lose the brief."
    They are therefore optional at validation; when present they must be strings (a
    wrong TYPE is a genuine caller bug worth a 400, since it is not "absent" -- it is
    malformed), and archival of them is handled best-effort in `_run_delivery`."""
    if payload.get("contractVersion") != SUPPORTED_CONTRACT_VERSION:
        return f"contractVersion must be {SUPPORTED_CONTRACT_VERSION}"
    if not payload.get("brief_markdown"):
        return "brief_markdown is required"
    if not payload.get("listening_script"):
        return "listening_script is required"
    for optional_str_field in ("candidates", "source_usage"):
        value = payload.get(optional_str_field)
        if value is not None and not isinstance(value, str):
            return f"{optional_str_field} must be a string (the raw JSON the skill produced)"
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return "metadata must be an object"
    if metadata is not None:
        idempotency_key = metadata.get("idempotency_key")
        if idempotency_key is not None and not _is_valid_idempotency_key(idempotency_key):
            return "metadata.idempotency_key must be a short string of [A-Za-z0-9._-]"
    return None


# An idempotency key is echoed into a DynamoDB partition-key value and a `/deliver/{id}`
# URL path, so it is constrained to a safe, bounded alphabet -- the production client
# supplies the run's `brief_date` (e.g. "2026-07-06"), which fits comfortably.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _is_valid_idempotency_key(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDEMPOTENCY_KEY_RE.match(value))


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

    metadata = payload.get("metadata") or {}
    idempotency_key = metadata.get("idempotency_key")

    delivery_id = uuid.uuid4().hex

    # Idempotency dedup (ADR-0015 D7): the async self-invoke worker leg is already
    # idempotent (it claims pending->in_progress and no-ops on a duplicate), but that
    # only defends against Lambda re-delivering the SAME deliveryId. It does NOT defend
    # against the CALLER (the production API client) POSTing /deliver twice -- e.g. a
    # client-side timeout then retry -- which would otherwise mint a SECOND deliveryId
    # and double-send. When the caller supplies an idempotency key (the run's
    # brief_date), a duplicate trigger returns the delivery the FIRST trigger already
    # started, and no second delivery/self-invoke happens. Claimed BEFORE the real
    # delivery row + self-invoke, and released (below) only if the self-invoke never
    # starts, so a genuine "nothing happened" failure can still be safely retried.
    if idempotency_key is not None:
        claimed_id = _claim_or_get_idempotent_delivery(table, idempotency_key, delivery_id)
        if claimed_id != delivery_id:
            logger.info(
                "DELIVERY_TRIGGER_IDEMPOTENT_REPLAY idempotency_key=%s delivery_id=%s",
                idempotency_key,
                claimed_id,
            )
            return {
                "statusCode": 202,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"deliveryId": claimed_id, "status": "pending", "idempotentReplay": True}),
            }

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
        # Nothing was sent (the worker never started), so release the idempotency claim
        # to let a subsequent retry start fresh rather than being deduped to this dead,
        # never-processed delivery (ADR-0015 D7/D8: a genuine "nothing happened" failure
        # must be re-drivable).
        if idempotency_key is not None:
            _release_idempotency_claim(table, idempotency_key)
        return {
            "statusCode": 502,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "trigger_failed"}),
        }

    logger.info("DELIVERY_TRIGGERED delivery_id=%s idempotency_key=%s", delivery_id, idempotency_key)
    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"deliveryId": delivery_id, "status": "pending"}),
    }


# ---------------------------------------------------------------------------
# Idempotency dedup (ADR-0015 D7) -- a front-guard "dedup row" in the SAME
# brief-deliveries table (partition key `deliveryId`), stored under a namespaced
# `idem#<key>` id so it coexists with real delivery rows (whose ids are random UUIDs
# that never collide with the `idem#` prefix). No GSI / no schema change needed.
# ---------------------------------------------------------------------------


def _idempotency_row_key(idempotency_key: str) -> str:
    return f"idem#{idempotency_key}"


def _claim_or_get_idempotent_delivery(table, idempotency_key: str, delivery_id: str) -> str:
    """Atomically claim `idempotency_key` for `delivery_id`. Returns `delivery_id` if
    THIS call won the claim (the first trigger for this key), else the delivery id a
    prior trigger already mapped the key to (a dedup replay -- the caller should poll
    THAT id, and no new delivery is started).

    A conditional PutItem (`attribute_not_exists(deliveryId)`) is the DynamoDB
    compare-and-swap idiom -- race-safe for two concurrent first-triggers: exactly one
    wins the put; the other's put fails the condition, reads the row, and returns the
    winner's mapped id. Mirrors the fail-closed, never-double-fire discipline of
    docs/adr/0010-restore-webhook-idempotency.md and the worker leg's own
    `_claim_delivery()` below."""
    row_key = _idempotency_row_key(idempotency_key)
    try:
        table.put_item(
            Item={
                "deliveryId": row_key,
                "mappedDeliveryId": delivery_id,
                "expiresAt": int(time.time()) + _IDEMPOTENCY_DEDUP_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(deliveryId)",
        )
        return delivery_id
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            existing = table.get_item(Key={"deliveryId": row_key}).get("Item") or {}
            # Defensive: if the row somehow lacks the mapped id, fall back to this
            # call's own id rather than returning None (never worse than "no dedup").
            return existing.get("mappedDeliveryId") or delivery_id
        raise


def _release_idempotency_claim(table, idempotency_key: str) -> None:
    """Delete the dedup row so a subsequent retry can re-claim the key. Called ONLY
    when the worker self-invoke never started (nothing was sent), so re-triggering is
    safe. Best-effort: a delete failure is logged, never raised (the caller is already
    returning a 502; a stale dedup row would at worst dedupe a retry to a
    clearly-failed delivery, which the client surfaces loudly per D8)."""
    try:
        table.delete_item(Key={"deliveryId": _idempotency_row_key(idempotency_key)})
    except Exception as e:  # noqa: BLE001 - best-effort cleanup
        logger.warning("IDEMPOTENCY_RELEASE_FAILED idempotency_key=%s error=%r", idempotency_key, e)


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
    # The other two content artifacts (ADR-0015 D2, contract v2) -- raw JSON strings the
    # skill produced, handed straight through and archived best-effort below. Optional at
    # this layer: their absence never blocks the send (they are additive archival
    # artifacts), matching `_validate_request_body`'s fail-safe stance.
    candidates_content = body.get("candidates")
    source_usage_content = body.get("source_usage")
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

    # Archive the other two artifacts from the REQUEST BODY (ADR-0015 D2) -- in the
    # decoupled model they are produced in the MicroVM, not on this Lambda's filesystem,
    # so their content travels in the body (v1 read them from this Lambda's own empty
    # WORKING_FOLDER). Best-effort: a missing/failed additive artifact never affects the
    # send, which already completed above.
    candidates_archived = archive_candidates_content(s3_client, brief_date, content=candidates_content)
    source_usage_archived = archive_source_usage_content(s3_client, brief_date, content=source_usage_content)

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
