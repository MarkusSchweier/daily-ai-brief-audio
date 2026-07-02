"""GET /unsubscribe?email=&token= — mark a subscriber unsubscribed, idempotently.

See docs/prd/public-subscriptions.md FR-16..FR-18, AC-4, AC-12 and docs/adr/0003. Runs
with the least-privilege role in docs/adr/0002 A (GetItem/UpdateItem on the table only,
no SES).
"""

from __future__ import annotations

import logging
from typing import Any

from subscriber_common import (
    STATUS_UNSUBSCRIBED,
    build_response,
    constant_time_equals,
    get_subscriber,
    get_table,
    normalize_email,
    now_epoch,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_INVALID_BODY = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Link invalid</title></head>"
    "<body><h1>This unsubscribe link is invalid</h1>"
    "<p>If you're still receiving the brief and want to stop, please use the "
    "unsubscribe link in your most recent email.</p></body></html>"
)
_UNSUBSCRIBED_BODY = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Unsubscribed</title></head>"
    "<body><h1>You're unsubscribed</h1>"
    "<p>You won't receive any further daily AI briefs. You can subscribe again at any "
    "time from the site.</p></body></html>"
)


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    return event.get("queryStringParameters") or {}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    table = get_table()
    return _handle(event, table)


def _handle(event: dict[str, Any], table) -> dict[str, Any]:
    params = _query_params(event)
    email = normalize_email(params.get("email", ""))
    token = params.get("token", "") or ""

    if not email or not token:
        logger.info("UNSUBSCRIBE_MISSING_PARAMS")
        return build_response(400, _INVALID_BODY)

    item = get_subscriber(table, email)
    if item is None:
        logger.info("UNSUBSCRIBE_NO_SUCH_SUBSCRIBER")
        return build_response(400, _INVALID_BODY)

    # Idempotent: already-unsubscribed + a matching token re-click shows the same
    # confirmation, no error, no re-subscription (AC-12).
    if item.get("status") == STATUS_UNSUBSCRIBED:
        if constant_time_equals(token, item.get("unsubscribeToken")):
            logger.info("UNSUBSCRIBE_ALREADY_UNSUBSCRIBED email=%s", email)
            return build_response(200, _UNSUBSCRIBED_BODY)
        logger.info("UNSUBSCRIBE_TOKEN_MISMATCH email=%s", email)
        return build_response(400, _INVALID_BODY)

    if not constant_time_equals(token, item.get("unsubscribeToken")):
        logger.info("UNSUBSCRIBE_TOKEN_MISMATCH email=%s", email)
        return build_response(400, _INVALID_BODY)

    table.update_item(
        Key={"email": email},
        UpdateExpression="SET #status = :unsub, unsubscribedAt = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":unsub": STATUS_UNSUBSCRIBED, ":now": now_epoch()},
    )
    logger.info("UNSUBSCRIBE_SUCCESS email=%s", email)
    return build_response(200, _UNSUBSCRIBED_BODY)
