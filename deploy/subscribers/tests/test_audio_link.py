"""Unit tests for `audio_link.py` (PRD docs/prd/audio-link-delivery.md, ADR-0017 D1/D9/D10).

Covers: the URL builder (FR-3/FR-9/AC-6, including the negative "no guessable input
reaches the URL" assertion), the `x-amz-expiration` header parser + Europe/Berlin date
formatter (D1/FR-8/AC-9, including the "UTC->Berlin changes the calendar day" case the
ADR calls out must use a synthetic non-midnight time to be reachable at all), and the
three-outcome `build_audio_link_result()` decision table (D1/FR-10/FR-18/AC-11/AC-12/AC-20).
"""

from __future__ import annotations

from datetime import datetime, timezone

import audio_link


# ---------------------------------------------------------------------------
# build_audio_url -- FR-3/FR-9/AC-6
# ---------------------------------------------------------------------------


def test_build_audio_url_joins_base_and_key_verbatim():
    url = audio_link.build_audio_url("https://briefing.mschweier.com", "audio/.00000000-0000-4000-8000-123456789abc.mp3")

    assert url == "https://briefing.mschweier.com/audio/.00000000-0000-4000-8000-123456789abc.mp3"


def test_build_audio_url_strips_trailing_slash_on_base():
    url = audio_link.build_audio_url("https://briefing.mschweier.com/", "audio/.abc.mp3")

    assert url == "https://briefing.mschweier.com/audio/.abc.mp3"


def test_build_audio_url_preserves_the_slash_in_the_key():
    url = audio_link.build_audio_url("https://briefing.mschweier.com", "audio/.abc.mp3")

    assert url.count("/audio/") == 1
    assert "%2F" not in url


def test_build_audio_url_percent_encodes_unexpected_characters_in_the_key():
    """ADR-0017 D9 step 5: `quote(audio_key, safe="/")` so an unexpected character can
    never yield a malformed (or HTML-injecting) URL."""
    url = audio_link.build_audio_url("https://briefing.mschweier.com", 'audio/.a"b<c>.mp3')

    assert '"' not in url
    assert "<" not in url
    assert ">" not in url


def test_build_audio_url_signature_cannot_reach_the_brief_date_subject_or_any_other_guessable_input():
    """FR-3/AC-6 negative assertion: the function's own signature is the guarantee --
    it accepts only `audio_base_url` and `audio_key`, so no brief date, subject, or
    other guessable value can structurally ever reach the built URL."""
    import inspect

    params = list(inspect.signature(audio_link.build_audio_url).parameters)
    assert params == ["audio_base_url", "audio_key"]


# ---------------------------------------------------------------------------
# parse_expiration_header -- ADR-0017 D1
# ---------------------------------------------------------------------------


def test_parse_expiration_header_parses_a_real_s3_expiration_value():
    header = 'expiry-date="Sat, 01 Aug 2026 00:00:00 GMT", rule-id="expire-audio-7-days"'

    dt = audio_link.parse_expiration_header(header)

    assert dt == datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_expiration_header_returns_none_for_none():
    assert audio_link.parse_expiration_header(None) is None


def test_parse_expiration_header_returns_none_for_empty_string():
    assert audio_link.parse_expiration_header("") is None


def test_parse_expiration_header_returns_none_when_expiry_date_attr_is_missing():
    assert audio_link.parse_expiration_header('rule-id="expire-audio-7-days"') is None


def test_parse_expiration_header_returns_none_for_unparseable_date_never_raises():
    header = 'expiry-date="not a real date"'

    assert audio_link.parse_expiration_header(header) is None


# ---------------------------------------------------------------------------
# format_expiry_date -- D1/FR-8/AC-9
# ---------------------------------------------------------------------------


def test_format_expiry_date_renders_spelled_out_month_no_time():
    dt = datetime(2026, 8, 4, 0, 0, 0, tzinfo=timezone.utc)

    assert audio_link.format_expiry_date(dt) == "4 August 2026"


def test_format_expiry_date_utc_to_berlin_conversion_changes_the_calendar_day():
    """AC-9's own note: a midnight-UTC expiry never demonstrates the day-changing
    case (Berlin is always ahead of UTC), so this test uses a synthetic
    non-midnight UTC time (23:30) that DOES cross into the next Berlin calendar day."""
    dt = datetime(2026, 8, 3, 23, 30, 0, tzinfo=timezone.utc)

    assert audio_link.format_expiry_date(dt) == "4 August 2026"


def test_format_expiry_date_does_not_use_locale_dependent_day_formatting():
    """Regression guard for the ADR's explicit 'never %-d' rule -- a single-digit
    day must not carry a leading zero (which %d, another platform-dependent
    formatting choice, would produce)."""
    dt = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)

    rendered = audio_link.format_expiry_date(dt)

    assert rendered.startswith("4 ")
    assert not rendered.startswith("04 ")


# ---------------------------------------------------------------------------
# build_audio_link_result -- the D1 three-outcome table + FR-18's blank-config
# degrade + FR-10's fail-safe.
# ---------------------------------------------------------------------------


def test_build_audio_link_result_with_parseable_expiration_renders_the_ratified_expiry_line():
    result = audio_link.build_audio_link_result(
        audio_base_url="https://briefing.mschweier.com",
        audio_key="audio/.abc.mp3",
        audio_available=True,
        expiration_header='expiry-date="Tue, 04 Aug 2026 00:00:00 GMT", rule-id="expire-audio-7-days"',
    )

    assert result.link_included is True
    assert result.expiry_label == "4 August 2026"
    assert result.html == (
        '<p style="margin:0 0 6px 0;">\U0001f3a7 '
        '<a href="https://briefing.mschweier.com/audio/.abc.mp3">Listen to this brief</a> '
        "(link expires 4 August 2026)</p>"
    )


def test_build_audio_link_result_with_unparseable_expiration_renders_the_generic_line():
    result = audio_link.build_audio_link_result(
        audio_base_url="https://briefing.mschweier.com",
        audio_key="audio/.abc.mp3",
        audio_available=True,
        expiration_header=None,
    )

    assert result.link_included is True
    assert result.expiry_label is None
    assert result.html == (
        '<p style="margin:0 0 6px 0;">\U0001f3a7 '
        '<a href="https://briefing.mschweier.com/audio/.abc.mp3">Listen to this brief</a> '
        "(link available for about 7 days)</p>"
    )


def test_build_audio_link_result_when_audio_not_available_renders_the_no_audio_line():
    """404/NoSuchKey/any HeadObject error -- D1: 'a 404 means the object is not
    there, so showing a link would be showing a known-broken link'."""
    result = audio_link.build_audio_link_result(
        audio_base_url="https://briefing.mschweier.com",
        audio_key="audio/.abc.mp3",
        audio_available=False,
    )

    assert result.link_included is False
    assert result.expiry_label is None
    assert result.html == audio_link.NO_AUDIO_LINE_HTML
    assert "<a " not in result.html


def test_build_audio_link_result_when_audio_key_is_none_renders_the_no_audio_line():
    """AC-11: synthesis failed (audio_ok=False), so there is no audio_key at all."""
    result = audio_link.build_audio_link_result(
        audio_base_url="https://briefing.mschweier.com",
        audio_key=None,
        audio_available=True,
    )

    assert result.link_included is False
    assert result.html == audio_link.NO_AUDIO_LINE_HTML


def test_build_audio_link_result_when_base_url_blank_renders_the_no_audio_line_never_raises():
    """FR-18/AC-20: an unset/blank audio base URL degrades to the no-audio line."""
    result = audio_link.build_audio_link_result(
        audio_base_url="",
        audio_key="audio/.abc.mp3",
        audio_available=True,
        expiration_header='expiry-date="Tue, 04 Aug 2026 00:00:00 GMT"',
    )

    assert result.link_included is False
    assert result.html == audio_link.NO_AUDIO_LINE_HTML


def test_build_audio_link_result_when_base_url_none_renders_the_no_audio_line_never_raises():
    result = audio_link.build_audio_link_result(
        audio_base_url=None,
        audio_key="audio/.abc.mp3",
        audio_available=True,
    )

    assert result.link_included is False
    assert result.html == audio_link.NO_AUDIO_LINE_HTML


def test_build_audio_link_result_never_raises_even_on_a_malformed_expiration_header_type():
    """REVIEW FINDING (Medium), fixed: `build_audio_link_result()` used to have no
    try/except of its own, and `parse_expiration_header()`'s `re.search()` raised
    a raw `TypeError` on a non-string `expiration_header` (e.g. a hypothetical
    future botocore/S3 response-shape change) -- reaching both call sites
    (`delivery_core.py`'s `_run_delivery()`, the welcome-send handler's
    `_handle()`) UNGUARDED, despite their own comments already asserting "never
    raises, and never blocks the send". Because this runs BEFORE `send_all()` in
    the delivery worker, that would have aborted the ENTIRE send, owner included
    -- directly contradicting FR-10 and CLAUDE.md's fail-safe invariant. Fixed by
    hardening `parse_expiration_header()` itself (a malformed type degrades
    exactly like a missing/unparseable header -- the generic-note case, since the
    object IS available; only its date is unreadable) plus a top-level
    `try/except` around `build_audio_link_result()`'s whole body as a
    last-resort safety net for anything else unexpected.

    Adversarial input: a caller (or a future botocore shape change) hands
    `expiration_header` a non-string value instead of str/None. The docstring's
    'Never raises' contract holds regardless -- this is the one input shape none
    of the other tests exercise."""
    result = audio_link.build_audio_link_result(
        audio_base_url="https://briefing.mschweier.com",
        audio_key="audio/.abc.mp3",
        audio_available=True,
        expiration_header=12345,  # not str/None -- e.g. a hypothetical SDK shape change
    )

    assert result.link_included is True  # link should still show; just no exact date
    assert result.expiry_label is None


def test_no_audio_line_html_matches_the_ratified_copy_exactly():
    assert audio_link.NO_AUDIO_LINE_HTML == (
        '<p style="margin:0 0 6px 0;">\U0001f3a7 No audio version available for this edition.</p>'
    )
