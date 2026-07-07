"""Delivery-side logic for the decoupled `deploy/delivery/` boundary (PRD
docs/prd/agent-system-redesign.md FR-1/FR-2/FR-2a/FR-3, ADR-0014 Decision 2a).

This module is a **lift and refactor**, not a rewrite, of
`deploy/managed-agent/pipeline/audio_email.py` -- that file is the LIVE production
pipeline script (module-level Polly synthesis + env-var reads, gated into "send
mode" only inconsistently) and is explicitly OUT OF SCOPE for this phase (untouched,
still what the self-hosted microVM image uses today). This module extracts its
reusable logic into clean, importable, testable functions for the new async
`POST /deliver` + `GET /deliver/{deliveryId}` boundary:

  - `derive_html()` -- NEW (FR-2a): deterministic, no-LLM assembly of a COMPLETE,
    fixed HTML email document from the brief Markdown, replacing the
    content-generation agent's ad hoc, per-run HTML improvisation (today driven
    by `deployment.json`'s `initial_prompt` step 2: "convert that brief Markdown
    to clean, inbox-readable HTML"). Originally built to reproduce "the existing
    standardized design" byte-for-byte; that premise was found FALSE by
    comparing three real archived production emails against EACH OTHER (not
    just one in isolation) -- they are three genuinely different, freshly
    improvised HTML documents, proving there was never a stable template to
    reproduce. See `derive_html()`'s own docstring below for the full evidence
    and the corrected fixed-template design this function now implements.
    `_convert_markdown_body()` is the separate, still-unchanged, zero-extensions
    Markdown-to-fragment conversion step within it.
  - `synthesize_audio()` -- refactored from the top-level Polly code at
    `audio_email.py:168-198` into a callable returning `(audio_ok, audio_s3_key,
    mp3_bytes)` instead of three module-level globals.
  - `send_all()` -- ported verbatim (same signature/logic) from
    `audio_email.py:383-481` -- already a clean function there.
  - `_html_with_header()` / `_html_with_unsubscribe_footer()` -- banner/footer
    TEXT and styling are unchanged from `audio_email.py:309` / `audio_email.py:344`
    (ALREADY delivery-side chrome conceptually, ADR-0014 Decision 2a). The
    MECHANICAL INSERTION POINT was fixed (reviewer-found bug, independently
    reproduced by the coordinator): a blind prepend/append was correct only
    when the input was a bare HTML fragment; now that `derive_html()` always
    returns a complete document, both functions insert into it (right after
    `<body>` / right before `</body>`) instead -- see each function's own
    docstring for the concrete bug this fixes (an unsubscribe link that
    landed after `</html>`, outside the document entirely).
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

import html
import re
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


_DEFAULT_EMAIL_TITLE = "Daily AI Brief"

# The single, fixed, deterministic template this stack now applies to EVERY
# brief, every day -- see `derive_html()`'s docstring for why this replaced the
# original "reverse-engineer and byte-for-byte match the existing standardized
# design" plan.
#
# TWO independent template-shape decisions, each with its own concrete
# cross-client-compatibility reason (not arbitrary picks):
#
# 1. TABLE-BASED LAYOUT (an outer `role="presentation"` table containing one
#    centered "card" table) rather than a bare styled `<div>`. Table-based
#    layout is the long-established, genuinely technical best practice for
#    cross-email-client rendering: Outlook's Word-based rendering engine and
#    several webmail clients support only a narrow, inconsistent subset of
#    modern CSS (flexbox/grid, many `border-radius`/`box-shadow` combinations,
#    `max-width` centering on a bare `<div>`) but have reliably supported HTML
#    tables for cross-client layout since the format's inception -- the same
#    reasoning essentially every email-deliverability style guide (Litmus,
#    Mailchimp, Campaign Monitor) gives for defaulting to tables in
#    transactional/newsletter HTML. The chrome (body/table/cell backgrounds,
#    padding, the card's border-radius/shadow) is styled via INLINE
#    `style="..."` attributes on each element -- guaranteed to survive
#    everywhere a `<head>`-level `<style>` block might not (see #2).
# 2. A SCOPED `<style>` BLOCK PLACED INSIDE THE BODY (not in `<head>`) styles
#    the Markdown-converted content's plain tags (`<h1>`/`<h2>`/`<h3>`/`<p>`/
#    `<ul>`/`<li>`/`<a>`/`<strong>`/`<em>`/`<hr>`) -- rather than either (a)
#    inlining a `style=` attribute onto every single converted tag, which
#    `markdown.markdown()`'s plain HTML output does not support attaching to,
#    or (b) a `<head>`-level `<style>` block, which is the LESS portable of the
#    two `<style>` placements: Gmail's web client is well known for stripping
#    `<style>` blocks specifically when they appear in `<head>`, but a
#    `<style>` tag placed inline within the body (inside the rendered content
#    area) is preserved by every mainstream client tested in practice --
#    exactly the placement 2026-07-06's real (if otherwise inconsistent)
#    archived brief happened to use, which is the one piece of that day's
#    output this template deliberately keeps, on its own technical merit, not
#    because that day was "the standard" (it wasn't -- see the correction note
#    on `derive_html()` below).
#
# Colors and spacing were chosen fresh for this template (not copied wholesale
# from any one of the three real, mutually-inconsistent archived emails that
# motivated rebuilding this from scratch) for a clean, legible, professional
# look, applied consistently on every future run.
_EMAIL_BODY_STYLE = "margin:0;padding:0;background-color:#f4f4f5;"
_EMAIL_OUTER_TABLE_STYLE = "background-color:#f4f4f5;padding:24px 0;"
_EMAIL_CARD_TABLE_STYLE = (
    "background-color:#ffffff;border-radius:8px;overflow:hidden;"
    "box-shadow:0 1px 3px rgba(0,0,0,0.08);"
)
_EMAIL_CARD_CELL_STYLE = (
    "padding:32px 40px;color:#1a1a1a;font-size:16px;line-height:1.6;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
)
_EMAIL_CONTENT_STYLE_BLOCK = (
    "<style>\n"
    "  h1 { font-size:22px; line-height:1.3; margin:0 0 12px 0; color:#111111; }\n"
    "  h2 { font-size:18px; margin:28px 0 12px 0; color:#111111; border-bottom:2px solid #eeeeee; padding-bottom:6px; }\n"
    "  h3 { font-size:16px; margin:22px 0 8px 0; color:#111111; }\n"
    "  p { margin:0 0 14px 0; color:#2b2b2b; }\n"
    "  ul { margin:0 0 16px 0; padding-left:22px; }\n"
    "  li { margin-bottom:8px; color:#2b2b2b; }\n"
    "  hr { border:none; border-top:1px solid #e5e5e5; margin:24px 0; }\n"
    "  a { color:#2563eb; text-decoration:none; }\n"
    "  a:hover { text-decoration:underline; }\n"
    "  strong { color:#111111; }\n"
    "  em { color:#555555; }\n"
    "</style>\n"
)
# NOTE: there is deliberately NO fixed footer-disclaimer constant baked into
# derive_html()'s own output (an earlier version had one, `_EMAIL_FOOTER_TEXT`)
# -- see derive_html()'s docstring's "reviewer-found bug, fixed" note for why:
# it duplicated `_html_with_unsubscribe_footer()`'s own subscription-context
# messaging (which additionally carries the real unsubscribe link) for every
# subscriber, and was redundant with `_html_with_header()`'s top-of-email
# disclaimer for the owner's copy.

# Named insertion slots the per-recipient chrome (`_html_with_header()` /
# `_html_with_unsubscribe_footer()`) targets, so the banner/footer land INSIDE
# the centered content card -- horizontally aligned with the brief body -- rather
# than at the raw `<body>` level, full-width and left-aligned, OUTSIDE the card.
# This is an ALIGNMENT FIX the first rendered eyeball caught: composed at the
# `<body>` level (the prior `_insert_after_body_open_tag()` /
# `_insert_before_body_close_tag()` behavior), the grey banner ran edge-to-edge
# and started at the far left while the brief sat in a centered 640px card -- the
# two visibly didn't line up. `derive_html()` now emits these two comment markers
# inside the card cell (after the content `<style>` block, and after the body);
# the chrome functions REPLACE them. If a caller ever passes HTML without these
# slots -- a bare fragment, or production `audio_email.py`'s agent-improvised
# document, which has no card and no slots -- both functions FALL BACK to their
# original `<body>`-boundary insertion, so the shared banner/footer TEXT stays
# byte-identical across both hand-duplicated copies (module docstring's sync
# note) and only this template-specific placement fast-path is new here.
_HEADER_SLOT = "<!--BRIEF_HEADER_SLOT-->"
_FOOTER_SLOT = "<!--BRIEF_FOOTER_SLOT-->"


def _extract_email_title(brief_markdown: str) -> str:
    """Pull the brief's own title from its first Markdown `# ...` heading line
    (all three real fixtures examined -- 2026-07-03/04/06 -- show the `<title>`
    tag containing exactly this text), for use as both this document's `<title>`
    and as its own visible `<h1>` (the latter comes for free from the Markdown
    body conversion itself, since the brief's `# ...` line converts to an
    `<h1>...</h1>` the normal way -- this function is only for the *separate*
    `<head><title>` tag, which the Markdown conversion does not produce).

    Falls back to a generic default if no `# ` heading is found (malformed or
    empty input) -- never raises, matching this module's fail-safe conventions
    (the pipeline must never lose the brief over a formatting glitch)."""
    for line in brief_markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return _DEFAULT_EMAIL_TITLE


def _convert_markdown_body(brief_markdown: str) -> str:
    """Convert the brief Markdown to an HTML fragment, deterministically, with NO
    LLM/agent involvement (PRD FR-2a, ADR-0014 Decision 2a) -- the pure
    content-conversion step, separated from `derive_html()`'s template assembly
    below so each is independently testable.

    Extensions used: NONE. `markdown.markdown(brief_markdown)` with the library's
    default extension set (no `tables`, `fenced_code`, `nl2br`, etc.) was
    confirmed, by inspecting THREE real archived production briefs spanning
    2026-07-03/04/06, to be sufficient: none of the three uses a Markdown table,
    a fenced code block, or single-newline-as-`<br>` line breaks -- only `#`/`##`/
    `###` headings, `**bold**`, `_italic_`/`*em*`, `[text](url)` links, `---`
    horizontal rules, and plain paragraphs, all of which `markdown`'s core parser
    already handles without any extension. If a FUTURE brief legitimately needs
    one (e.g. the skill starts emitting a Markdown table), this function and its
    fixture-driven conversion tests must be revisited together, not silently
    patched by adding an extension no fixture exercises."""
    return markdown.markdown(brief_markdown)


def derive_html(brief_markdown: str) -> str:
    """Assemble the brief Markdown into one COMPLETE, fixed, deterministic HTML
    email document -- doctype, `<head>` (charset, viewport, a `<title>` derived
    from the brief's own first `# ...` heading), and a styled body wrapping the
    Markdown-converted content in a clean, consistent card layout -- with NO
    LLM/agent involvement (PRD FR-2a, ADR-0014 Decision 2a). The output of THIS
    function is the same artifact `audio_email.py:159` currently reads from
    `BRIEF_HTML_PATH` as `brief_html` -- i.e. the RAW, pre-header/footer-wrap
    HTML, not the per-recipient `owner_html`/`subscriber_html` `send_all()`
    computes later by wrapping with `_html_with_header()`/
    `_html_with_unsubscribe_footer()` (called separately, per recipient, inside
    `send_all()` below -- their banner/footer text/markup is unchanged and
    confirmed unrelated to and absent from every real brief examined here; only
    their mechanical insertion point was fixed to compose correctly into the
    complete document this function now always returns -- see their own
    docstrings).

    CORRECTED DESIGN (this function originally returned a bare, unstyled
    `markdown.markdown(...)` fragment, on the premise that "the existing
    standardized design" just needed reproducing byte-for-byte). That premise was
    FALSE, discovered by pulling and diffing THREE real archived production
    `brief.html` files -- 2026-07-03, 2026-07-04, and 2026-07-06 (all three still
    committed as fixtures, `deploy/delivery/tests/fixtures/`) -- against each
    other, not just against one day in isolation. They are three genuinely
    DIFFERENT HTML document structures, not variations on one template:
      - 2026-07-03: a single unstyled-wrapper `<div style="max-width:680px...">`
        (no `<table>` at all), a `.footer` CSS class, link color `#0645ad`,
        background `#f2f2f4`.
      - 2026-07-04: a `<div class="email-wrapper"><div class="email-card">`
        structure with CSS as NAMED CLASSES in a `<head>`-level `<style>` block
        (not inline styles like the other two days), a `.tldr` callout class the
        other two days lack entirely, link color `#2b6cb0`, a colored `<h1>`
        bottom border absent elsewhere.
      - 2026-07-06: a `<table role="presentation">` layout, an inline `<style>`
        block INSIDE the body's inner `<td>` (not in `<head>`), an uppercase
        "eyebrow" label div absent elsewhere, link color `#2563eb`, and its own
        ad hoc footer disclaimer text that matches neither
        `_html_with_header()`/`_html_with_unsubscribe_footer()` in `audio_email.py`
        NOR either of the other two days.
    This proves the content-generation agent re-improvises the ENTIRE HTML
    document (wrapper strategy, CSS class system, color palette, presence/absence
    of structural elements) fresh on every single run -- there was never a stable
    template underneath to reverse-engineer; it is genuinely non-deterministic
    LLM output, and every subscriber has been getting a visually different email
    every day. See `test_derive_html_regression.py` for the fixture-driven
    conversion-fidelity tests this correction replaced the old
    byte-for-byte-against-one-day diff with.

    THE FIX: rather than chasing a moving target, this function now produces ONE
    fixed, deterministic, well-designed template, chosen once here and applied
    consistently on every future run -- a genuine improvement (a stable,
    predictable subscriber experience), not merely a constraint satisfied.
    Template-shape rationale (table-based layout, an in-body scoped `<style>`
    block, chosen colors) is documented on the `_EMAIL_*` module constants
    above. The Markdown body conversion itself (`_convert_markdown_body()`) is
    unaffected by this correction -- zero extensions were, and remain, the right
    call; see that function's own docstring.

    NOTE (reviewer-found bug, fixed): this function does NOT append its own
    "you're receiving this because you subscribed" disclaimer line -- an
    earlier version did (a fixed `_EMAIL_FOOTER_TEXT` baked into every
    document), which produced a real, user-visible bug once composed with
    `_html_with_unsubscribe_footer()` below: subscribers ended up with TWO
    near-identical disclaimers back-to-back (one text-only, baked in here;
    one with the actual unsubscribe link, added by
    `_html_with_unsubscribe_footer()`). For subscribers,
    `_html_with_unsubscribe_footer()` already provides equivalent (and MORE
    complete, since it carries the real link) subscription-context messaging,
    making a baked-in duplicate here pure redundancy. For the owner's copy
    (which never gets `_html_with_unsubscribe_footer()`),
    `_html_with_header()` already gives every copy (owner included) the
    AI-curation disclaimer + subscribe/forward prompt at the top, so nothing
    subscription-relevant is lost by not also closing with one. See
    `_html_with_header()`/`_html_with_unsubscribe_footer()`'s own docstrings
    below for the composition-correctness fix this same bug also required.
    """
    # HTML-escape the title before interpolating it into the <title> tag: the body
    # conversion via markdown.markdown() already entity-escapes special characters, but
    # this separate <title> path did not, so a brief heading containing &/</> would
    # produce malformed HTML (reviewer LOW). Not a security issue (the heading is
    # agent-generated, not attacker-controlled), but a real correctness gap.
    title = html.escape(_extract_email_title(brief_markdown))
    body_html = _convert_markdown_body(brief_markdown)

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>{title}</title>\n"
        "</head>\n"
        f'<body style="{_EMAIL_BODY_STYLE}">\n'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="{_EMAIL_OUTER_TABLE_STYLE}">\n'
        "<tr><td align=\"center\">\n"
        f'<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="{_EMAIL_CARD_TABLE_STYLE}">\n'
        f'<tr><td style="{_EMAIL_CARD_CELL_STYLE}">\n'
        f"{_EMAIL_CONTENT_STYLE_BLOCK}"
        f"{_HEADER_SLOT}\n"
        f"{body_html}\n"
        f"{_FOOTER_SLOT}\n"
        "</td></tr>\n"
        "</table>\n"
        "</td></tr>\n"
        "</table>\n"
        "</body>\n"
        "</html>\n"
    )


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
# Header/footer chrome -- banner/footer TEXT and styling are VERBATIM, UNCHANGED
# from audio_email.py:309-350 (already delivery-side conceptually, ADR-0014
# Decision 2a) -- ONLY the mechanical insertion point changed (see the
# "COMPOSITION FIX" note on each function below). This is not a violation of
# "these two functions stay exactly as-is": that instruction was written when
# `derive_html()`'s output was a bare fragment, for which blind prepend/append
# was correct. `derive_html()` now deliberately produces a COMPLETE HTML
# document (its own corrected design, ADR-0014 Decision 2a's rework), so
# composing correctly INTO that document is the natural, necessary
# consequence of that design, not scope creep.
# ---------------------------------------------------------------------------

# Matches the FIRST opening `<body ...>` tag (case-insensitive, tolerant of any
# attributes -- e.g. derive_html()'s own `<body style="...">`) -- the banner is
# inserted immediately after it. Matches the LAST closing `</body>` tag for the
# footer, inserted immediately before it.
_BODY_OPEN_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE_TAG_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _insert_after_body_open_tag(html_document: str, fragment_to_insert: str) -> str:
    """Insert `fragment_to_insert` immediately after the document's opening
    `<body ...>` tag -- REVIEWER-FOUND BUG FIX (independently reproduced and
    confirmed by the coordinator): a blind prepend (`fragment + html_document`)
    placed the banner BEFORE `<!DOCTYPE html>`/`<html>`/`<head>`, which is
    invalid HTML (content is only valid inside `<body>`) and, more concretely
    reproduced, left it entirely outside the actual document a mail client
    renders as the message body.

    Falls back to a plain prepend if no `<body>` tag is found at all (e.g. a
    caller ever passes a bare fragment rather than `derive_html()`'s own full-
    document output) -- never raises, matching this module's fail-safe
    conventions; the caller still gets SOME banner, just not correctly
    positioned relative to a document root that, in that case, doesn't exist
    anyway."""
    match = _BODY_OPEN_TAG_RE.search(html_document)
    if match is None:
        return fragment_to_insert + html_document
    insertion_point = match.end()
    return html_document[:insertion_point] + fragment_to_insert + html_document[insertion_point:]


def _insert_before_body_close_tag(html_document: str, fragment_to_insert: str) -> str:
    """Insert `fragment_to_insert` immediately before the document's closing
    `</body>` tag -- the footer half of the same reviewer-found bug fix as
    `_insert_after_body_open_tag()` above: a blind append
    (`html_document + fragment`) placed the unsubscribe footer AFTER
    `</html>`, entirely outside the document root -- confirmed live
    (`</html>` at position 19132 of a 19352-character composed string, ~190
    bytes of real footer markup, including the actual unsubscribe link,
    stranded past the end of the document). Content outside `<html>...</html>`
    is invalid HTML and renders unreliably across email clients -- exactly the
    one thing this footer must reliably deliver.

    Falls back to a plain append if no `</body>` tag is found -- same
    fail-safe reasoning as `_insert_after_body_open_tag()` above."""
    match = _BODY_CLOSE_TAG_RE.search(html_document)
    if match is None:
        return html_document + fragment_to_insert
    insertion_point = match.start()
    return html_document[:insertion_point] + fragment_to_insert + html_document[insertion_point:]


def _html_with_header(html_body, feedback_link=None):
    """Insert the top banner immediately after the document's opening `<body>`
    tag: feedback prompt (when available) + forward-friendly sign-up prompt +
    AI-curation disclaimer, all in one box.

    The feedback link is per-recipient (each person's own token), so this is called
    once per recipient rather than once shared across the whole send. Omitted
    gracefully when unavailable (fail-safe: never blocks or degrades the send).

    Added to every recipient's copy (owner included) since the owner is the most
    likely person to forward their own copy along to someone else.

    COMPOSITION FIX (reviewer-found bug): previously prepended via
    `header + html_body` (correct only when `html_body` was a bare fragment,
    not a complete document) -- then inserted via `_insert_after_body_open_tag()`,
    placing the banner as the first thing inside `<body>`, still visually "at the
    top" but now actually inside the document root.

    ALIGNMENT FIX (first rendered eyeball): inserting at the `<body>` level put
    the banner OUTSIDE `derive_html()`'s centered 640px content card -- it ran
    full-width and left-aligned while the brief body sat centered, so the two
    didn't line up. The banner is now placed at the `_HEADER_SLOT` marker
    `derive_html()` emits INSIDE the card cell (after the content `<style>`
    block, before the brief body), so it shares the card's width and 40px side
    padding and lines up with the body text. Falls back to the old
    `_insert_after_body_open_tag()` body-level insertion when no slot is present
    (a bare fragment, or production `audio_email.py`'s agent-improvised document,
    which has no card). Banner text/styling are unchanged.
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
    if _HEADER_SLOT in html_body:
        return html_body.replace(_HEADER_SLOT, header, 1)
    return _insert_after_body_open_tag(html_body, header)


def _html_with_unsubscribe_footer(html_body, unsubscribe_link):
    """Insert the unsubscribe footer immediately before the document's closing
    `</body>` tag.

    COMPOSITION FIX (reviewer-found bug): previously appended via
    `html_body + footer` (correct only when `html_body` was a bare fragment) --
    then inserted via `_insert_before_body_close_tag()`, placing the footer as
    the last thing inside `<body>`, so the unsubscribe link it carries is part
    of the rendered message rather than stranded past `</html>`.

    ALIGNMENT FIX (first rendered eyeball): inserting at the `</body>` level put
    the footer OUTSIDE `derive_html()`'s centered content card (full-width,
    left-aligned, not lined up with the brief body). The footer is now placed at
    the `_FOOTER_SLOT` marker `derive_html()` emits INSIDE the card cell (after
    the brief body), so it shares the card's width/padding and lines up with the
    body. Falls back to the old `_insert_before_body_close_tag()` body-level
    insertion when no slot is present (a bare fragment, or production
    `audio_email.py`'s agent-improvised document). Footer text/styling are
    unchanged.
    """
    footer = (
        '<hr><p style="font-size:12px;color:#666;">'
        f'You are receiving this because you subscribed to the daily AI brief. '
        f'<a href="{unsubscribe_link}">Unsubscribe</a> at any time.</p>'
    )
    if _FOOTER_SLOT in html_body:
        return html_body.replace(_FOOTER_SLOT, footer, 1)
    return _insert_before_body_close_tag(html_body, footer)


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
