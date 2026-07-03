"""GET /confirm?email=&token= — activate a pending subscription.

See docs/prd/public-subscriptions.md FR-9..FR-11, AC-2, AC-11 and docs/adr/0003. Runs with
the least-privilege role in docs/adr/0002 A (GetItem/UpdateItem on the table only, no SES).

On the actual `pending` -> `confirmed` transition ONLY (never the idempotent
already-confirmed re-click branch), this handler also asynchronously invokes the
welcome-send Lambda (docs/adr/0009, docs/prd/instant-welcome-brief.md FR-3/FR-7/FR-9) so
the new subscriber immediately receives the latest brief. This Lambda gains ONLY
`lambda:InvokeFunction` on that one target -- it never holds SES or S3 rights (those
grants live on the welcome-send Lambda's own role per ADR-0009's documented deviation
from the PRD's literal FR-13/FR-14 wording). The invoke is wrapped so a failure there is
logged but can never fail (or even delay) this handler's own response.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from subscriber_common import (
    STATUS_CONFIRMED,
    STATUS_PENDING,
    SUBSCRIBE_SITE_URL,
    build_response,
    constant_time_equals,
    generate_token,
    get_subscriber,
    get_table,
    normalize_email,
    now_epoch,
    render_page,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Set via CDK environment to the welcome-send Lambda's ARN once it exists (stack.py
# wires this after both functions are constructed). Left unset in any context that
# doesn't need the welcome send (e.g. a bare unit test) -- see _invoke_welcome_send.
WELCOME_FUNCTION_NAME = os.environ.get("WELCOME_FUNCTION_NAME", "")

# Single neutral page for every failure mode (no such row, wrong token, expired, wrong
# state) so responses never differentiate why confirmation failed (AC-11, ADR-0003 §Tokens).
_INVALID_OR_EXPIRED_BODY = render_page(
    "Link invalid or expired",
    "This confirmation link is invalid or has expired",
    f'<p>Please <a href="{SUBSCRIBE_SITE_URL}">sign up again</a> to receive a fresh confirmation email.</p>',
)
_CONFIRMED_BODY = render_page(
    "Subscribed",
    "You're subscribed",
    "<p>You'll now receive the daily AI brief (written text plus narrated audio) by email. "
    "You can unsubscribe at any time using the link in any brief email.</p>",
)


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    return event.get("queryStringParameters") or {}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    table = get_table()
    lambda_client = boto3.client("lambda")
    return _handle(event, table, lambda_client)


def _invoke_welcome_send(lambda_client, email: str, first_name: str, unsubscribe_token: str) -> None:
    """Fire-and-forget async invoke of the welcome-send Lambda (FR-3, ADR-0009).

    Wrapped so ANY failure here -- missing target config, IAM denial, throttling, an
    unreachable Lambda control plane -- is logged and never propagates: this function's
    caller has already committed the `confirmed` state and must still return its 200
    page regardless (FR-9/AC-8). `lambda_client=None` (the default a bare unit test may
    pass) is treated the same as "no target configured" -- a deliberate no-op, not an
    error, so existing tests that don't care about the welcome send aren't forced to
    stub one out.
    """
    if lambda_client is None:
        return
    if not WELCOME_FUNCTION_NAME:
        logger.warning("CONFIRM_WELCOME_INVOKE_SKIPPED_NO_TARGET email=%s", email)
        return
    payload = {"email": email, "firstName": first_name, "unsubscribeToken": unsubscribe_token}
    try:
        lambda_client.invoke(
            FunctionName=WELCOME_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        logger.info("CONFIRM_WELCOME_INVOKED email=%s", email)
    except Exception as e:  # noqa: BLE001 - must never fail confirmation (FR-9)
        logger.error("CONFIRM_WELCOME_INVOKE_FAILED email=%s error=%r", email, e)


def _handle(event: dict[str, Any], table, lambda_client=None) -> dict[str, Any]:
    params = _query_params(event)
    email = normalize_email(params.get("email", ""))
    token = params.get("token", "") or ""

    if not email or not token:
        logger.info("CONFIRM_MISSING_PARAMS")
        return build_response(400, _INVALID_OR_EXPIRED_BODY)

    item = get_subscriber(table, email)
    if item is None:
        logger.info("CONFIRM_NO_SUCH_SUBSCRIBER")
        return build_response(400, _INVALID_OR_EXPIRED_BODY)

    # Already confirmed: re-clicking a (possibly cached) link is a benign idempotent no-op
    # that shows the same confirmed page, per ADR-0003.
    if item.get("status") == STATUS_CONFIRMED:
        logger.info("CONFIRM_ALREADY_CONFIRMED email=%s", email)
        return build_response(200, _CONFIRMED_BODY)

    stored_token = item.get("confirmToken")
    expires_at = item.get("confirmTokenExpiresAt") or 0

    token_ok = item.get("status") == STATUS_PENDING and constant_time_equals(token, stored_token)
    not_expired = now_epoch() < int(expires_at) if expires_at else False

    if not (token_ok and not_expired):
        logger.info("CONFIRM_INVALID_OR_EXPIRED email=%s", email)
        return build_response(400, _INVALID_OR_EXPIRED_BODY)

    unsubscribe_token = generate_token()
    try:
        table.update_item(
            Key={"email": email},
            UpdateExpression=(
                "SET #status = :confirmed, confirmedAt = :now, unsubscribeToken = :unsub_token "
                "REMOVE confirmToken, confirmTokenExpiresAt"
            ),
            # Guards the actual `pending`->`confirmed` transition against a second,
            # near-simultaneous request (e.g. a duplicate link-scanner GET, or a user
            # double-clicking) racing this one: both requests can read the same `pending`
            # snapshot before either writes, so the read-then-check above is not enough on
            # its own (flagged as a risk in docs/prd/instant-welcome-brief.md §7). Only the
            # request whose UpdateItem actually flips the still-`pending` row wins and
            # invokes the welcome send; the loser's write is rejected here and falls
            # through to the idempotent "already confirmed" response below, matching
            # FR-7/AC-6 (sent-once).
            ConditionExpression="#status = :pending",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":confirmed": STATUS_CONFIRMED,
                ":now": now_epoch(),
                ":unsub_token": unsubscribe_token,
                ":pending": STATUS_PENDING,
            },
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("CONFIRM_LOST_TRANSITION_RACE email=%s", email)
            return build_response(200, _CONFIRMED_BODY)
        raise
    logger.info("CONFIRM_SUCCESS email=%s", email)

    # Only on this actual pending->confirmed transition branch (never the idempotent
    # already-confirmed re-click branch above, FR-7/AC-6), and only AFTER the
    # UpdateItem above has succeeded -- never before, and never allowed to affect the
    # response below (FR-9/AC-8).
    _invoke_welcome_send(lambda_client, email, item.get("firstName", ""), unsubscribe_token)

    return build_response(200, _CONFIRMED_BODY)
