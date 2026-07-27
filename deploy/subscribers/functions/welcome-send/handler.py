"""Async welcome-send target, invoked by the confirm Lambda (docs/adr/0009) on a new
subscriber's actual `pending` -> `confirmed` transition. Sends the most recently
archived brief (HTML body plus an audio LINK, when the audio pointer resolves to a
still-existing S3 object -- PRD docs/prd/audio-link-delivery.md, ADR-0017), or a
welcome-only email when no brief has ever been archived (cold start, PRD
docs/prd/instant-welcome-brief.md FR-8).

Payload (the confirm Lambda's `InvocationType='Event'` body -- metadata only, never any
audio bytes, per ADR-0009 -- and ADR-0017 D8 means this Lambda never downloads audio
bytes at all any more, not even server-side):
    {"email": "...", "firstName": "...", "unsubscribeToken": "..."}

Runs with the least-privilege role in `brief_subscribers/stack.py`'s
`WelcomeSendFunctionRole`: SES send restricted to `From=aibriefing@mschweier.com`, and
S3 read scoped to `briefs/*` + `audio/*` on the one bucket. PRD FR-13/FR-14's grants
land HERE (not on the confirm Lambda) per ADR-0009's explicit deviation from the PRD's
literal wording. That same `s3:GetObject` grant on `audio/*` also authorizes the
`HeadObject` call ADR-0017 D1 relies on (HeadObject is authorized by GetObject) -- no
IAM change was needed for the audio-as-link feature.
"""

from __future__ import annotations

import logging
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

import audio_link
import feedback_token
import latest_brief
from subscriber_common import normalize_email

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SENDER = "aibriefing@mschweier.com"
SES_REGION = "us-east-1"
WELCOME_SUBJECT = "Welcome to the Daily AI Brief"
# The public host the audio-as-link feature serves audio/* from (PRD
# docs/prd/audio-link-delivery.md, ADR-0017 D6) -- the SAME host as
# SUBSCRIBE_SITE_URL below (the `/audio/*` behavior lives on this Lambda's OWN
# stack's distribution), a plain module constant rather than CDK context/an env
# var, mirroring SUBSCRIBE_SITE_URL's own precedent.
AUDIO_BASE_URL = "https://briefing.mschweier.com"

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
# (deploy/delivery/functions/deliver/delivery_core.py's derive_html _HEADER_SLOT +
# _html_with_header) so a welcome email looks IDENTICAL to a regular daily SUBSCRIBER
# email -- ONE top "meta" box carrying feedback + subscribe + unsubscribe + disclaimer at
# a single 14px size -- with ONE addition: the welcome intro as the first line of that
# box. Hand-duplicated (this Lambda cannot import the delivery deploy unit); a change to
# the daily chrome there MUST be mirrored here (same convention as feedback_token.py).
SUBSCRIBE_SITE_URL = "https://briefing.mschweier.com"
_HEADER_SLOT = "<!--BRIEF_HEADER_SLOT-->"
_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)

# The welcome intro -- the ONE difference from a regular daily email -- rendered as the
# first line INSIDE the meta box, at the SAME 14px as the rest (owner request: align the
# sizes in the top box). "in the morning" (no specific time) is the owner's chosen copy.
_WELCOME_INTRO_LINE = (
    '<p style="margin:0 0 12px 0;">'
    "<strong>Welcome to the Daily AI Brief!</strong> This is the most recent edition — "
    "the same one that went out to subscribers today. Going forward, you'll receive a "
    "fresh edition every weekday in the morning."
    "</p>"
)
_COLD_START_INTRO_LINE = (
    '<p style="margin:0 0 12px 0;">'
    "<strong>Welcome to the Daily AI Brief!</strong> You're subscribed. We haven't "
    "published an edition yet, but you'll receive a fresh one every weekday in the morning."
    "</p>"
)


def _meta_box_html(
    feedback_link: str | None, unsubscribe_link: str | None, *, intro_line: str = "", audio_line_html: str = ""
) -> str:
    """The WELCOME variant of delivery_core._html_with_header's top meta box. Order
    amended by ADR-0017 D11 at Gate 2 (2026-07-27, overriding the architect's literal
    reading of FR-13): [welcome intro] -> [audio link, 🎧] -> unsubscribe (✉️, when
    available) -> feedback (💬, when available) -> AI disclaimer -- deliberately NO
    forward/subscribe line (the recipient just subscribed; that line is the regular
    daily brief's, not the welcome mail's). Greeting-first is the right call here: the
    welcome email's whole job is to say "you're subscribed," so leading with a listen
    link would bury that (unlike the daily brief, where the audio line IS first --
    D11). `audio_line_html` defaults to `""` (rendering nothing) so the cold-start
    caller, which never resolves an audio link at all (AC-17), needs no special case.
    ALL lines at one 14px size, flush and divider-separated (aligned with the brief
    body)."""
    feedback_line = (
        f'<p style="margin:0 0 6px 0;">💬 Have thoughts on today\'s brief? '
        f'<a href="{feedback_link}">Share feedback</a> — we process every submission.</p>'
        if feedback_link
        else ""
    )
    unsubscribe_line = (
        f'<p style="margin:0 0 6px 0;">✉️ You\'re subscribed to the daily AI brief — '
        f'<a href="{unsubscribe_link}">unsubscribe</a> any time.</p>'
        if unsubscribe_link
        else ""
    )
    return (
        '<div style="margin:0 0 20px 0;padding:0 0 16px 0;border-bottom:1px solid #eeeeee;'
        'font-size:14px;color:#666;line-height:1.5;">'
        f"{intro_line}"
        f"{audio_line_html}"
        f"{unsubscribe_line}"
        f"{feedback_line}"
        '<p style="margin:0;">This brief is curated and written by an AI agent, '
        "which may make mistakes. For anything important, please verify with "
        "original sources and do your own research.</p>"
        "</div>"
    )


def _insert_after_body_open(doc: str, fragment: str) -> str:
    m = _BODY_OPEN_RE.search(doc)
    return doc[: m.end()] + fragment + doc[m.end() :] if m else fragment + doc


def _fill_header_slot(doc: str, box: str) -> str:
    """Fill the derive_html _HEADER_SLOT (post-cut-over archived brief.html) with the meta
    box, or fall back to inserting just inside <body> for an old-format (no-slot) brief."""
    if _HEADER_SLOT in doc:
        return doc.replace(_HEADER_SLOT, box, 1)
    return _insert_after_body_open(doc, box)


def _welcome_body(
    brief_html: str, unsubscribe_link: str, feedback_link: str | None = None, audio_line_html: str = ""
) -> str:
    """A welcome email = the archived brief rendered EXACTLY like a daily subscriber email
    (ONE meta box: audio + feedback + subscribe + unsubscribe + disclaimer), PLUS the
    welcome intro as the first line of that box. No separate footer -- unsubscribe lives
    in the box now, mirroring the daily mail."""
    box = _meta_box_html(feedback_link, unsubscribe_link, intro_line=_WELCOME_INTRO_LINE, audio_line_html=audio_line_html)
    return _fill_header_slot(brief_html, box)


def _cold_start_body(unsubscribe_link: str, feedback_link: str | None = None) -> str:
    """Cold start: no archived brief yet -- a minimal self-contained document carrying the
    same meta box + a cold-start intro (no brief content)."""
    box = _meta_box_html(feedback_link, unsubscribe_link, intro_line=_COLD_START_INTRO_LINE)
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:28px 24px;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        'color:#1a1a1a;font-size:17px;line-height:1.65;">'
        f"{box}"
        "</body></html>"
    )


def _resolve_audio_metadata(s3_client, audio_key: str | None) -> tuple[bool, str | None]:
    """`HeadObject` the pointer's audio key to confirm it's genuinely still there (the
    7-day `audio/` lifecycle may have expired it since the pointer was written) and read
    its S3 lifecycle `Expiration` header -- the file's REAL remaining expiry (PRD
    docs/prd/audio-link-delivery.md FR-14: never confirmation-date + 7, since the
    welcome email sends the LATEST edition, which may be up to seven days into its own
    lifecycle). Mirrors `deploy/delivery/functions/deliver/delivery_core.py`'s
    `resolve_audio_metadata()` (ADR-0017 D1/D8) -- kept as its own function here, not
    imported (this Lambda cannot import that deploy unit; see this module's header
    docstring on hand-duplication), so this Lambda's existing
    `WELCOME_AUDIO_POINTER_STALE`/`WELCOME_AUDIO_FETCH_FAILED` log distinction (PRD
    instant-welcome-brief.md FR-5/AC-5) is preserved verbatim rather than collapsed into
    delivery_core's more generic single log line.

    Returns `(audio_available, expiration_header)`. `audio_available=False` (with
    `expiration_header=None`) both when `audio_key` is `None` (no pointer at all) and
    when the `HeadObject` call fails for any reason -- including the expected common
    case, a stale pointer to an object the 7-day lifecycle already deleted. Never
    raises -- this must never block the send.

    SECURITY FINDING L1, fixed: the two failure log lines below used to record the
    FULL `audio_key`. Since this feature turns that key into a live PUBLIC URL, they
    now go through the shared `audio_link.redact_audio_key()` (ADR-0017 D9 step 4's
    key-hygiene rule, which covers runtime logs). The log-line NAMES
    (`WELCOME_AUDIO_POINTER_STALE`, `WELCOME_AUDIO_FETCH_FAILED`) are unchanged and
    stay greppable; only the key's rendering changed."""
    if not audio_key:
        return False, None
    try:
        response = s3_client.head_object(Bucket=latest_brief.BUCKET, Key=audio_key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            logger.info("WELCOME_AUDIO_POINTER_STALE key=%s", audio_link.redact_audio_key(audio_key))
        else:
            logger.warning(
                "WELCOME_AUDIO_FETCH_FAILED key=%s error=%r", audio_link.redact_audio_key(audio_key), e
            )
        return False, None
    except Exception as e:  # noqa: BLE001 - any other fetch failure degrades the same way
        logger.warning("WELCOME_AUDIO_FETCH_FAILED key=%s error=%r", audio_link.redact_audio_key(audio_key), e)
        return False, None
    return True, response.get("Expiration")


def _build_message(recipient: str, html_body: str) -> MIMEMultipart:
    # Mirrors audio_email.py's _build_message shape (multipart/mixed wrapping a
    # multipart/alternative HTML part). ADR-0017 D8: the MP3 attachment branch is
    # DELETED -- the audio is now a link inside the HTML body (_meta_box_html()),
    # never a MIME part.
    msg = MIMEMultipart("mixed")
    msg["Subject"] = WELCOME_SUBJECT
    msg["From"] = SENDER
    msg["To"] = recipient
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)
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
        # FR-8/AC-7: cold start -- welcome-only, no brief content, no audio line at all
        # (AC-17 -- not even the no-audio fallback line), and no feedback link either
        # (no edition exists yet to attribute feedback to -- _feedback_link() returns
        # None whenever brief_date is None).
        html_body = _cold_start_body(unsubscribe_link, _feedback_link(email, None))
        audio_link_included = False
        audio_link_expiry: str | None = None
    else:
        feedback_link = _feedback_link(email, brief.date)
        audio_available, expiration_header = _resolve_audio_metadata(s3_client, brief.audio_key)
        audio_link_result = audio_link.build_audio_link_result(
            audio_base_url=AUDIO_BASE_URL,
            audio_key=brief.audio_key,
            audio_available=audio_available,
            expiration_header=expiration_header,
        )
        html_body = _welcome_body(brief.html, unsubscribe_link, feedback_link, audio_link_result.html)
        audio_link_included = audio_link_result.link_included
        audio_link_expiry = audio_link_result.expiry_label

    msg = _build_message(email, html_body)
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

    # FR-17-equivalent structured log line (ADR-0017): records whether an audio link was
    # embedded and, when it was, the expiry date used -- so an audio-less welcome send is
    # diagnosable from CloudWatch logs alone.
    logger.info(
        "WELCOME_SENT email=%s message_id=%s audio_link_included=%s audio_link_expiry=%s cold_start=%s",
        email,
        r.get("MessageId"),
        audio_link_included,
        audio_link_expiry,
        not brief.found,
    )
    return {"sent": True, "message_id": r.get("MessageId")}
