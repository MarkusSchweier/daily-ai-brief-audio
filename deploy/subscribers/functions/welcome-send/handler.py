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
import re
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

import feedback_token
import latest_brief
from subscriber_common import normalize_email

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


# --- HTML chrome: kept EXACTLY in sync with the daily mail's chrome
# (deploy/delivery/functions/deliver/delivery_core.py's derive_html slots +
# _html_with_header / _html_with_unsubscribe_footer) so a welcome email looks IDENTICAL
# to a regular daily email -- with ONE addition: the welcome intro at the very top
# (owner request 2026-07-07). Hand-duplicated (this Lambda cannot import the delivery
# deploy unit); a change to the daily chrome there MUST be mirrored here, same convention
# as feedback_token.py's hand-duplication across deploy units.
SUBSCRIBE_SITE_URL = "https://briefing.mschweier.com"
_HEADER_SLOT = "<!--BRIEF_HEADER_SLOT-->"
_FOOTER_SLOT = "<!--BRIEF_FOOTER_SLOT-->"
_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _daily_header_banner_html(feedback_link: str | None) -> str:
    """EXACT mirror of delivery_core._html_with_header's inner banner: feedback prompt +
    forward/subscribe prompt + AI-curation disclaimer, in the flush divider-preheader
    style (text aligned with the brief body)."""
    feedback_line = (
        f'<p style="margin:0 0 6px 0;">💬 Have thoughts on today\'s brief? '
        f'<a href="{feedback_link}">Share feedback</a> — we process every submission.</p>'
        if feedback_link
        else ""
    )
    return (
        '<div style="margin:0 0 20px 0;padding:0 0 16px 0;border-bottom:1px solid #eeeeee;'
        'font-size:13px;color:#666;line-height:1.5;">'
        f"{feedback_line}"
        '<p style="margin:0 0 6px 0;">📬 Received this as a forward? Anyone can get '
        f'their own daily copy — <a href="{SUBSCRIBE_SITE_URL}">subscribe here</a>.</p>'
        '<p style="margin:0;">This brief is curated and written by an AI agent, '
        "which may make mistakes. For anything important, please verify with "
        "original sources and do your own research.</p>"
        "</div>"
    )


def _unsubscribe_footer_html(unsubscribe_link: str) -> str:
    """EXACT mirror of delivery_core._html_with_unsubscribe_footer's footer."""
    return (
        '<hr><p style="font-size:13px;color:#666;">'
        "You are receiving this because you subscribed to the daily AI brief. "
        f'<a href="{unsubscribe_link}">Unsubscribe</a> at any time.</p>'
    )


# The ONE difference from a regular daily email -- the welcome intro at the very top.
# "in the morning" (not a specific time) is the owner's chosen copy (2026-07-07).
_WELCOME_INTRO_HTML = (
    '<p style="margin:0 0 18px 0;color:#1a1a1a;font-size:16px;line-height:1.55;">'
    "<strong>Welcome to the Daily AI Brief!</strong> This is the most recent edition — "
    "the same one that went out to subscribers today. Going forward, you'll receive a "
    "fresh edition every weekday in the morning."
    "</p>"
)
_COLD_START_INTRO_HTML = (
    '<p style="margin:0 0 18px 0;color:#1a1a1a;font-size:16px;line-height:1.55;">'
    "<strong>Welcome to the Daily AI Brief!</strong> You're subscribed. We haven't "
    "published an edition yet, but you'll receive a fresh one every weekday in the morning."
    "</p>"
)


def _insert_after_body_open(doc: str, fragment: str) -> str:
    m = _BODY_OPEN_RE.search(doc)
    return doc[: m.end()] + fragment + doc[m.end() :] if m else fragment + doc


def _insert_before_body_close(doc: str, fragment: str) -> str:
    m = _BODY_CLOSE_RE.search(doc)
    return doc[: m.start()] + fragment + doc[m.start() :] if m else doc + fragment


def _fill_or_insert(doc: str, slot: str, fragment: str, *, at_top: bool) -> str:
    """Fill the derive_html slot (post-cut-over archived brief.html) with `fragment`, or
    fall back to inserting inside <body> for an old-format (pre-cut-over, no-slot)
    archived brief.html -- so the welcome mail composes correctly against either."""
    if slot in doc:
        return doc.replace(slot, fragment, 1)
    return _insert_after_body_open(doc, fragment) if at_top else _insert_before_body_close(doc, fragment)


def _welcome_body(brief_html: str, unsubscribe_link: str, feedback_link: str | None = None) -> str:
    """A welcome email = the archived brief rendered EXACTLY like a daily subscriber email
    (same header banner + unsubscribe footer, filled into the same slots), PLUS the
    welcome intro at the very top of the header."""
    header = _WELCOME_INTRO_HTML + _daily_header_banner_html(feedback_link)
    composed = _fill_or_insert(brief_html, _HEADER_SLOT, header, at_top=True)
    return _fill_or_insert(composed, _FOOTER_SLOT, _unsubscribe_footer_html(unsubscribe_link), at_top=False)


def _cold_start_body(unsubscribe_link: str, feedback_link: str | None = None) -> str:
    """Cold start: no archived brief yet -- a minimal self-contained document carrying the
    same chrome + a cold-start intro (no brief content)."""
    doc = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:28px 24px;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        'color:#1a1a1a;font-size:17px;line-height:1.65;">'
        f"{_COLD_START_INTRO_HTML}{_daily_header_banner_html(feedback_link)}"
        "</body></html>"
    )
    return _insert_before_body_close(doc, _unsubscribe_footer_html(unsubscribe_link))


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
