"""POST /subscribe — create (or refresh) a pending subscriber and send a confirm email.

See docs/prd/public-subscriptions.md FR-5..FR-11 and docs/adr/0003 for the state-machine
this implements. Runs with the least-privilege role in docs/adr/0002 §A (PutItem/GetItem/
UpdateItem on the table only, plus SES send restricted to From=aibriefing@mschweier.com).
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any
from urllib.parse import quote

import boto3

from subscriber_common import (
    CONFIRM_TOKEN_TTL_SECONDS,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    build_response,
    clamp_name,
    generate_token,
    get_subscriber,
    get_table,
    is_valid_email,
    normalize_email,
    now_epoch,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SENDER = "aibriefing@mschweier.com"
SES_REGION = "us-east-1"

# Set via CDK context/env once the confirm site's API base + confirm route are known.
# Kept as a module-level constant (not hardcoded inline in the email body builder) so it
# is easy to find/update — see deploy/subscribers/README.md for how this is configured.
import os

API_BASE_URL = os.environ.get("API_BASE_URL", "")

# Generic, non-leaking response bodies (AC-9, AC-14): the caller cannot distinguish
# "new signup", "already pending", or "already confirmed" from the HTTP response.
_NEUTRAL_SUCCESS_BODY = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Check your inbox</title></head>"
    "<body><h1>Almost there</h1><p>If that address is new to us, we've sent a confirmation "
    "email — click the link inside to start receiving the daily AI brief. If you're already "
    "signed up, there's nothing further to do.</p></body></html>"
)
_INVALID_EMAIL_BODY = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Invalid email</title></head>"
    "<body><h1>That email address doesn't look right</h1><p>Please go back and check it, "
    "then try again.</p></body></html>"
)


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    content_type = ""
    headers = event.get("headers") or {}
    for k, v in headers.items():
        if k.lower() == "content-type":
            content_type = (v or "").lower()
            break
    if "application/json" in content_type or not content_type:
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}
    # application/x-www-form-urlencoded fallback for a plain HTML form without JS
    from urllib.parse import parse_qsl

    return {k: v for k, v in parse_qsl(raw)}


def _send_confirmation_email(ses_client, email: str, first_name: str, token: str) -> None:
    # URL-encode email/token: local-parts may legally contain "&", "=", "+", etc. (the
    # regex-based validator does not forbid them), and an unescaped value here would
    # corrupt the query string / produce a broken confirm link. Mirrors the same
    # urllib.parse.quote() used for the unsubscribe link in deploy/audio_email.py.
    confirm_link = f"{API_BASE_URL}/confirm?email={quote(email)}&token={quote(token)}"
    text_body = (
        f"Hi {first_name or 'there'},\n\n"
        "Please confirm your subscription to the daily AI brief (delivered as written text "
        "plus narrated audio, every day). This link expires in about 48 hours:\n\n"
        f"{confirm_link}\n\n"
        "If you didn't request this, you can ignore this email.\n"
    )
    html_body = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
        f"<p>Hi {html.escape(first_name) or 'there'},</p>"
        "<p>Please confirm your subscription to the <strong>daily AI brief</strong> "
        "(written text plus narrated audio, every day). This link expires in about "
        "48 hours:</p>"
        f'<p><a href="{html.escape(confirm_link)}">Confirm my subscription</a></p>'
        "<p>If you didn't request this, you can ignore this email.</p>"
        "</body></html>"
    )
    ses_client.send_email(
        Source=SENDER,
        Destination={"ToAddresses": [email]},
        Message={
            "Subject": {"Data": "Confirm your subscription to the daily AI brief", "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text_body, "Charset": "UTF-8"},
                "Html": {"Data": html_body, "Charset": "UTF-8"},
            },
        },
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    table = get_table()
    ses_client = boto3.client("ses", region_name=SES_REGION)
    return _handle(event, table, ses_client)


def _handle(event: dict[str, Any], table, ses_client) -> dict[str, Any]:
    payload = _parse_body(event)

    # Honeypot: a bot-filled hidden field. Silently succeed, do nothing (FR-5, AC-14).
    if (payload.get("website") or "").strip():
        logger.info("SUBSCRIBE_HONEYPOT_TRIPPED")
        return build_response(200, _NEUTRAL_SUCCESS_BODY)

    email = normalize_email(payload.get("email", ""))
    first_name = clamp_name(payload.get("firstName", ""))
    last_name = clamp_name(payload.get("lastName", ""))

    if not is_valid_email(email):
        logger.info("SUBSCRIBE_INVALID_EMAIL")
        return build_response(400, _INVALID_EMAIL_BODY)

    existing = get_subscriber(table, email)

    if existing and existing.get("status") == STATUS_CONFIRMED:
        # AC-9: do not reset anything, do not leak status beyond the neutral message.
        logger.info("SUBSCRIBE_ALREADY_CONFIRMED email=%s", email)
        return build_response(200, _NEUTRAL_SUCCESS_BODY)

    # New signup, unconfirmed re-submit (AC-10), or re-subscribe after unsubscribe (AC-15):
    # all land on a fresh pending row with a fresh token.
    token = generate_token()
    expires_at = now_epoch() + CONFIRM_TOKEN_TTL_SECONDS
    source_ip = (
        (event.get("requestContext", {}).get("http", {}) or {}).get("sourceIp")
        or (event.get("requestContext", {}) or {}).get("identity", {}).get("sourceIp")
        or ""
    )

    item = {
        "email": email,
        "firstName": first_name,
        "lastName": last_name,
        "status": STATUS_PENDING,
        "confirmToken": token,
        "confirmTokenExpiresAt": expires_at,
        "createdAt": now_epoch(),
        "sourceIp": source_ip,
    }
    table.put_item(Item=item)

    try:
        _send_confirmation_email(ses_client, email, first_name, token)
    except Exception as exc:  # noqa: BLE001 - log and still return the neutral response
        logger.error("SUBSCRIBE_SES_SEND_FAILED email=%s error=%r", email, exc)
        # The row exists; the user can re-submit to trigger a resend (AC-10). We still
        # return the neutral success page so we never leak whether the address existed.

    logger.info("SUBSCRIBE_PENDING_CREATED email=%s", email)
    return build_response(200, _NEUTRAL_SUCCESS_BODY)
