"""Delivery-side logic for the decoupled `deploy/delivery/` boundary (PRD
docs/prd/agent-system-redesign.md FR-1/FR-2/FR-2a/FR-3, ADR-0014 Decision 2a).

This module is a **lift and refactor**, not a rewrite, of
`deploy/managed-agent/pipeline/audio_email.py` -- that file is the LIVE production
pipeline script (module-level Polly synthesis + env-var reads, gated into "send
mode" only inconsistently) and is explicitly OUT OF SCOPE for this phase (untouched,
still what the self-hosted microVM image uses today). This module extracts its
reusable logic into clean, importable, testable functions for the new async
`POST /deliver` + `GET /deliver/{deliveryId}` boundary:

  - `derive_html()` -- NEW (FR-2a): deterministic, no-LLM Markdown -> HTML body
    conversion, replacing the content-generation agent's ad hoc
    `markdown.markdown(...)` call (today driven by `deployment.json`'s
    `initial_prompt` step 2: "convert that brief Markdown to clean, inbox-readable
    HTML"). See its own docstring below for the byte-for-byte regression evidence.
  - `synthesize_audio()` -- refactored from the top-level Polly code at
    `audio_email.py:168-198` into a callable returning `(audio_ok, audio_s3_key,
    mp3_bytes)` instead of three module-level globals.
  - `send_all()` -- ported verbatim (same signature/logic) from
    `audio_email.py:383-481` -- already a clean function there.
  - `_html_with_header()` / `_html_with_unsubscribe_footer()` -- verbatim, unchanged
    copies of `audio_email.py:309` / `audio_email.py:344`. These are ALREADY
    delivery-side chrome conceptually (ADR-0014 Decision 2a) and must not change.
  - `_query_confirmed_subscribers()` / `_unsubscribe_link()` -- ported verbatim from
    `audio_email.py:213-251`.
  - `_feedback_link()` / `_get_feedback_signing_secret()` -- ported verbatim from
    `audio_email.py:269-306`.
  - `_build_confirmation_email()` / `send_confirmation_email()` -- ported verbatim
    from `audio_email.py:484-536`.

Two CRITICAL differences from `audio_email.py`, both required by this module being a
plain importable Lambda module rather than a `python3 audio_email.py` script:

1. **No module-level side effects.** `audio_email.py` does real I/O (env var reads
   that raise `KeyError` if unset, Polly synthesis) at import time. Every one of
   those steps is wrapped in a function here, so importing this module never talks
   to AWS or requires any env var to be set -- the Lambda handler (`handler.py`)
   calls these functions explicitly, at the point in the delivery flow where each
   step's inputs are actually ready.
2. **No CLI mode.** `audio_email.py`'s `read-recent-briefs` CLI branch and its
   `__main__` send-mode block are managed-agent/microVM-specific entrypoints (the
   research skill invokes the former as a subprocess) -- this module has neither;
   `brief_history.py` (a sibling, hand-duplicated copy, see below) is called
   directly by `handler.py` instead.

`brief_history.py` (for `archive_todays_brief()` / `archive_candidates_file()`) is
hand-duplicated alongside this module (not imported cross-deploy-unit) -- this repo's
established convention for exactly this situation: `feedback_token.py`'s docstring
documents the same hand-duplication across THREE independent deploy units already
(`deploy/managed-agent/pipeline/`, `deploy/subscribers/layers/common/python/`,
`deploy/feedback/functions/submit/`), and `deploy/eval/functions/common/review_auth.py`
cites that same precedent for its own duplication. `deploy/managed-agent/` and
`deploy/delivery/` are independent Lambda deployment units with no shared package,
so this file is copied, not imported, and any future change to the *shared* logic
(`send_all()`, the header/footer helpers, the subscriber query, the feedback link)
must be applied to both copies by hand -- exactly the same discipline this repo
already follows for `feedback_token.py` and `review_auth.py`.
"""

from __future__ import annotations

import time
import urllib.parse
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown

import feedback_token  # noqa: E402 - hand-duplicated, see module docstring

REGION = "us-east-1"
BUCKET = "cowork-polly-tts-740353583786"
# Both sends go from aibriefing@ (owner's inbox address, RECIP, is unchanged) --
# verbatim from audio_email.py.
SENDER = "aibriefing@mschweier.com"
RECIP = "mail@mschweier.com"
SUBSCRIBER_SENDER = "aibriefing@mschweier.com"
SUBSCRIBE_SITE_URL = "https://briefing.mschweier.com"

# MAX_AUDIO_ATTACHMENT_BYTES mirrors audio_email.py's constant of the same
# name/value -- an oversized MP3 is dropped (never sent unattached-of-brief) rather
# than risking an SES raw-message-size rejection that would otherwise cost the
# recipient the written brief too.
MAX_AUDIO_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15 MB

# How long to wait for Polly's async synthesis task before giving up -- verbatim
# from audio_email.py:171 (`deadline = time.time() + 300`).
POLLY_SYNTHESIS_TIMEOUT_SECONDS = 300
POLLY_POLL_INTERVAL_SECONDS = 5


# ---------------------------------------------------------------------------
# NEW (FR-2a): deterministic Markdown -> HTML derivation, no LLM.
# ---------------------------------------------------------------------------


def derive_html(brief_markdown: str) -> str:
    """Convert brief Markdown to the inbox-readable HTML body, deterministically,
    with NO LLM/agent involvement (PRD FR-2a, ADR-0014 Decision 2a).

    This replaces the content-generation agent's today's ad hoc conversion step
    (`deployment.json`'s `initial_prompt` step 2: "convert that brief Markdown to
    clean, inbox-readable HTML and save to /workspace/brief.html"). The output of
    THIS function is the same artifact `audio_email.py:159` currently reads from
    `BRIEF_HTML_PATH` as `brief_html` -- i.e. the RAW, pre-header/footer-wrap
    conversion, not the per-recipient `owner_html`/`subscriber_html` `send_all()`
    computes later by wrapping with `_html_with_header()`/
    `_html_with_unsubscribe_footer()` (those two functions stay unchanged, called
    separately, per recipient, inside `send_all()` below).

    Extensions used: NONE. `markdown.markdown(brief_markdown)` with the library's
    default extension set (no `tables`, `fenced_code`, `nl2br`, etc.) was confirmed,
    by direct byte-for-byte diff against a REAL archived production `brief.html`
    (2026-07-06's live scheduled run -- see
    `deploy/delivery/tests/fixtures/2026-07-06-brief.md` /
    `2026-07-06-brief.html`, and `test_derive_html_regression.py`), to reproduce the
    exact HTML body the content-generation agent's own ad hoc conversion produced
    that day -- an EXACT match once the archived file's surrounding delivery-side
    chrome (the outer `<html>`/`<head>`/table/`<style>` wrapper CSS, and the
    "You're receiving this..." footer div -- both already delivery-owned, unrelated
    to this function) is excluded. The real brief that day used only the Markdown
    features `markdown`'s core parser already handles without any extension: `#`/`##`/
    `###` headings, `**bold**`, `_italic_`/`*em*`, `[text](url)` links, `---`
    horizontal rules, and plain paragraphs -- no pipe tables, no fenced code blocks,
    and no bare-URL/single-newline-as-`<br>` behavior that would require `nl2br`. So
    no extensions are added here; if a FUTURE brief legitimately needs one (e.g. the
    skill starts emitting a Markdown table), this function and its regression test
    fixture must be revisited together, not silently patched by adding an extension
    with no fixture proving it still matches the standardized design.
    """
    return markdown.markdown(brief_markdown)


# ---------------------------------------------------------------------------
# Polly synthesis -- refactored from audio_email.py's top-level try/except into a
# callable. Same fail-safe (CLAUDE.md: "never lose the brief over an audio/email
# glitch") -- a synthesis failure returns audio_ok=False, never raises.
# ---------------------------------------------------------------------------


def synthesize_audio(polly_client, s3_client, script: str, mp3_out_path: str) -> tuple[bool, str | None, bytes | None]:
    """Synthesize `script` to MP3 via Polly's ASYNC task API (`OutputUri`, never a
    hand-built S3 key -- CLAUDE.md), download it to `mp3_out_path`, and read the
    bytes back. Returns `(audio_ok, audio_s3_key, mp3_bytes)`:

      - `audio_ok`: False on ANY failure (start, poll, timeout, download) -- verbatim
        fail-safe from `audio_email.py:168-181` (`except Exception: audio_ok = False`).
      - `audio_s3_key`: the run's ACTUAL `OutputUri`-derived key, or `None` if
        synthesis failed or the resulting MP3 exceeds `MAX_AUDIO_ATTACHMENT_BYTES`
        (mirrors `audio_email.py:196-198`'s "drop the attachment, don't risk an SES
        raw-message-size rejection" behavior -- but the caller still gets a non-None
        `audio_s3_key` in that oversized case, matching production: only the EMAIL
        attachment is dropped, the S3 object itself still exists and is still a
        valid archive pointer).
      - `mp3_bytes`: the downloaded bytes, or `None` if synthesis failed OR the file
        was too large to attach.

    Never raises -- verbatim fail-safe semantics from audio_email.py.
    """
    audio_ok = True
    audio_s3_key: str | None = None
    try:
        task = polly_client.start_speech_synthesis_task(
            Text=script,
            OutputFormat="mp3",
            VoiceId="Matthew",
            Engine="neural",
            OutputS3BucketName=BUCKET,
            OutputS3KeyPrefix="audio/",
        )
        task_id = task["SynthesisTask"]["TaskId"]
        deadline = time.time() + POLLY_SYNTHESIS_TIMEOUT_SECONDS
        while True:
            synthesis_task = polly_client.get_speech_synthesis_task(TaskId=task_id)["SynthesisTask"]
            status = synthesis_task["TaskStatus"]
            if status == "completed":
                break
            if status == "failed":
                raise RuntimeError(synthesis_task.get("TaskStatusReason", "polly failed"))
            if time.time() > deadline:
                raise TimeoutError("polly timed out")
            time.sleep(POLLY_POLL_INTERVAL_SECONDS)
        # Use OutputUri, never build the S3 key (CLAUDE.md invariant).
        audio_s3_key = urllib.parse.urlparse(synthesis_task["OutputUri"]).path.split(f"{BUCKET}/", 1)[1]
        s3_client.download_file(BUCKET, audio_s3_key, mp3_out_path)
    except Exception as e:  # noqa: BLE001 - fail-safe: never lose the brief over an audio glitch
        print("AUDIO_STEP_FAILED:", repr(e))
        return False, None, None

    mp3_bytes: bytes | None
    with open(mp3_out_path, "rb") as f:
        mp3_bytes = f.read()
    if len(mp3_bytes) > MAX_AUDIO_ATTACHMENT_BYTES:
        print("AUDIO_TOO_LARGE_SKIPPING_ATTACHMENT:", len(mp3_bytes))
        mp3_bytes = None

    return audio_ok, audio_s3_key, mp3_bytes


# ---------------------------------------------------------------------------
# MIME message building -- verbatim from audio_email.py:201-210.
# ---------------------------------------------------------------------------


def _build_message(sender, recipient, subject, html_body, mp3_bytes, mp3_filename):
    """Build the MIME message shared by the owner send and every subscriber send."""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)
    if mp3_bytes is not None:
        p = MIMEApplication(mp3_bytes, _subtype="mpeg")
        p.add_header("Content-Disposition", "attachment", filename=mp3_filename)
        msg.attach(p)
    return msg


# ---------------------------------------------------------------------------
# Subscriber query + unsubscribe link -- verbatim from audio_email.py:213-251.
# ---------------------------------------------------------------------------


def _query_confirmed_subscribers(dynamodb_client, table_name):
    """Query-only (never Scan) the status-index GSI for confirmed subscribers.

    Scoped IAM: dynamodb:Query on the status-index GSI ARN only (docs/adr/0002 §B).
    Returns a (subscribers, query_failed) tuple: `subscribers` is a list of dicts
    with email/firstName/unsubscribeToken (empty on either a genuine
    zero-subscriber day or a query failure); `query_failed` is True only when the
    query itself raised, so callers (send_all(), and in turn the confirmation
    email) can distinguish "0 because empty" from "0 because the lookup broke". A
    query failure never blocks the owner's send -- the empty list on failure
    preserves that behavior unchanged.
    """
    subscribers = []
    query_failed = False
    try:
        paginator = dynamodb_client.get_paginator("query")
        for page in paginator.paginate(
            TableName=table_name,
            IndexName="status-index",
            KeyConditionExpression="#status = :confirmed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":confirmed": {"S": "confirmed"}},
        ):
            for item in page.get("Items", []):
                subscribers.append(
                    {
                        "email": item.get("email", {}).get("S", ""),
                        "firstName": item.get("firstName", {}).get("S", ""),
                        "unsubscribeToken": item.get("unsubscribeToken", {}).get("S", ""),
                    }
                )
    except Exception as e:
        print("SUBSCRIBERS_QUERY_FAILED:", repr(e))
        query_failed = True
        subscribers = []
    return subscribers, query_failed


def _unsubscribe_link(email, token, subscribers_api_base_url):
    base = subscribers_api_base_url.rstrip("/")
    return f"{base}/unsubscribe?email={urllib.parse.quote(email)}&token={urllib.parse.quote(token)}"


# ---------------------------------------------------------------------------
# Feedback link (docs/prd/reader-feedback.md FR-5, ADR-0011/ADR-0012 §B) --
# verbatim from audio_email.py:265-306. Fail-safe: never blocks the send.
# ---------------------------------------------------------------------------

_feedback_secret_cache: str | None = None
_feedback_secret_fetch_attempted = False


def _get_feedback_signing_secret(secretsmanager_client, feedback_token_secret_arn: str):
    """Best-effort, cached-once fetch of the feedback-token signing secret. Returns
    None (never raises) when `feedback_token_secret_arn` is unset or the fetch fails."""
    global _feedback_secret_cache, _feedback_secret_fetch_attempted
    if _feedback_secret_fetch_attempted:
        return _feedback_secret_cache
    _feedback_secret_fetch_attempted = True
    if not feedback_token_secret_arn:
        return None
    try:
        response = secretsmanager_client.get_secret_value(SecretId=feedback_token_secret_arn)
        _feedback_secret_cache = response["SecretString"]
    except Exception as e:  # noqa: BLE001 - fail-safe: a secret-fetch glitch must never block the send
        print("FEEDBACK_LINK_SKIPPED: secret fetch failed:", repr(e))
        _feedback_secret_cache = None
    return _feedback_secret_cache


def _feedback_link(secretsmanager_client, email, brief_date, feedback_base_url, feedback_token_secret_arn):
    """Build a per-recipient feedback link, or return None if unavailable for any
    reason (missing config, secret-fetch failure, token-generation failure). Never
    raises -- the caller must be able to send the email regardless."""
    if not feedback_base_url:
        print("FEEDBACK_LINK_SKIPPED: FEEDBACK_BASE_URL not set")
        return None
    secret = _get_feedback_signing_secret(secretsmanager_client, feedback_token_secret_arn)
    if not secret:
        print("FEEDBACK_LINK_SKIPPED: signing secret unavailable")
        return None
    try:
        token = feedback_token.generate(secret, email, brief_date)
    except Exception as e:  # noqa: BLE001 - fail-safe: token generation must never block the send
        print("FEEDBACK_LINK_SKIPPED: token generation failed:", repr(e))
        return None
    base = feedback_base_url.rstrip("/")
    return f"{base}/?t={urllib.parse.quote(token)}"


# ---------------------------------------------------------------------------
# Header/footer chrome -- VERBATIM, UNCHANGED from audio_email.py:309-350.
# Already delivery-side conceptually (ADR-0014 Decision 2a) -- must not change.
# ---------------------------------------------------------------------------


def _html_with_header(html_body, feedback_link=None):
    """Prepend the top banner: feedback prompt (when available) + forward-friendly
    sign-up prompt + AI-curation disclaimer, all in one box.

    The feedback link is per-recipient (each person's own token), so this is called
    once per recipient rather than once shared across the whole send. Omitted
    gracefully when unavailable (fail-safe: never blocks or degrades the send).

    Added to every recipient's copy (owner included) since the owner is the most
    likely person to forward their own copy along to someone else.
    """
    feedback_line = (
        f'<p style="margin:0 0 6px 0;">💬 Have thoughts on today\'s brief? '
        f'<a href="{feedback_link}">Share feedback</a> — we process every submission.</p>'
        if feedback_link
        else ""
    )
    header = (
        '<div style="background:#f5f5f7;border-radius:8px;padding:12px 16px;'
        'margin-bottom:20px;font-size:12px;color:#555;line-height:1.5;">'
        f"{feedback_line}"
        '<p style="margin:0 0 6px 0;">📬 Received this as a forward? Anyone can get '
        f'their own daily copy — <a href="{SUBSCRIBE_SITE_URL}">subscribe here</a>.</p>'
        '<p style="margin:0;">This brief is curated and written by an AI agent, '
        "which may make mistakes. For anything important, please verify with "
        "original sources and do your own research.</p>"
        "</div>"
    )
    return header + html_body


def _html_with_unsubscribe_footer(html_body, unsubscribe_link):
    footer = (
        '<hr><p style="font-size:12px;color:#666;">'
        f'You are receiving this because you subscribed to the daily AI brief. '
        f'<a href="{unsubscribe_link}">Unsubscribe</a> at any time.</p>'
    )
    return html_body + footer


# ---------------------------------------------------------------------------
# send_all() -- ported verbatim (same signature/logic) from audio_email.py:383-481,
# with the module-scope PIPELINE_TIMEZONE / SUBSCRIBERS_API_BASE_URL /
# FEEDBACK_BASE_URL / FEEDBACK_TOKEN_SECRET_ARN reads replaced by explicit
# parameters (this module has no module-level env reads at all -- see docstring).
# ---------------------------------------------------------------------------


def send_all(
    ses_client,
    dynamodb_client,
    secretsmanager_client,
    subject,
    brief_html,
    mp3_bytes,
    mp3_filename,
    table_name,
    brief_date,
    *,
    subscribers_api_base_url: str = "",
    feedback_base_url: str = "",
    feedback_token_secret_arn: str = "",
    skip_subscriber_fanout=False,
):
    """Send the owner's copy, then fan out to every confirmed subscriber.

    Isolated as its own function (rather than inline top-level script code) so the
    failure-isolation loop logic is unit-testable without invoking Polly/S3. Returns
    (sent_count, failed_count, subscriber_sent_count, subscriber_failed_count,
    subscriber_query_failed) and prints the same SES_SENT / SES_SEND_FAILED /
    SES_SENT_SUMMARY log lines production has always relied on for operational
    visibility. `sent_count`/`failed_count` include the owner's own send, first;
    `subscriber_sent_count`/`subscriber_failed_count` are the subscriber-only
    breakdown, and `subscriber_query_failed` is True only when the DynamoDB
    subscriber query itself raised, never for a genuine zero-subscriber day. When
    `skip_subscriber_fanout` is True, the subscriber-only fields are all
    0/0/False -- no fan-out was attempted.

    `brief_date` is passed in explicitly (the caller resolves "today" in the
    pipeline's own timezone) rather than this function computing it itself, since
    this module has no module-level `PIPELINE_TIMEZONE` env read.
    """
    sent_count = 0
    failed_count = 0
    subscriber_sent_count = 0
    subscriber_failed_count = 0
    subscriber_query_failed = False

    # 1) Owner's copy — sent from aibriefing@mschweier.com to mail@mschweier.com
    # (recipient unchanged), always attempted first and never gated on subscriber
    # sends succeeding.
    owner_feedback_link = _feedback_link(secretsmanager_client, RECIP, brief_date, feedback_base_url, feedback_token_secret_arn)
    owner_html = _html_with_header(brief_html, owner_feedback_link)
    owner_msg = _build_message(SENDER, RECIP, subject, owner_html, mp3_bytes, mp3_filename)
    try:
        r = ses_client.send_raw_email(Source=SENDER, Destinations=[RECIP], RawMessage={"Data": owner_msg.as_string()})
        print("SES_SENT", r["MessageId"], "audio_attached=", mp3_bytes is not None)
        sent_count += 1
    except Exception as e:
        print("SES_SEND_FAILED:", RECIP, repr(e))
        failed_count += 1

    # 2) Subscriber fan-out — from aibriefing@mschweier.com, one send per confirmed
    # subscriber, each failure isolated so one bad address never blocks anyone else.
    if skip_subscriber_fanout:
        print("SUBSCRIBER_FANOUT_SKIPPED (manual validation run)")
    else:
        subscribers, subscriber_query_failed = _query_confirmed_subscribers(dynamodb_client, table_name)
        for subscriber in subscribers:
            email = subscriber["email"]
            if not email:
                continue
            try:
                unsubscribe_link = _unsubscribe_link(email, subscriber.get("unsubscribeToken", ""), subscribers_api_base_url)
                subscriber_feedback_link = _feedback_link(
                    secretsmanager_client, email, brief_date, feedback_base_url, feedback_token_secret_arn
                )
                subscriber_html = _html_with_header(brief_html, subscriber_feedback_link)
                subscriber_html = _html_with_unsubscribe_footer(subscriber_html, unsubscribe_link)
                subscriber_msg = _build_message(SUBSCRIBER_SENDER, email, subject, subscriber_html, mp3_bytes, mp3_filename)
                r = ses_client.send_raw_email(
                    Source=SUBSCRIBER_SENDER, Destinations=[email], RawMessage={"Data": subscriber_msg.as_string()}
                )
                print("SES_SENT", r["MessageId"], "recipient=", email, "audio_attached=", mp3_bytes is not None)
                sent_count += 1
                subscriber_sent_count += 1
            except Exception as e:
                print("SES_SEND_FAILED:", email, repr(e))
                failed_count += 1
                subscriber_failed_count += 1

    print(f"SES_SENT_SUMMARY sent={sent_count} failed={failed_count}")
    return sent_count, failed_count, subscriber_sent_count, subscriber_failed_count, subscriber_query_failed


# ---------------------------------------------------------------------------
# Post-send owner confirmation email -- ported verbatim (same signature/logic) from
# audio_email.py:484-536.
# ---------------------------------------------------------------------------


def _build_confirmation_email(
    run_date,
    subscriber_sent_count,
    subscriber_failed_count,
    *,
    skipped,
    subscriber_query_failed,
):
    """Build the short post-send owner confirmation. Pure string-building, no I/O,
    so it's unit-testable on its own. Returns (subject, body) — short, plain text,
    no full brief content."""
    subject = f"AI Brief sent — {run_date}"
    lines = [f"Today's AI brief ({run_date}) was sent."]

    if skipped:
        lines.append("Fan-out skipped for this validation run — no subscribers were mailed.")
    elif subscriber_query_failed:
        lines.append("0 subscribers (subscriber lookup failed — please check).")
    else:
        lines.append(f"Sent to {subscriber_sent_count} subscriber{'s' if subscriber_sent_count != 1 else ''}.")
        if subscriber_failed_count > 0:
            lines.append(f"{subscriber_failed_count} subscriber send{'s' if subscriber_failed_count != 1 else ''} failed.")

    body = "\n".join(lines)
    return subject, body


def send_confirmation_email(
    ses_client,
    run_date,
    subscriber_sent_count,
    subscriber_failed_count,
    *,
    skipped,
    subscriber_query_failed,
):
    """Send the short post-send owner confirmation.

    Best-effort only: any exception (building the message or the SES call itself)
    is caught and logged here, never raised — the caller must be able to always
    proceed to the archival step regardless of this function's outcome.
    """
    try:
        subject, body = _build_confirmation_email(
            run_date,
            subscriber_sent_count,
            subscriber_failed_count,
            skipped=skipped,
            subscriber_query_failed=subscriber_query_failed,
        )
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = SENDER
        msg["To"] = RECIP
        msg.attach(MIMEText(body, "plain", "utf-8"))
        r = ses_client.send_raw_email(Source=SENDER, Destinations=[RECIP], RawMessage={"Data": msg.as_string()})
        print("CONFIRMATION_SENT", r["MessageId"])
    except Exception as e:
        # Never raised: the brief and fan-out have already completed by the time
        # this runs, so a confirmation glitch must never fail the pipeline.
        print("CONFIRMATION_SEND_FAILED:", repr(e))


__all__ = [
    "REGION",
    "BUCKET",
    "SENDER",
    "RECIP",
    "SUBSCRIBER_SENDER",
    "SUBSCRIBE_SITE_URL",
    "MAX_AUDIO_ATTACHMENT_BYTES",
    "derive_html",
    "synthesize_audio",
    "send_all",
    "send_confirmation_email",
    "_build_confirmation_email",
    "_html_with_header",
    "_html_with_unsubscribe_footer",
    "_query_confirmed_subscribers",
    "_unsubscribe_link",
    "_feedback_link",
    "_get_feedback_signing_secret",
]
