"""Shared helpers for the subscribe / confirm / unsubscribe Lambdas.

Deployed as a Lambda Layer (this file lives at the layer's `python/` root, so it is
importable as a top-level module from `/opt/python` in every function's runtime). Kept
dependency-light (stdlib + boto3 only, both available in the Lambda Python runtime).
See docs/adr/0003-subscriber-data-model-and-tokens.md for the schema and token design
this implements.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from typing import Any, Optional

import boto3

TABLE_NAME = os.environ.get("SUBSCRIBERS_TABLE_NAME", "brief-subscribers")
CONFIRM_TOKEN_TTL_SECONDS = 48 * 60 * 60  # ~48h, per PRD FR-8/FR-10 and ADR-0003

# Deliberately conservative RFC-5322-ish check: good enough to reject obvious typos/garbage
# without trying to be a full email grammar validator (AC-13).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_UNSUBSCRIBED = "unsubscribed"

MAX_NAME_LENGTH = 100


def get_table():
    """Return the boto3 DynamoDB Table resource for the subscribers table."""
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(TABLE_NAME)


def normalize_email(raw_email: str) -> str:
    """Normalize an email address the same way everywhere (lowercase, trimmed).

    Must be applied identically on subscribe/confirm/unsubscribe/fan-out, or lookups miss
    (ADR-0003 developer note).
    """
    return (raw_email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    """Syntactic-only validity check; not a deliverability guarantee."""
    if not email or len(email) > 254:
        return False
    return bool(_EMAIL_RE.match(email))


def generate_token() -> str:
    """256-bit opaque URL-safe token (ADR-0003: not signed, stored + compared)."""
    return secrets.token_urlsafe(32)


def now_epoch() -> int:
    return int(time.time())


def constant_time_equals(a: Optional[str], b: Optional[str]) -> bool:
    """Timing-safe compare; treats missing values as unequal without short-circuiting."""
    if a is None or b is None:
        return False
    return hmac.compare_digest(a, b)


def clamp_name(name: str) -> str:
    """Trim and length-bound a free-text name field."""
    return (name or "").strip()[:MAX_NAME_LENGTH]


def build_response(status_code: int, body: str, content_type: str = "text/html; charset=utf-8") -> dict[str, Any]:
    """Shape a Lambda proxy response for API Gateway HTTP API."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": content_type},
        "body": body,
    }


def get_subscriber(table, email: str) -> Optional[dict[str, Any]]:
    resp = table.get_item(Key={"email": email})
    return resp.get("Item")


__all__ = [
    "TABLE_NAME",
    "CONFIRM_TOKEN_TTL_SECONDS",
    "MAX_NAME_LENGTH",
    "STATUS_PENDING",
    "STATUS_CONFIRMED",
    "STATUS_UNSUBSCRIBED",
    "get_table",
    "normalize_email",
    "is_valid_email",
    "generate_token",
    "now_epoch",
    "constant_time_equals",
    "clamp_name",
    "build_response",
    "get_subscriber",
]
