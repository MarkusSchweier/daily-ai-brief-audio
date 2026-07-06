"""Regression test for `delivery_core.derive_html()` (PRD FR-2a, ADR-0014 Decision
2a) against a REAL production brief -- the flagged regression risk this whole
function exists to de-risk, not a rubber-stamp refactor check.

Fixture provenance: `2026-07-06-brief.md` / `2026-07-06-brief.html` are a genuine
production output from the live scheduled Managed Agents run on 2026-07-06,
copied verbatim (not hand-edited) from a real archived
`s3://cowork-polly-tts-740353583786/briefs/2026-07-06/` folder.

CRITICAL detail this test's comparison depends on (traced directly from
`deploy/managed-agent/pipeline/audio_email.py`): the archived `briefs/<date>/brief.html`
is the RAW, pre-header/footer-wrap conversion output --
`audio_email.py:159` reads `BRIEF_HTML_PATH` into a module-level `brief_html`
variable, and `audio_email.py:581-589`'s call to
`brief_history.archive_todays_brief(..., html=brief_html, ...)` passes that SAME raw
variable -- NOT the per-recipient `owner_html`/`subscriber_html` `send_all()`
computes later by wrapping with `_html_with_header()`/
`_html_with_unsubscribe_footer()`. So `derive_html()`'s output must be diffed
DIRECTLY against the archived fixture, with NO header/footer wrapping applied for
the comparison -- the wrapping happens as a separate, later, unchanged step
(covered by its own tests in test_delivery_core_send_all.py).

The archived `brief.html`, however, is a FULL HTML DOCUMENT (`<html>`/`<head>`/an
outer styled table with its own `<style>` block, plus a "You're receiving this..."
footer div) -- that surrounding document chrome is NOT produced by the
content-generation agent's Markdown->HTML conversion step at all; it is a SEPARATE
delivery-side wrapping layer already applied before archival (distinct from, and in
addition to, `_html_with_header()`/`_html_with_unsubscribe_footer()`, which apply on
top of THAT). What `derive_html()` reproduces is specifically the INNER content
region -- from the first `<h1>` through the final closing `</p>` -- which is exactly
what the agent's `markdown.markdown(...)` call itself would have produced from
`brief.md`. This test isolates that inner region before comparing.
"""

from __future__ import annotations

from pathlib import Path

import delivery_core

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_MARKDOWN_PATH = FIXTURES_DIR / "2026-07-06-brief.md"
FIXTURE_HTML_PATH = FIXTURES_DIR / "2026-07-06-brief.html"

# The archived full-document fixture's inner-content boundaries: everything between
# the closing `</style>` tag (the last thing before the agent's own Markdown-derived
# body begins) and the delivery-side footer `<tr>` (which is NOT part of the agent's
# conversion output at all -- a separate wrapping step).
_INNER_START_MARKER = "</style>"
_INNER_END_MARKER = "</td></tr>"


def _extract_inner_markdown_derived_region(full_html_document: str) -> str:
    """Isolate the region of the archived full HTML document that corresponds
    exactly to what `markdown.markdown(brief_markdown)` itself produced -- i.e.
    excluding the surrounding delivery-side document chrome (the outer
    `<html>`/`<head>`/table/`<style>` wrapper and the "You're receiving
    this..." footer div, both already delivery-owned and unrelated to this
    conversion step)."""
    start = full_html_document.index(_INNER_START_MARKER) + len(_INNER_START_MARKER)
    end = full_html_document.index(_INNER_END_MARKER, start)
    return full_html_document[start:end].strip()


def test_derive_html_matches_real_production_brief_byte_for_byte():
    """The core regression check (AC-2a): `derive_html()` on the REAL production
    Markdown must reproduce the REAL production HTML body byte-for-byte (after
    excluding the surrounding document chrome that isn't part of the conversion
    step at all -- see module docstring). If this ever fails, it means either a
    `markdown` library version change altered its default output, or a future
    brief uses a Markdown feature (a table, fenced code, etc.) the current
    zero-extensions configuration doesn't handle identically to how the
    content-generation agent's own ad hoc conversion once handled it -- in either
    case, this must be investigated and the fixture/extensions revisited
    deliberately, never silently patched around."""
    brief_markdown = FIXTURE_MARKDOWN_PATH.read_text(encoding="utf-8")
    full_archived_html = FIXTURE_HTML_PATH.read_text(encoding="utf-8")
    expected_inner_html = _extract_inner_markdown_derived_region(full_archived_html)

    derived_html = delivery_core.derive_html(brief_markdown).strip()

    assert derived_html == expected_inner_html


def test_derive_html_uses_no_extensions_by_design():
    """Documents (and pins) the judgment call `derive_html()`'s docstring makes
    explicit: the real 2026-07-06 production brief needed ZERO markdown
    extensions to reproduce exactly -- no tables, no fenced code blocks, no
    nl2br. This test fails loudly if a future edit adds an `extensions=` argument
    to the actual `markdown.markdown(...)` call without a fixture proving it's
    still needed and still faithful -- forcing that decision to be deliberate,
    not silent. Checks the *executable* source (comments/strings stripped via
    tokenize, mirroring
    deploy/managed-agent/tests/test_audio_email_fanout.py's
    `test_no_credential_file_loading_anywhere_in_the_module`), so the function's
    own explanatory docstring -- which necessarily discusses "extensions" in
    prose, for exactly the reason this test exists -- doesn't produce a false
    positive."""
    import inspect
    import io
    import tokenize

    source = inspect.getsource(delivery_core.derive_html)
    code_tokens = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE)
    ]
    code_only = " ".join(code_tokens)

    assert "extensions" not in code_only


def test_derive_html_produces_a_string_not_none_or_bytes():
    result = delivery_core.derive_html("# Hello\n\nA paragraph.")
    assert isinstance(result, str)
    assert "<h1>Hello</h1>" in result


def test_derive_html_handles_headings_bold_italics_links_and_hr():
    """Sanity check on the specific Markdown features the real fixture actually
    uses (headings, bold, italics, links, horizontal rules) -- confirming
    derive_html() handles each independently of the full-fixture byte-for-byte
    test above."""
    markdown_text = (
        "# Title\n\n"
        "## Section\n\n"
        "**bold text** and _italic text_ and a [link](https://example.com).\n\n"
        "---\n\n"
        "Another paragraph."
    )
    html = delivery_core.derive_html(markdown_text)

    assert "<h1>Title</h1>" in html
    assert "<h2>Section</h2>" in html
    assert "<strong>bold text</strong>" in html
    assert "<em>italic text</em>" in html
    assert '<a href="https://example.com">link</a>' in html
    assert "<hr" in html
