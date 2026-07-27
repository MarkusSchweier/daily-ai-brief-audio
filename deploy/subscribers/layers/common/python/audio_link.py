"""Shared audio-link rendering logic for the audio-as-link delivery feature (PRD
docs/prd/audio-link-delivery.md, ADR-0017 D10). Owns three things that must be
identical, byte-for-byte, everywhere the daily brief or the welcome email mentions
audio:

  - the public audio URL builder (`build_audio_url`) -- FR-3/FR-9/AC-6: the URL is
    built from the audio base URL plus the object's own `OutputUri`-derived S3 key,
    VERBATIM, and nothing else (never the brief date, subject, or any other
    guessable input);
  - the `x-amz-expiration` header parser + Europe/Berlin date formatter
    (`parse_expiration_header` / `format_expiry_date`) -- ADR-0017 D1;
  - the three ratified, byte-exact meta-box HTML lines (`build_audio_link_result` /
    `NO_AUDIO_LINE_HTML`) -- ADR-0017 D10/D11;
  - the log-safety key redactor (`redact_audio_key`) -- ADR-0017 D9 step 4: a live
    `audio/*` key is a working PUBLIC URL, so neither unit may log one in full.

Each caller (`delivery_core.py`'s `_html_with_header()`, the welcome-send handler's
`_meta_box_html()`) owns only WHERE this line goes in its own meta box -- never its
own copy of the wording, URL, or date format.

Duplicated verbatim in:
  - deploy/delivery/functions/deliver/audio_link.py            (this copy -- imported by delivery_core.py)
  - deploy/subscribers/layers/common/python/audio_link.py      (imported by welcome-send/handler.py)

`deploy/delivery/` and `deploy/subscribers/` are independent Lambda deployment units
with no shared package -- this repo's established convention for exactly this
situation (see `feedback_token.py`'s docstring, hand-duplicated across four
independent deploy units, and `brief_history.py`'s docstring citing the same
precedent). Any change here MUST be applied to the other copy too; a test asserts
the two files are byte-identical (ADR-0017 D10, "R4 becomes a failing test, not a
checklist item").

This module does NO I/O (no S3 calls) -- both callers already hold whatever
`HeadObject` response (or failure) they need before calling into this module; that
keeps this module pure and trivially unit-testable, and keeps the fail-safe
philosophy (CLAUDE.md: "never lose the brief over an audio/email glitch") entirely
in the hands of the two callers' own try/except around their own S3 calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

# The pipeline's own timezone (matches audio_email.py / delivery handler.py's
# PIPELINE_TIMEZONE convention) -- the expiry date is always rendered in this
# timezone, regardless of which timezone the S3-reported expiry header is in
# (ADR-0017 D1/FR-8).
#
# NOTE (review clarification): this is a DIFFERENT knob than
# `handler.py`'s own module-level `PIPELINE_TIMEZONE` (env-var-driven, default
# `"Europe/Berlin"`, used only for `_today_local_date()`'s `brief_date`
# calculation). They share a name and today's default value by convention, not
# by any code link -- this one is a hardcoded literal (ADR-0017 D6's
# "hand-duplicated module constant, not CDK context/an env var" choice, applied
# here too since the expiry date must always render in the SAME timezone
# regardless of any per-deploy override of the run's own calendar-date
# timezone). Changing `handler.py`'s env var does NOT change this one, and vice
# versa; if the two ever need to diverge, that is a deliberate, separate change
# in each file, not a bug.
PIPELINE_TIMEZONE = "Europe/Berlin"

# Matches the `expiry-date="..."` portion of an S3 `Expiration` / `x-amz-expiration`
# header, e.g. `expiry-date="Sat, 01 Aug 2026 00:00:00 GMT", rule-id="expire-audio-7-days"`.
_EXPIRY_DATE_ATTR_RE = re.compile(r'expiry-date="([^"]*)"')

# The three ratified, byte-exact HTML lines (ADR-0017 D11) -- each is the FIRST line
# of the daily brief's meta box (welcome email: directly after the greeting, D11
# amendment). `{url}` / `{expiry_label}` are the only interpolated values.
_LINK_EXPIRY_TEMPLATE = (
    '<p style="margin:0 0 6px 0;">\U0001f3a7 <a href="{url}">Listen to this brief</a> '
    "(link expires {expiry_label})</p>"
)
NO_AUDIO_LINE_HTML = '<p style="margin:0 0 6px 0;">\U0001f3a7 No audio version available for this edition.</p>'
_LINK_GENERIC_TEMPLATE = (
    '<p style="margin:0 0 6px 0;">\U0001f3a7 <a href="{url}">Listen to this brief</a> '
    "(link available for about 7 days)</p>"
)


def build_audio_url(audio_base_url: str, audio_key: str) -> str:
    """Build the public audio URL from `audio_base_url` (e.g.
    `https://briefing.mschweier.com`) and the object's own `OutputUri`-derived S3
    key (e.g. `audio/.<uuid>.mp3`), passed through VERBATIM (FR-3/FR-9/AC-6) --
    never the brief date, subject, or any other guessable input; the function
    signature itself makes that structurally impossible.

    `urllib.parse.quote(audio_key, safe="/")` (ADR-0017 D9 step 5) ensures an
    unexpected character in the key can never yield a malformed URL while the `/`
    path separator is preserved, so `audio/.<uuid>.mp3` maps to `/audio/.<uuid>.mp3`
    on the URL path, matching the `/audio/*` behavior's path pattern exactly (no
    `originPath` on that behavior, ADR-0017 D3)."""
    base = audio_base_url.rstrip("/")
    return f"{base}/{quote(audio_key, safe='/')}"


def redact_audio_key(audio_key: str) -> str:
    """Redact an audio S3 key for logging. A live `audio/*` key is a working
    PUBLIC URL for as long as the object exists, and this repo's own key-hygiene
    rule (ADR-0017 D9 step 4) covers runtime logs as well as evidence docs and
    test fixtures -- so no log line, on any path including transient/error ones,
    may carry the full key. Keeps just enough shape (a short prefix, plus the
    total length) to correlate log lines, without reproducing a usable key.

    Lives HERE, in the shared hand-duplicated module, rather than privately in
    either deploy unit (security recommendation, L1): both the delivery Lambda's
    `delivery_core.resolve_audio_metadata()` and the welcome-send handler's own
    `_resolve_audio_metadata()` log key-bearing failure lines, so one redaction
    implementation covered by the byte-equality drift test beats two that can
    drift apart.

    Never raises -- this exists to make a log line safer, so it must not itself
    become a new failure mode."""
    try:
        prefix = audio_key[:12]
    except Exception:  # noqa: BLE001 - a logging helper must never raise
        return "<unloggable audio key>"
    return f"{prefix}...<redacted, len={len(audio_key)}>"


def parse_expiration_header(expiration_header: str | None) -> datetime | None:
    """Parse an S3 `HeadObject` response's `Expiration` field (the `x-amz-expiration`
    response header, shaped `expiry-date="Tue, 04 Aug 2026 00:00:00 GMT",
    rule-id="..."`) into an aware datetime, or `None` if the header is missing,
    empty, not a string (e.g. a hypothetical future botocore/S3 response-shape
    change -- REVIEW FINDING, fixed: `re.search()` raises `TypeError` on a
    non-string/non-bytes input, which used to reach the caller unguarded), or its
    `expiry-date` portion is not a valid HTTP-date (ADR-0017 D1). Never raises."""
    if not expiration_header:
        return None
    try:
        match = _EXPIRY_DATE_ATTR_RE.search(expiration_header)
        if not match:
            return None
        return parsedate_to_datetime(match.group(1))
    except (TypeError, ValueError, IndexError):
        return None


def format_expiry_date(expiry_dt: datetime) -> str:
    """Render `expiry_dt` (an aware datetime, any source timezone) in
    `PIPELINE_TIMEZONE` (Europe/Berlin) with an unambiguous, spelled-out month and
    no time-of-day -- e.g. `4 August 2026` (ADR-0017 D1/FR-8). Built by hand
    (`d.day` / `%B` / `d.year`), deliberately NOT `%-d`, which is a glibc extension
    that behaves differently across platforms (a developer's macOS vs. Lambda)."""
    local = expiry_dt.astimezone(ZoneInfo(PIPELINE_TIMEZONE))
    return f"{local.day} {local:%B} {local.year}"


@dataclass(frozen=True)
class AudioLinkResult:
    """The resolved meta-box line, plus the FR-17 structured-logging fields a
    caller needs to record whether a link was embedded and, when it was, the
    expiry date used."""

    html: str
    link_included: bool
    expiry_label: str | None = None


def build_audio_link_result(
    *,
    audio_base_url: str | None,
    audio_key: str | None,
    audio_available: bool,
    expiration_header: str | None = None,
) -> AudioLinkResult:
    """Resolve the audio meta-box line from inputs the caller already has in hand
    after attempting to resolve that edition's audio object -- implements
    ADR-0017 D1's three-outcome table plus FR-18's blank-config degrade:

      - `audio_base_url` unset/blank (FR-18), OR `audio_key` unset/None (e.g.
        Polly synthesis failed, AC-11), OR `audio_available=False` (the caller's
        own `HeadObject` returned 404/`NoSuchKey`/any other error) -> the no-audio
        line, no link, no date (`NO_AUDIO_LINE_HTML`).
      - `audio_available=True` with a parseable `expiration_header` -> the link,
        with the exact expiry date (AC-9).
      - `audio_available=True` but `expiration_header` is missing/unparseable ->
        the link, with the generic "about 7 days" note (AC-12).

    Never raises -- every input is treated as untrusted/best-effort, matching this
    feature's fail-safe requirement (FR-10). This function performs no I/O; the
    caller is responsible for the `HeadObject` call (and its own try/except) that
    produces `audio_available`/`expiration_header`.

    REVIEW FINDING (Medium), fixed: this function used to have no try/except of
    its own, so an unexpected input shape (e.g. a malformed `expiration_header`
    reaching `parse_expiration_header()`) could raise past both call sites
    (`delivery_core.py`'s `_run_delivery()`, the welcome-send handler's
    `_handle()`) despite their own comments already asserting "never raises" --
    and because this runs BEFORE `send_all()` in the delivery worker, that would
    have aborted the ENTIRE send, owner included, directly contradicting FR-10.
    The whole body is now wrapped in a top-level `try/except Exception` as a
    last-resort safety net (degrading to the no-audio line, matching every other
    best-effort helper in this diff) -- on top of, not instead of,
    `parse_expiration_header()`'s own now-hardened handling of a malformed
    `expiration_header`, which is the specific/expected case and degrades more
    precisely to the generic-note line (the object IS available; only its date is
    unreadable), exactly like the missing/unparseable-header case."""
    try:
        if not audio_base_url or not audio_key or not audio_available:
            return AudioLinkResult(html=NO_AUDIO_LINE_HTML, link_included=False, expiry_label=None)

        url = build_audio_url(audio_base_url, audio_key)
        expiry_dt = parse_expiration_header(expiration_header)
        if expiry_dt is not None:
            expiry_label = format_expiry_date(expiry_dt)
            html = _LINK_EXPIRY_TEMPLATE.format(url=url, expiry_label=expiry_label)
            return AudioLinkResult(html=html, link_included=True, expiry_label=expiry_label)

        html = _LINK_GENERIC_TEMPLATE.format(url=url)
        return AudioLinkResult(html=html, link_included=True, expiry_label=None)
    except Exception:  # noqa: BLE001 - genuinely never raise; see docstring fail-safe contract
        return AudioLinkResult(html=NO_AUDIO_LINE_HTML, link_included=False, expiry_label=None)


__all__ = [
    "PIPELINE_TIMEZONE",
    "NO_AUDIO_LINE_HTML",
    "AudioLinkResult",
    "build_audio_url",
    "redact_audio_key",
    "parse_expiration_header",
    "format_expiry_date",
    "build_audio_link_result",
]
