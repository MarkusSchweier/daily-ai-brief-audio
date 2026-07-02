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


SUBSCRIBE_SITE_URL = "https://briefing.mschweier.com"

# Same palette as deploy/subscribers/site/styles.css, inlined here (not linked) so these
# transactional pages render correctly even if the main site/CDN is ever unreachable.
_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #1a1a2e;
    line-height: 1.55;
    background:
      radial-gradient(60rem 60rem at 12% -10%, #fbcfe8 0%, transparent 55%),
      radial-gradient(50rem 50rem at 110% 10%, #bfdbfe 0%, transparent 50%),
      radial-gradient(70rem 70rem at 50% 120%, #ddd6fe 0%, transparent 60%),
      #f5f3ff;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1.5rem;
  }}
  .card {{
    max-width: 30rem;
    width: 100%;
    background: #ffffff;
    border: 1px solid rgba(124, 58, 237, 0.12);
    border-radius: 1.1rem;
    box-shadow: 0 12px 30px rgba(76, 29, 149, 0.08);
    padding: 2rem 1.75rem;
    text-align: center;
  }}
  .eyebrow {{
    display: inline-block;
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: #3730a3;
    margin-bottom: 0.75rem;
  }}
  h1 {{ font-size: 1.4rem; margin: 0 0 0.75rem; color: #1a1a2e; }}
  p {{ font-size: 0.95rem; color: #5b5b76; margin: 0 0 0.5rem; }}
  a {{ color: #3730a3; }}
</style>
</head>
<body>
<div class="card">
  <span class="eyebrow">The Daily AI Brief</span>
  <h1>{heading}</h1>
  {message_html}
</div>
</body>
</html>"""


def render_page(title: str, heading: str, message_html: str) -> str:
    """Wrap a confirm/unsubscribe/subscribe response in the shared page shell.

    Visual-only helper — status codes and control flow in each handler are unaffected.
    See deploy/subscribers/site/styles.css for the same palette used on the main site.
    """
    return _PAGE_TEMPLATE.format(title=title, heading=heading, message_html=message_html)


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
    "SUBSCRIBE_SITE_URL",
    "render_page",
]
