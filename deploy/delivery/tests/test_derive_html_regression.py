"""Content-conversion fidelity and template-stability tests for
`delivery_core.derive_html()` / `_convert_markdown_body()` (PRD FR-2a, ADR-0014
Decision 2a) -- the flagged regression risk this whole function exists to de-risk.

CORRECTED APPROACH (this file originally diffed `derive_html()`'s output
byte-for-byte against ONE archived production `brief.html`, on the premise that
"the existing standardized design" just needed reproducing exactly). That premise
was found FALSE: pulling and diffing THREE real archived production emails
against EACH OTHER (2026-07-03, 2026-07-04, 2026-07-06 -- all three still
committed as fixtures below) showed they are three genuinely DIFFERENT HTML
document structures -- different wrapper strategy (a bare `<div>` vs. nested
`<div class="...">` vs. a `<table>`), different CSS delivery mechanism (inline
styles vs. named classes in a `<head>`-level `<style>` block vs. an in-body
`<style>` block), different color palettes, and different structural elements
present/absent from day to day (a `.tldr` callout only on one day; an "eyebrow"
label div only on another). There was never one stable template to reverse-
engineer -- the content-generation agent re-improvises the entire HTML document
fresh on every run, so a byte-for-byte diff against any single historical day was
testing conformance to an artifact of that one day's LLM output, not a real
regression target. See `delivery_core.derive_html()`'s own docstring for the full
evidence and the corrected fixed-template design.

This file now tests two genuinely different things instead:
  1. **Content-conversion fidelity** (`test_derive_html_content_fidelity.py`-style
     checks, kept in this file): given each of the three REAL markdown fixtures,
     the real headings/list items/links/bold-italic text/horizontal rules that
     fixture actually contains appear correctly transformed in
     `derive_html()`'s output. This is a check against the INPUT's own real
     content, not a diff against unrelated historical HTML output.
  2. **Template structural stability**: `derive_html()`'s own NEW, fixed template
     (doctype, matching tags, a `<title>` derived from the brief's own `# `
     heading, the stable footer line) is well-formed and consistent across
     every one of the three real inputs -- a shape/consistency check on what
     THIS function now deterministically produces, not a diff against history.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import delivery_core

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# All three real archived production briefs examined during this correction --
# spanning three consecutive days that turned out to have three mutually
# inconsistent HTML structures (see module docstring). Only the `.md` files are
# used as test input now; the `.html` files remain committed for provenance /
# historical record (anyone inspecting this fixture directory can see exactly
# what motivated the redesign) but are NOT diffed against -- there is no stable
# target to diff against, by design.
FIXTURE_MARKDOWN_FILENAMES = [
    "2026-07-03-brief.md",
    "2026-07-04-brief.md",
    "2026-07-06-brief.md",
]


def _load_fixture_markdown(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Content-conversion fidelity, against each REAL fixture's own actual content.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_every_h1_and_h2_heading_from_the_real_fixture_appears_converted(filename):
    """Every `# `/`## ` heading line the real brief actually contains must
    appear as a correctly-tagged `<h1>`/`<h2>` in the converted body -- proving
    heading conversion is faithful against real content, not a synthetic
    example.

    Expected heading text is passed through `html.escape()` before comparison:
    every real fixture's category headings contain a literal `&` (e.g.
    "Research & Models", "Industry, Deals & Strategy"), which `markdown`
    correctly HTML-entity-escapes to `&amp;` in valid HTML output -- this is
    correct, desired behavior (not a conversion bug), so the test's expected
    string must apply the same escaping, not compare against the raw
    unescaped source text."""
    import html as html_module

    markdown_text = _load_fixture_markdown(filename)
    html = delivery_core.derive_html(markdown_text)

    for line in markdown_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading_text = html_module.escape(stripped[3:].strip())
            assert f"<h2>{heading_text}</h2>" in html, f"missing h2 for {heading_text!r} in {filename}"
        elif stripped.startswith("# "):
            heading_text = html_module.escape(stripped[2:].strip())
            assert f"<h1>{heading_text}</h1>" in html, f"missing h1 for {heading_text!r} in {filename}"


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_bold_and_italic_markers_from_the_real_fixture_are_converted(filename):
    """Every real fixture uses `**bold**` (source attributions,
    e.g. `**Sources:**`) and `_italic_` (the TL;DR/summary line under the H1) --
    confirm both survive conversion into `<strong>`/`<em>` somewhere in the
    output, against the REAL fixture's own occurrences, not a synthetic
    example."""
    markdown_text = _load_fixture_markdown(filename)
    html = delivery_core.derive_html(markdown_text)

    assert "**Sources:**" in markdown_text  # sanity: the fixture really uses this
    assert "<strong>Sources:</strong>" in html

    italic_match = re.search(r"^_(.+)_$", markdown_text, re.MULTILINE)
    assert italic_match is not None  # sanity: the fixture really has an italic summary line
    assert f"<em>{italic_match.group(1)}</em>" in html


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_markdown_links_from_the_real_fixture_are_converted_with_correct_hrefs(filename):
    """Every real fixture's `[text](url)` source-attribution links must survive
    conversion into `<a href="url">text</a>` -- spot-checked against the FIRST
    such link the fixture actually contains, extracted programmatically (not a
    hand-copied example that could silently drift from the fixture)."""
    markdown_text = _load_fixture_markdown(filename)
    html = delivery_core.derive_html(markdown_text)

    link_match = re.search(r"\[([^\]]+)\]\((https?://[^)]+)\)", markdown_text)
    assert link_match is not None  # sanity: the fixture really has at least one link
    link_text, link_url = link_match.group(1), link_match.group(2)
    assert f'<a href="{link_url}">{link_text}</a>' in html


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_horizontal_rules_from_the_real_fixture_are_converted(filename):
    """Every real fixture uses `---` as a section divider -- confirm the
    converted output contains at least as many `<hr` tags as the source has
    `---` lines (an `>=` check, not `==`, since derive_html() also adds its own
    stable footer `<hr>` -- see the template-stability tests below for that
    piece)."""
    markdown_text = _load_fixture_markdown(filename)
    html = delivery_core.derive_html(markdown_text)

    source_hr_count = sum(1 for line in markdown_text.splitlines() if line.strip() == "---")
    assert source_hr_count > 0  # sanity: the fixture really uses --- dividers
    assert html.count("<hr") >= source_hr_count


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_paragraph_text_from_the_real_fixture_survives_conversion_verbatim(filename):
    """A real, distinctive sentence fragment from each fixture's body (the last
    line before the final "Sources checked" footer paragraph, extracted
    programmatically) must appear verbatim in the converted HTML -- proving
    ordinary paragraph text isn't mangled, dropped, or HTML-escaped
    unexpectedly."""
    markdown_text = _load_fixture_markdown(filename)
    html = delivery_core.derive_html(markdown_text)

    sources_checked_match = re.search(r"_Sources checked:.+?_", markdown_text, re.DOTALL)
    assert sources_checked_match is not None  # sanity: every real fixture ends this way
    # The italic markers become <em> tags, but the inner text must survive intact.
    inner_text = sources_checked_match.group(0)[1:-1]
    assert inner_text.split("Generated")[0].strip()[:80] in html


# ---------------------------------------------------------------------------
# Template structural stability -- `derive_html()`'s OWN new, fixed template
# (not a diff against any historical archived HTML).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_output_is_a_well_formed_complete_html_document(filename):
    markdown_text = _load_fixture_markdown(filename)
    html = delivery_core.derive_html(markdown_text)

    assert html.startswith("<!DOCTYPE html>")
    assert '<html lang="en">' in html
    assert "<head>" in html and "</head>" in html
    assert "<body" in html and html.rstrip().endswith("</html>")
    assert '<meta charset="UTF-8">' in html
    assert 'name="viewport"' in html


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_title_tag_is_derived_from_the_briefs_own_h1_heading(filename):
    """The `<title>` text must match the brief's own first `# ...` heading line
    -- confirmed present in all three real fixtures' `<title>` tags during this
    correction's investigation, and now made an explicit, deterministic rule
    this function follows rather than an incidental byte-for-byte artifact."""
    markdown_text = _load_fixture_markdown(filename)
    html = delivery_core.derive_html(markdown_text)

    first_heading_line = next(line for line in markdown_text.splitlines() if line.strip().startswith("# "))
    expected_title = first_heading_line.strip()[2:].strip()

    assert f"<title>{expected_title}</title>" in html


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_derive_html_does_not_bake_in_its_own_subscription_disclaimer(filename):
    """REVIEWER-FOUND BUG, FIXED: an earlier version of this template baked a
    fixed "you're receiving this because you subscribed" disclaimer into EVERY
    document it produced. Composed with `_html_with_unsubscribe_footer()` (which
    adds its own, equivalent-but-more-complete disclaimer carrying the actual
    unsubscribe link), a real subscriber ended up with the phrase appearing
    TWICE, back to back. `derive_html()` must NOT produce any such text on its
    own -- that messaging belongs exclusively to `_html_with_unsubscribe_footer()`
    (for subscribers) and is simply absent for the owner's copy (whose
    `_html_with_header()` banner already covers subscription-context messaging
    at the top). See `deploy/delivery/tests/test_html_composition.py` for the
    full composed-pipeline regression tests this bug required."""
    markdown_text = _load_fixture_markdown(filename)
    html = delivery_core.derive_html(markdown_text)

    assert "subscribed" not in html.lower()
    assert "unsubscribe" not in html.lower()


def test_derive_html_output_never_defines_an_email_footer_text_constant_reference():
    """Documents the removal explicitly: `derive_html()`'s own docstring/module
    constants no longer include a fixed footer-disclaimer string at all (the
    old `_EMAIL_FOOTER_TEXT` module constant is gone, not just unused)."""
    assert not hasattr(delivery_core, "_EMAIL_FOOTER_TEXT")


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_content_style_block_is_present_and_placed_inside_the_body_not_head(filename):
    """The scoped `<style>` block styling the converted content's headings/
    paragraphs/links/etc. must be placed INSIDE `<body>`, not `<head>` -- the
    documented cross-client-compatibility reason (Gmail strips `<head>`-level
    `<style>` blocks) is the whole point of this placement; a regression here
    would silently defeat that reasoning."""
    markdown_text = _load_fixture_markdown(filename)
    html = delivery_core.derive_html(markdown_text)

    head_start = html.index("<head>")
    head_end = html.index("</head>")
    body_start = html.index("<body")

    style_block_position = html.index("<style>")
    assert style_block_position > body_start
    assert not (head_start < style_block_position < head_end)


def test_derive_html_output_is_consistent_across_repeated_calls_with_the_same_input():
    """The whole point of this correction: derive_html() must be genuinely
    deterministic -- the SAME markdown input must produce byte-identical HTML
    output on every call, unlike the non-deterministic LLM-improvised
    conversion this replaces."""
    markdown_text = _load_fixture_markdown("2026-07-06-brief.md")

    first_call = delivery_core.derive_html(markdown_text)
    second_call = delivery_core.derive_html(markdown_text)
    third_call = delivery_core.derive_html(markdown_text)

    assert first_call == second_call == third_call


def test_derive_html_uses_no_extensions_by_design():
    """Documents (and pins) the judgment call `_convert_markdown_body()`'s
    docstring makes explicit: all three real fixtures (2026-07-03/04/06) needed
    ZERO markdown extensions to convert faithfully -- no tables, no fenced code
    blocks, no nl2br. This test fails loudly if a future edit adds an
    `extensions=` argument to the actual `markdown.markdown(...)` call without a
    fixture proving it's still needed and still faithful -- forcing that
    decision to be deliberate, not silent. Checks the *executable* source
    (comments/strings stripped via tokenize, mirroring
    deploy/managed-agent/tests/test_audio_email_fanout.py's
    `test_no_credential_file_loading_anywhere_in_the_module`), so the
    function's own explanatory docstring -- which necessarily discusses
    "extensions" in prose, for exactly the reason this test exists -- doesn't
    produce a false positive."""
    import inspect
    import io
    import tokenize

    source = inspect.getsource(delivery_core._convert_markdown_body)
    code_tokens = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE)
    ]
    code_only = " ".join(code_tokens)

    assert "extensions" not in code_only


@pytest.mark.parametrize("filename", FIXTURE_MARKDOWN_FILENAMES)
def test_none_of_the_three_real_fixtures_needs_a_table_or_fenced_code_extension(filename):
    """Direct sanity check on the claim `_convert_markdown_body()`'s docstring
    makes: none of the three real fixtures actually contains a Markdown pipe
    table or a fenced code block. If a future brief legitimately needs one,
    THIS test (not just the no-extensions-in-source test above) is the one that
    should start failing first, precisely locating which fixture drove the
    need."""
    markdown_text = _load_fixture_markdown(filename)

    assert not re.search(r"^\|.+\|$", markdown_text, re.MULTILINE)
    assert "```" not in markdown_text


# ---------------------------------------------------------------------------
# Sanity checks on simple, synthetic input (independent of the real fixtures --
# useful for pinpointing a break without needing to reason about a full brief).
# ---------------------------------------------------------------------------


def test_derive_html_produces_a_string_not_none_or_bytes():
    result = delivery_core.derive_html("# Hello\n\nA paragraph.")
    assert isinstance(result, str)
    assert "<h1>Hello</h1>" in result


def test_derive_html_handles_headings_bold_italics_links_and_hr():
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


def test_derive_html_falls_back_to_default_title_when_no_h1_heading_present():
    """Fail-safe: malformed/empty input must never raise -- degrades to a
    generic title rather than crashing the whole delivery."""
    html = delivery_core.derive_html("Just a paragraph, no heading at all.")

    assert f"<title>{delivery_core._DEFAULT_EMAIL_TITLE}</title>" in html


def test_convert_markdown_body_is_a_pure_fragment_with_no_document_wrapper():
    """`_convert_markdown_body()` (the piece `derive_html()` calls internally)
    must NOT itself produce a doctype/html/head/body -- confirming the
    template-assembly responsibility is cleanly separated into `derive_html()`
    alone, not duplicated or leaked into the conversion step."""
    result = delivery_core._convert_markdown_body("# Title\n\nBody text.")

    assert "<!DOCTYPE" not in result
    assert "<html" not in result
    assert "<body" not in result
