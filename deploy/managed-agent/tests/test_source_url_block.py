"""Unit tests for deploy/managed-agent/tools/source_url_block.py.

The extraction rules this file pins down are a CONTRACT, not an implementation detail:
the drift test (test_source_url_block_drift.py) compares two committed prompts against
this generator's output byte for byte, so a silent change in how a URL is matched,
ordered or de-duplicated would either break both prompts at once or -- worse -- change
which URL STRING the agent is handed. Verbatim is the load-bearing property, because the
platform matches URL strings against prior context (docs/adr/0018, D4).

Fixture text throughout, per D7, so the rules are exercised on cases today's real
`sources.md` does not happen to contain (duplicates, angle brackets, a pre-heading URL),
plus a few assertions against the real file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import source_url_block as sub  # noqa: E402


# --- extraction ---------------------------------------------------------------------


def test_urls_come_out_in_document_order():
    """Document order is the skill's own tier order, and it keeps each outlet's feed URL
    adjacent to its site URL -- which is what lets the block's prose say "that outlet's
    own site page is listed next to its feed"."""
    text = (
        "## Tier 1\n"
        "- Alpha: https://alpha.example/news  · feed: https://alpha.example/feed\n"
        "- Beta: https://beta.example/blog/\n"
        "## Tier 2\n"
        "- Gamma: https://gamma.example/\n"
    )

    assert sub.extract_source_urls(text) == [
        "https://alpha.example/news",
        "https://alpha.example/feed",
        "https://beta.example/blog/",
        "https://gamma.example/",
    ]


def test_duplicate_urls_are_dropped_keeping_the_first_occurrence():
    text = (
        "## Tier 1\n"
        "- Alpha: https://alpha.example/news\n"
        "## Tier 2\n"
        "- Alpha again: https://alpha.example/news\n"
        "- Beta: https://beta.example/\n"
    )

    assert sub.extract_source_urls(text) == ["https://alpha.example/news", "https://beta.example/"]
    # The second (de-duplicated) group still renders, carrying only its surviving URL.
    block = sub.render_source_url_block(text)
    assert "Tier 1\nhttps://alpha.example/news\n" in block
    assert "Tier 2\nhttps://beta.example/\n" in block


def test_a_group_whose_only_urls_were_duplicates_is_dropped_entirely():
    """A heading with nothing under it would be prompt tokens with no content."""
    text = "## Tier 1\n- https://alpha.example/\n## Tier 2\n- https://alpha.example/\n"

    block = sub.render_source_url_block(text)

    assert "Tier 1" in block
    assert "Tier 2" not in block


def test_trailing_sentence_punctuation_and_the_middle_dot_separator_are_stripped():
    """`·` is `sources.md`'s own feed/site separator, so it can end up glued to a URL."""
    text = (
        "## Tier 1\n"
        "- Alpha: https://alpha.example/news.\n"
        "- Beta: https://beta.example/,\n"
        "- Gamma: https://gamma.example/·\n"
        "- Delta: https://delta.example/a?b=1;\n"
    )

    assert sub.extract_source_urls(text) == [
        "https://alpha.example/news",
        "https://beta.example/",
        "https://gamma.example/",
        "https://delta.example/a?b=1",
    ]


def test_markdown_and_angle_bracket_delimiters_are_not_swallowed():
    text = (
        "## Tier 1\n"
        "- [Alpha](https://alpha.example/news) and <https://beta.example/feed>\n"
        '- "https://gamma.example/" plus `https://delta.example/`\n'
        "- [Epsilon][https://epsilon.example/]\n"
    )

    assert sub.extract_source_urls(text) == [
        "https://alpha.example/news",
        "https://beta.example/feed",
        "https://gamma.example/",
        "https://delta.example/",
        "https://epsilon.example/",
    ]


def test_verify_markers_are_dropped_and_the_url_itself_is_untouched():
    """`(verify)` is maintainer-facing prose about an unconfirmed feed endpoint. The URL
    is listed unchanged; the block's fallback rule covers an endpoint that is wrong."""
    text = "## Tier 1\n- Alpha — Blog: https://alpha.example/blog/  · feed: https://alpha.example/rss.xml (verify)\n"

    assert sub.extract_source_urls(text) == ["https://alpha.example/blog/", "https://alpha.example/rss.xml"]
    assert "verify" not in sub.render_source_url_block(text)


def test_urls_are_not_normalized():
    """Scheme, case and trailing slash stay verbatim: the agent must be handed exactly
    the string it will try to fetch."""
    text = "## Tier 1\n- http://Alpha.Example/News/\n- https://beta.example\n"

    assert sub.extract_source_urls(text) == ["http://Alpha.Example/News/", "https://beta.example"]


def test_non_http_schemes_and_bare_hostnames_are_ignored():
    text = (
        "## Tier 1\n"
        "- mailto:someone@alpha.example and ftp://alpha.example/pub\n"
        "- www.beta.example (no scheme)\n"
        "- https://gamma.example/\n"
    )

    assert sub.extract_source_urls(text) == ["https://gamma.example/"]


# --- headings -----------------------------------------------------------------------


def test_headings_are_copied_verbatim_minus_the_markup():
    text = "## Tier 4 — Tech press (industry + developer pulse)\n- https://alpha.example/\n"

    block = sub.render_source_url_block(text)

    assert "\nTier 4 — Tech press (industry + developer pulse)\nhttps://alpha.example/\n" in block
    assert "## Tier 4" not in block


def test_urls_before_the_first_heading_are_grouped_under_ungrouped():
    text = "Intro prose with https://early.example/ in it.\n## Tier 1\n- https://alpha.example/\n"

    block = sub.render_source_url_block(text)

    assert "\nUngrouped\nhttps://early.example/\n" in block
    assert sub.extract_source_urls(text) == ["https://early.example/", "https://alpha.example/"]


def test_deeper_and_shallower_markdown_headings_are_not_group_headings():
    text = "# Title\n### Sub\n- https://alpha.example/\n"

    block = sub.render_source_url_block(text)

    assert "\nUngrouped\nhttps://alpha.example/\n" in block
    assert "Title" not in block
    assert "Sub" not in block


# --- rendering ----------------------------------------------------------------------


def test_block_starts_with_the_begin_marker_and_ends_with_the_end_marker():
    block = sub.render_source_url_block("## Tier 1\n- https://alpha.example/\n")

    assert block.startswith(sub.BLOCK_BEGIN_MARKER)
    assert block.endswith(sub.BLOCK_END_MARKER)
    assert block.count(sub.BLOCK_BEGIN_MARKER) == 1
    assert block.count(sub.BLOCK_END_MARKER) == 1
    assert not block.endswith("\n")  # no trailing newline: the carrier owns its separator


def test_markers_are_plain_ascii_single_lines():
    """They have to survive JSON round-tripping into `deployment.json` and be greppable."""
    for marker in (sub.BLOCK_BEGIN_MARKER, sub.BLOCK_END_MARKER):
        assert marker.isascii()
        assert "\n" not in marker
        assert marker.startswith("=== ") and marker.endswith(" ===")


def test_rendering_is_deterministic():
    text = sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8")

    assert sub.render_source_url_block(text) == sub.render_source_url_block(text)


def test_rendering_raises_when_there_are_no_urls_at_all():
    """A `sources.md` restructure must never silently produce an empty block (D4)."""
    assert sub.extract_source_urls("## Tier 1\n- nothing fetchable here\n") == []

    with pytest.raises(sub.SourceUrlBlockError):
        sub.render_source_url_block("## Tier 1\n- nothing fetchable here\n")


def test_every_extracted_url_appears_exactly_once_in_the_rendered_block():
    text = sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8")
    block = sub.render_source_url_block(text)

    urls = sub.extract_source_urls(text)
    assert urls
    for url in urls:
        assert block.count(f"\n{url}\n") == 1, url


# --- the block's prose (PRD FR-4 / AC-2) --------------------------------------------


def test_the_block_states_all_four_rules():
    block = sub.render_source_url_block(sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8"))
    lowered = block.lower()

    # 1. why the URLs are here, and that reading sources.md from disk does not count
    assert "web_fetch can only retrieve a url that already appeared" in lowered
    assert "reading sources.md from disk does not count" in lowered
    # 2. a fetched feed/index page unlocks the article links inside it
    assert "article links inside it" in lowered
    # 3. never retry a url_not_in_prior_context rejection
    assert "url_not_in_prior_context" in block
    assert "do not retry that url" in lowered
    # 4. fall back via the skill's own chain, ending at web_search
    assert "site/index page" in lowered
    assert "web_search" in block


def test_the_block_does_not_instruct_exhaustive_fetching():
    """D5's cost guard, and the one thing in the prose that is load-bearing by its
    ABSENCE: fetch budgeting stays with the skill."""
    block = sub.render_source_url_block(sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8"))
    lowered = block.lower()

    assert "not an instruction to\n  fetch everything" in lowered
    assert "budget your fetches exactly as the skill says" in lowered
    for forbidden in ("fetch all of", "fetch every url", "fetch each url", "fetch them all"):
        assert forbidden not in lowered


def test_the_block_emits_no_outlet_labels():
    """URLs only (D4): re-emitting the labels would create a second copy of the exact
    strings the skill's `source-usage.json` contract depends on, in a file the drift test
    checks for generator equality but not for semantic agreement with the skill."""
    block = sub.render_source_url_block(sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8"))

    for label in ("Anthropic — News", "Hugging Face Daily Papers", "TechCrunch — AI"):
        assert label not in block


# --- against the real sources.md ----------------------------------------------------


def test_the_real_sources_md_is_where_the_generator_expects_it():
    assert sub.DEFAULT_SOURCES_PATH.is_file(), sub.DEFAULT_SOURCES_PATH
    assert sub.DEFAULT_SOURCES_PATH.parts[-3:] == ("skills", "daily-ai-brief", "sources.md")


def test_the_real_sources_md_yields_a_plausible_list_of_absolute_urls():
    urls = sub.extract_source_urls(sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8"))

    assert len(urls) >= 50  # 62 on 2026-10-17; a collapse to a handful means extraction broke
    assert len(set(urls)) == len(urls)
    assert all(url.startswith("http") for url in urls)
    assert all(" " not in url for url in urls)


# --- CLI ----------------------------------------------------------------------------


def test_cli_prints_the_block_for_the_default_sources_file(capsys):
    assert sub.main([]) == 0

    printed = capsys.readouterr().out
    assert printed == sub.render_source_url_block(sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8")) + "\n"


def test_cli_sources_override_is_honored(tmp_path, capsys):
    sources = tmp_path / "sources.md"
    sources.write_text("## Tier 1\n- https://alpha.example/\n", encoding="utf-8")

    assert sub.main(["--sources", str(sources)]) == 0

    printed = capsys.readouterr().out
    assert "https://alpha.example/" in printed
    assert "https://www.anthropic.com/news" not in printed  # i.e. not the real file


def test_cli_json_string_mode_prints_a_pasteable_ascii_json_literal(capsys):
    assert sub.main(["--json-string"]) == 0

    printed = capsys.readouterr().out.rstrip("\n")
    assert printed.startswith('"') and printed.endswith('"')
    assert printed.isascii()  # the tier headings' em dashes are escaped, as deployment.json's are
    assert json.loads(printed) == sub.render_source_url_block(
        sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8")
    )


def test_cli_fails_loudly_on_a_sources_file_with_no_urls(tmp_path):
    sources = tmp_path / "sources.md"
    sources.write_text("# Nothing here\n", encoding="utf-8")

    with pytest.raises(sub.SourceUrlBlockError):
        sub.main(["--sources", str(sources)])
