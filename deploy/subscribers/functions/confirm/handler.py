"""GET /confirm?email=&token= — activate a pending subscription.

See docs/prd/public-subscriptions.md FR-9..FR-11, AC-2, AC-11 and docs/adr/0003. Runs with
the least-privilege role in docs/adr/0002 A (GetItem/UpdateItem on the table only, no SES).
"""

from __future__ import annotations

import logging
from typing import Any

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
    return _handle(event, table)


def _handle(event: dict[str, Any], table) -> dict[str, Any]:
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
    table.update_item(
        Key={"email": email},
        UpdateExpression=(
            "SET #status = :confirmed, confirmedAt = :now, unsubscribeToken = :unsub_token "
            "REMOVE confirmToken, confirmTokenExpiresAt"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":confirmed": STATUS_CONFIRMED,
            ":now": now_epoch(),
            ":unsub_token": unsubscribe_token,
        },
    )
    logger.info("CONFIRM_SUCCESS email=%s", email)
    return build_response(200, _CONFIRMED_BODY)
