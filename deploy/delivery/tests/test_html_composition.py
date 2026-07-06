"""Regression tests for the FULL end-to-end HTML composition pipeline --
`derive_html()` -> `_html_with_header()` -> `_html_with_unsubscribe_footer()` --
exactly as `send_all()` actually chains them for a real subscriber (and the
`derive_html()` -> `_html_with_header()` chain for the owner's copy, which never
gets the unsubscribe footer).

WHY THIS FILE EXISTS (reviewer-found bug, independently reproduced by the
coordinator): `derive_html()` was corrected (a prior fix) to return a COMPLETE
HTML document (`<!DOCTYPE html>...</html>`) instead of a bare fragment.
`_html_with_header()`/`_html_with_unsubscribe_footer()` still did naive string
concatenation (`header + html_body` / `html_body + footer`) -- correct only when
`html_body` was a bare fragment, now broken. The composed subscriber HTML a real
recipient would receive had the unsubscribe footer -- including the ACTUAL
unsubscribe link -- land AFTER the closing `</html>` tag, outside the document
root entirely (content there is invalid HTML and renders unreliably across email
clients; this is the ONE thing that footer must reliably deliver). Separately,
`derive_html()` also used to bake its own fixed "you're receiving this because
you subscribed" disclaimer into every document, which -- once composed with
`_html_with_unsubscribe_footer()`'s own equivalent-but-more-complete messaging --
produced two near-identical disclaimers back to back for every subscriber.

The existing test suite never caught either issue: `test_derive_html_regression.py`
tests `derive_html()` in isolation; `test_delivery_core_send_all.py` feeds
`_html_with_header()`/`_html_with_unsubscribe_footer()` a trivial `"<p>brief</p>"`
fragment (which the naive prepend/append handles fine, masking the bug) rather
than real `derive_html()` output. This file closes that gap by testing the
REAL composed pipeline end to end, using a real fixture, exactly as `send_all()`
actually calls it.
"""

from __future__ import annotations

from pathlib import Path

import delivery_core

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_fixture_markdown(filename: str = "2026-07-06-brief.md") -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8")


def _compose_subscriber_html(brief_markdown: str, *, feedback_link=None, unsubscribe_link="https://briefing.mschweier.com/unsubscribe?email=alice%40example.com&token=tok-123") -> str:
    """Exactly the chain `send_all()` applies for a real subscriber
    (`delivery_core.py`'s subscriber-fanout branch): derive -> header -> footer."""
    derived = delivery_core.derive_html(brief_markdown)
    with_header = delivery_core._html_with_header(derived, feedback_link)
    return delivery_core._html_with_unsubscribe_footer(with_header, unsubscribe_link)


def _compose_owner_html(brief_markdown: str, *, feedback_link=None) -> str:
    """Exactly the chain `send_all()` applies for the owner's own copy: derive ->
    header only (the owner never gets `_html_with_unsubscribe_footer()`)."""
    derived = delivery_core.derive_html(brief_markdown)
    return delivery_core._html_with_header(derived, feedback_link)


# ---------------------------------------------------------------------------
# THE core regression check (this phase's exact requested test): exactly one
# </html>, at the true end of the string, nothing after it.
# ---------------------------------------------------------------------------


def test_composed_subscriber_html_has_exactly_one_html_close_tag_at_the_true_end():
    """The bug's precise signature, reproduced independently by the coordinator
    before this fix: `</html>` landed ~190 bytes before the actual end of the
    composed string, with real footer markup (including the unsubscribe link)
    sitting after it. This test pins the fix: exactly one `</html>`, and
    nothing but (at most) trailing whitespace follows it."""
    brief_markdown = _load_fixture_markdown()

    subscriber_html = _compose_subscriber_html(
        brief_markdown,
        feedback_link="https://feedback.mschweier.com/?t=abc123",
    )

    assert subscriber_html.count("</html>") == 1

    close_tag_position = subscriber_html.index("</html>")
    trailing_content = subscriber_html[close_tag_position + len("</html>"):]
    assert trailing_content.strip() == "", f"content found after </html>: {trailing_content!r}"


def test_composed_subscriber_html_unsubscribe_link_is_genuinely_inside_the_document():
    """Not just "no content after </html>" in the abstract -- specifically, the
    real unsubscribe link (the one thing this footer exists to deliver) must
    itself be positioned BEFORE </html>, i.e. actually inside the rendered
    document a mail client would show."""
    brief_markdown = _load_fixture_markdown()
    unsubscribe_link = "https://briefing.mschweier.com/unsubscribe?email=alice%40example.com&token=tok-123"

    subscriber_html = _compose_subscriber_html(brief_markdown, unsubscribe_link=unsubscribe_link)

    close_tag_position = subscriber_html.index("</html>")
    link_position = subscriber_html.index(unsubscribe_link)

    assert link_position < close_tag_position


def test_composed_subscriber_html_has_no_content_before_doctype_either():
    """The other half of "inside the document": the header banner must not have
    been prepended BEFORE <!DOCTYPE html> either (the old blind-prepend bug's
    mirror-image failure mode on the header side) -- the document must still
    start with the doctype."""
    brief_markdown = _load_fixture_markdown()

    subscriber_html = _compose_subscriber_html(brief_markdown, feedback_link="https://feedback.mschweier.com/?t=abc123")

    assert subscriber_html.startswith("<!DOCTYPE html>")


def test_composed_subscriber_html_has_exactly_one_pair_of_doctype_and_html_tags():
    """Sanity check that the insertion logic didn't accidentally duplicate the
    document's own structural tags while splicing content in."""
    brief_markdown = _load_fixture_markdown()

    subscriber_html = _compose_subscriber_html(brief_markdown, feedback_link="https://feedback.mschweier.com/?t=abc123")

    assert subscriber_html.count("<!DOCTYPE html>") == 1
    assert subscriber_html.count("<html") == 1
    assert subscriber_html.count("</html>") == 1
    assert subscriber_html.count("<body") == 1
    assert subscriber_html.count("</body>") == 1


# ---------------------------------------------------------------------------
# The duplicate-disclaimer bug (the other half of the reviewer's finding).
# ---------------------------------------------------------------------------


def test_composed_subscriber_html_has_no_duplicate_subscription_disclaimer():
    """derive_html() no longer bakes in its own fixed "you're receiving this
    because you subscribed" line -- confirming the ONLY such disclaimer a real
    subscriber sees is the one `_html_with_unsubscribe_footer()` provides
    (which additionally carries the real unsubscribe link)."""
    brief_markdown = _load_fixture_markdown()

    subscriber_html = _compose_subscriber_html(brief_markdown)

    # Case-insensitive, substring-tolerant count of the recurring phrase both
    # the old baked-in line and the footer's own line shared.
    assert subscriber_html.lower().count("subscribed to the") == 1


def test_composed_owner_html_has_no_subscription_disclaimer_at_all():
    """The owner's copy never gets `_html_with_unsubscribe_footer()` (no
    unsubscribe link makes sense for the owner) -- confirming derive_html()'s
    removal of its own baked-in disclaimer means the owner's copy correctly has
    NONE, not a stray leftover one."""
    brief_markdown = _load_fixture_markdown()

    owner_html = _compose_owner_html(brief_markdown, feedback_link=None)

    assert "subscribed to the" not in owner_html.lower()


# ---------------------------------------------------------------------------
# Header banner and footer are actually present, correctly positioned, styled
# as before, and the feedback link (when supplied) rides along correctly.
# ---------------------------------------------------------------------------


def test_header_banner_is_the_first_thing_inside_body_and_before_the_original_content():
    brief_markdown = _load_fixture_markdown()
    derived = delivery_core.derive_html(brief_markdown)

    with_header = delivery_core._html_with_header(derived, feedback_link=None)

    body_open_position = with_header.index("<body")
    banner_position = with_header.index("curated and written by an AI agent")
    original_h1_position = with_header.index("<h1>")

    assert body_open_position < banner_position < original_h1_position


def test_footer_is_the_last_thing_inside_body_and_after_the_original_content():
    brief_markdown = _load_fixture_markdown()
    derived = delivery_core.derive_html(brief_markdown)
    with_header = delivery_core._html_with_header(derived, feedback_link=None)

    subscriber_html = delivery_core._html_with_unsubscribe_footer(with_header, "https://example.com/unsub")

    body_close_position = subscriber_html.index("</body>")
    footer_position = subscriber_html.index("Unsubscribe")
    original_h1_position = subscriber_html.index("<h1>")

    assert original_h1_position < footer_position < body_close_position


def test_feedback_link_survives_full_composition_when_supplied():
    brief_markdown = _load_fixture_markdown()

    subscriber_html = _compose_subscriber_html(
        brief_markdown,
        feedback_link="https://feedback.mschweier.com/?t=xyz789",
    )

    assert "https://feedback.mschweier.com/?t=xyz789" in subscriber_html
    assert "Share feedback" in subscriber_html


def test_feedback_link_absent_gracefully_when_not_supplied():
    brief_markdown = _load_fixture_markdown()

    subscriber_html = _compose_subscriber_html(brief_markdown, feedback_link=None)

    assert "Share feedback" not in subscriber_html


def test_original_derive_html_content_is_still_fully_present_after_composition():
    """Confirms the insertion logic didn't accidentally clobber or truncate any
    of the actual brief content while splicing the banner/footer in."""
    brief_markdown = _load_fixture_markdown()
    derived = delivery_core.derive_html(brief_markdown)

    subscriber_html = _compose_subscriber_html(brief_markdown)

    # Every line of the ORIGINAL derive_html() output must still be present,
    # in order, inside the composed document (the banner/footer are pure
    # insertions, never a replacement of any existing content).
    for original_line in derived.splitlines():
        assert original_line in subscriber_html


# ---------------------------------------------------------------------------
# Insertion-helper unit tests, independent of the full pipeline (pinpoint a
# break in the mechanism itself, not just its end-to-end symptom).
# ---------------------------------------------------------------------------


def test_insert_after_body_open_tag_places_content_right_after_body_tag():
    document = "<html><body style=\"color:red\">ORIGINAL</body></html>"

    result = delivery_core._insert_after_body_open_tag(document, "INSERTED")

    assert result == '<html><body style="color:red">INSERTEDORIGINAL</body></html>'


def test_insert_before_body_close_tag_places_content_right_before_close_tag():
    document = "<html><body>ORIGINAL</body></html>"

    result = delivery_core._insert_before_body_close_tag(document, "INSERTED")

    assert result == "<html><body>ORIGINALINSERTED</body></html>"


def test_insert_after_body_open_tag_falls_back_to_prepend_when_no_body_tag_found():
    """Fail-safe: a caller passing a bare fragment (no <body> at all) must not
    raise -- degrades to the old prepend behavior, which is at least
    non-destructive even if not perfectly positioned relative to a document
    root that, in that case, doesn't exist."""
    fragment = "<p>just a fragment, no body tag</p>"

    result = delivery_core._insert_after_body_open_tag(fragment, "INSERTED")

    assert result == "INSERTED<p>just a fragment, no body tag</p>"


def test_insert_before_body_close_tag_falls_back_to_append_when_no_body_tag_found():
    fragment = "<p>just a fragment, no body tag</p>"

    result = delivery_core._insert_before_body_close_tag(fragment, "INSERTED")

    assert result == "<p>just a fragment, no body tag</p>INSERTED"


def test_insert_after_body_open_tag_is_case_insensitive_and_tolerates_attributes():
    document = '<HTML><BODY CLASS="foo" STYLE="color:blue">ORIGINAL</BODY></HTML>'

    result = delivery_core._insert_after_body_open_tag(document, "X")

    assert result == '<HTML><BODY CLASS="foo" STYLE="color:blue">XORIGINAL</BODY></HTML>'


def test_insert_before_body_close_tag_is_case_insensitive():
    document = "<html><BODY>ORIGINAL</BODY></html>"

    result = delivery_core._insert_before_body_close_tag(document, "X")

    assert result == "<html><BODY>ORIGINALX</BODY></html>"
