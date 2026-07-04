"""Async welcome-send target, invoked by the confirm Lambda (docs/adr/0009) on a new
subscriber's actual `pending` -> `confirmed` transition. Sends the most recently
archived brief (HTML body plus the narrated MP3, when the audio pointer resolves to a
still-existing S3 object), or a welcome-only email when no brief has ever been archived
(cold start, PRD docs/prd/instant-welcome-brief.md FR-8).

Payload (the confirm Lambda's `InvocationType='Event'` body -- metadata only, never the
MP3 itself, per ADR-0009):
    {"email": "...", "firstName": "...", "unsubscribeToken": "..."}

Runs with the least-privilege role in `brief_subscribers/stack.py`'s
`WelcomeSendFunctionRole`: SES send restricted to `From=aibriefing@mschweier.com`, and
S3 read scoped to `briefs/*` + `audio/*` on the one bucket. PRD FR-13/FR-14's grants
land HERE (not on the confirm Lambda) per ADR-0009's explicit deviation from the PRD's
literal wording.
"""

from __future__ import annotations

import logging
import os
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

import feedback_token
import latest_brief
from subscriber_common import normalize_email, weekday_send_time_label

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SENDER = "aibriefing@mschweier.com"
SES_REGION = "us-east-1"
WELCOME_SUBJECT = "Welcome to the Daily AI Brief"
MP3_ATTACHMENT_FILENAME = "daily-ai-brief.mp3"
# Mirrors deploy/managed-agent/pipeline/audio_email.py's constant of the same name/value --
# two independent deploy units, kept in sync by hand (same convention as latest_brief.py's
# duplicated-constants docstring). An oversized MP3 is dropped, not attached -- the subscriber
# still gets the written brief -- rather than risking an SES raw-message-size rejection that
# would otherwise cost them the whole email (ADR-0009's "Verification note" flagged this as an
# open item for the implementer to close).
MAX_AUDIO_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15 MB

# Set via CDK environment once the HTTP API exists (mirrors subscribe/handler.py's
# API_BASE_URL wiring) -- used to build the per-subscriber unsubscribe link.
SUBSCRIBERS_API_BASE_URL = os.environ.get("SUBSCRIBERS_API_BASE_URL", "")

# Feedback link (docs/prd/reader-feedback.md FR-5, ADR-0011, ADR-0012 §B): both must be
# set for a feedback link to be embedded at all -- see _get_feedback_signing_secret()
# and _feedback_link() below. Neither being set is the expected state before the
# feedback stack is deployed and wired in (backward-compatible, never blocks the send).
FEEDBACK_TOKEN_SECRET_ARN = os.environ.get("FEEDBACK_TOKEN_SECRET_ARN", "")
FEEDBACK_BASE_URL = os.environ.get("FEEDBACK_BASE_URL", "")

_feedback_secret_cache: str | None = None
_feedback_secret_fetch_attempted = False


def _get_feedback_signing_secret() -> str | None:
    """Best-effort, cached-once-per-cold-start fetch of the feedback-token signing
    secret, mirroring the launcher's `_get_secret` shape
    (deploy/managed-agent/microvm/launcher/launcher.py). Returns None (never raises)
    when FEEDBACK_TOKEN_SECRET_ARN is unset or the fetch fails -- the welcome send must
    never be blocked by this (same fail-safe convention as the MP3 fetch above)."""
    global _feedback_secret_cache, _feedback_secret_fetch_attempted
    if _feedback_secret_fetch_attempted:
        return _feedback_secret_cache
    _feedback_secret_fetch_attempted = True
    if not FEEDBACK_TOKEN_SECRET_ARN:
        return None
    try:
        client = boto3.client("secretsmanager", region_name=SES_REGION)
        response = client.get_secret_value(SecretId=FEEDBACK_TOKEN_SECRET_ARN)
        _feedback_secret_cache = response["SecretString"]
    except Exception as e:  # noqa: BLE001 - fail-safe: a secret-fetch glitch must never block the send
        logger.warning("FEEDBACK_LINK_SKIPPED: secret fetch failed error=%r", e)
        _feedback_secret_cache = None
    return _feedback_secret_cache


def _feedback_link(email: str, brief_date: str | None) -> str | None:
    """Build a feedback link for `email` attributed to `brief_date`, or return None if
    unavailable for any reason (missing config, secret-fetch failure, token-generation
    failure, or no brief_date -- the cold-start case has no edition to attribute to).
    Never raises."""
    if not FEEDBACK_BASE_URL:
        logger.info("FEEDBACK_LINK_SKIPPED: FEEDBACK_BASE_URL not set")
        return None
    if not brief_date:
        logger.info("FEEDBACK_LINK_SKIPPED: no brief date to attribute (cold start)")
        return None
    secret = _get_feedback_signing_secret()
    if not secret:
        logger.info("FEEDBACK_LINK_SKIPPED: signing secret unavailable")
        return None
    try:
        token = feedback_token.generate(secret, email, brief_date)
    except Exception as e:  # noqa: BLE001 - fail-safe: token generation must never block the send
        logger.warning("FEEDBACK_LINK_SKIPPED: token generation failed error=%r", e)
        return None
    base = FEEDBACK_BASE_URL.rstrip("/")
    return f"{base}/?t={quote(token)}"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    ses_client = boto3.client("ses", region_name=SES_REGION)
    s3_client = boto3.client("s3", region_name=SES_REGION)
    return _handle(event, ses_client, s3_client)


def _unsubscribe_link(email: str, token: str) -> str:
    # Mirrors deploy/managed-agent/pipeline/audio_email.py's _unsubscribe_link exactly
    # (same query-string shape, same URL-encoding of both parts).
    base = SUBSCRIBERS_API_BASE_URL.rstrip("/")
    return f"{base}/unsubscribe?email={quote(email)}&token={quote(token)}"


def _disclaimer_html() -> str:
    return (
        '<p style="margin:0;">This brief is curated and written by an AI agent, '
        "which may make mistakes. For anything important, please verify with "
        "original sources and do your own research.</p>"
    )


def _welcome_header_html() -> str:
    """FR-4's exact drafted copy, rendered from the centralized send-time source
    (subscriber_common.weekday_send_time_label(), FR-10/FR-11) -- not a hardcoded time
    string. Mirrors the visual shape of audio_email.py's _html_with_header banner."""
    time_label = weekday_send_time_label()
    return (
        '<div style="background:#f5f5f7;border-radius:8px;padding:12px 16px;'
        'margin-bottom:20px;font-size:12px;color:#555;line-height:1.5;">'
        '<p style="margin:0 0 6px 0;"><strong>Welcome to the Daily AI Brief!</strong> '
        "This is the most recent edition — the same one that went out to subscribers "
        f"today. Going forward, you'll receive a fresh edition every weekday at "
        f"<strong>{time_label}</strong>.</p>"
        f"{_disclaimer_html()}"
        "</div>"
    )


def _cold_start_header_html() -> str:
    """FR-8's cold-start welcome-only copy: confirms the subscription and states the
    schedule, with no brief content implied (the caller sends no brief body alongside
    this header in the cold-start case)."""
    time_label = weekday_send_time_label()
    return (
        '<div style="background:#f5f5f7;border-radius:8px;padding:12px 16px;'
        'margin-bottom:20px;font-size:12px;color:#555;line-height:1.5;">'
        '<p style="margin:0 0 6px 0;"><strong>Welcome to the Daily AI Brief!</strong> '
        "You're subscribed. We haven't published an edition yet, but you'll receive a "
        f"fresh one every weekday at <strong>{time_label}</strong>.</p>"
        f"{_disclaimer_html()}"
        "</div>"
    )


def _html_with_unsubscribe_footer(html_body: str, unsubscribe_link: str) -> str:
    # Verbatim mirror of audio_email.py's _html_with_unsubscribe_footer (FR-6: same
    # framing regular daily emails carry).
    footer = (
        '<hr><p style="font-size:12px;color:#666;">'
        "You are receiving this because you subscribed to the daily AI brief. "
        f'<a href="{unsubscribe_link}">Unsubscribe</a> at any time.</p>'
    )
    return html_body + footer


def _html_with_feedback_link(html_body: str, feedback_link: str | None) -> str:
    """Mirrors audio_email.py's _html_with_feedback_link exactly (same placement near
    the unsubscribe footer, same tone) -- a no-op when no link is available (PRD FR-5,
    fail-safe: never fails or degrades the send)."""
    if not feedback_link:
        return html_body
    footer = (
        '<p style="font-size:12px;color:#666;">'
        f'Have thoughts on today\'s brief? <a href="{feedback_link}">Share feedback</a>.</p>'
    )
    return html_body + footer


def _cold_start_body(unsubscribe_link: str, feedback_link: str | None = None) -> str:
    body = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
        "<p>Thanks for confirming your subscription!</p>"
        "</body></html>"
    )
    body = _html_with_unsubscribe_footer(_cold_start_header_html() + body, unsubscribe_link)
    return _html_with_feedback_link(body, feedback_link)


def _welcome_body(brief_html: str, unsubscribe_link: str, feedback_link: str | None = None) -> str:
    body = _html_with_unsubscribe_footer(_welcome_header_html() + brief_html, unsubscribe_link)
    return _html_with_feedback_link(body, feedback_link)


def _fetch_audio_bytes(s3_client, audio_key: str | None) -> bytes | None:
    """Best-effort MP3 fetch: an absent pointer (audio_key=None) or a pointer resolving
    to a gone object (expired under the 7-day audio/ lifecycle, or otherwise missing --
    FR-5/AC-5) both resolve to None, never an exception that would block the send."""
    if not audio_key:
        return None
    try:
        obj = s3_client.get_object(Bucket=latest_brief.BUCKET, Key=audio_key)
        content_length = obj.get("ContentLength", 0)
        if content_length > MAX_AUDIO_ATTACHMENT_BYTES:
            # Checked before .read() so an oversized object's body is never pulled over
            # the wire just to be discarded.
            logger.info("WELCOME_AUDIO_TOO_LARGE key=%s bytes=%d", audio_key, content_length)
            return None
        return obj["Body"].read()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            logger.info("WELCOME_AUDIO_POINTER_STALE key=%s", audio_key)
        else:
            logger.warning("WELCOME_AUDIO_FETCH_FAILED key=%s error=%r", audio_key, e)
        return None
    except Exception as e:  # noqa: BLE001 - any other fetch failure degrades the same way
        logger.warning("WELCOME_AUDIO_FETCH_FAILED key=%s error=%r", audio_key, e)
        return None


def _build_message(recipient: str, html_body: str, mp3_bytes: bytes | None) -> MIMEMultipart:
    # Mirrors audio_email.py's _build_message (same MIME shape: multipart/mixed wrapping
    # a multipart/alternative HTML part, plus an optional MP3 attachment part).
    msg = MIMEMultipart("mixed")
    msg["Subject"] = WELCOME_SUBJECT
    msg["From"] = SENDER
    msg["To"] = recipient
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)
    if mp3_bytes is not None:
        part = MIMEApplication(mp3_bytes, _subtype="mpeg")
        part.add_header("Content-Disposition", "attachment", filename=MP3_ATTACHMENT_FILENAME)
        msg.attach(part)
    return msg


def _handle(event: dict[str, Any], ses_client, s3_client) -> dict[str, Any]:
    email = normalize_email(event.get("email", ""))
    unsubscribe_token = event.get("unsubscribeToken") or ""

    if not email or not unsubscribe_token:
        logger.error(
            "WELCOME_SEND_MISSING_FIELDS email_present=%s token_present=%s", bool(email), bool(unsubscribe_token)
        )
        return {"sent": False, "reason": "missing_fields"}

    unsubscribe_link = _unsubscribe_link(email, unsubscribe_token)
    brief = latest_brief.resolve_latest_brief(s3_client)

    if not brief.found:
        # FR-8/AC-7: cold start -- welcome-only, no brief content, no audio, and no
        # feedback link either (no edition exists yet to attribute feedback to --
        # _feedback_link() returns None whenever brief_date is None).
        html_body = _cold_start_body(unsubscribe_link, _feedback_link(email, None))
        mp3_bytes = None
    else:
        feedback_link = _feedback_link(email, brief.date)
        html_body = _welcome_body(brief.html, unsubscribe_link, feedback_link)
        mp3_bytes = _fetch_audio_bytes(s3_client, brief.audio_key)

    msg = _build_message(email, html_body, mp3_bytes)
    try:
        r = ses_client.send_raw_email(Source=SENDER, Destinations=[email], RawMessage={"Data": msg.as_string()})
    except Exception as e:
        # Deliberately RE-RAISED, not swallowed: this Lambda is invoked
        # InvocationType='Event' (ADR-0009), so a raised exception here is exactly what
        # gives a transient SES failure Lambda's automatic async-invoke retries (2 by
        # default) and, if configured, an on-failure destination for observability of
        # sends that ultimately fail -- the retriability ADR-0009 calls out as a benefit
        # of this design over an inline synchronous send. It never affects the confirm
        # Lambda's already-returned response (FR-9) -- that isolation is structural
        # (the async invoke already returned before this code runs), not something this
        # handler needs to defend against.
        logger.error("WELCOME_SEND_FAILED email=%s error=%r", email, e)
        raise

    logger.info(
        "WELCOME_SENT email=%s message_id=%s audio_attached=%s cold_start=%s",
        email,
        r.get("MessageId"),
        mp3_bytes is not None,
        not brief.found,
    )
    return {"sent": True, "message_id": r.get("MessageId")}
