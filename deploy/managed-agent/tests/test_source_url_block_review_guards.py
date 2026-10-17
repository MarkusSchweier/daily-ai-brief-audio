"""Reviewer-added guards for the ADR-0018 source-URL block generator.

These complement `test_source_url_block.py` (which pins the extraction RULES on
fixture text) and `test_source_url_block_drift.py` (which pins the two CARRIERS) with
the one property neither asserts directly: that the generator's regex, run on the
**real** `sources.md`, produces exactly the URLs a human reading that file would see --
character for character. "Verbatim" is the load-bearing property of the whole decision
(the platform matches URL STRINGS against prior context), so a URL that is extracted
*nearly* right is worse than one not extracted at all: the drift test would still pass,
both carriers would agree, and production would be handed a URL that cannot be fetched.

So:

  * `test_the_generator_agrees_with_an_independent_extraction...` re-extracts the real
    file with a deliberately different algorithm (a whitespace-terminated scan, no
    structural-delimiter set, no trailing-punctuation strip) and demands equality. On
    today's `sources.md` the generator's extra rules are all no-ops, so any future
    divergence means one of them has started FIRING on a real URL -- which is exactly
    the moment a human needs to look.
  * The `..._hazard` tests are forward-looking guards on the real file for the three
    shapes where the documented extraction rules would silently mangle a URL: a `)`
    inside a URL path, a `·` glued to a URL, and a URL on a `## ` heading line.
    `sources.md` has none today; these fail on the commit that introduces one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import source_url_block as sub  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
MANAGED_AGENT_DIR = REPO_ROOT / "deploy" / "managed-agent"
CANDIDATES_DIR = REPO_ROOT / "deploy" / "candidates"
SKILL_DIR = MANAGED_AGENT_DIR / "skills" / "daily-ai-brief"

_WHITESPACE = set(" \t\r\n\x0b\x0c ")


def _sources_text() -> str:
    return sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8")


def _independent_urls(text: str) -> list[str]:
    """Every `http(s)://...` run up to the next whitespace, in document order.

    Deliberately NOT the generator's regex: no structural-delimiter set, no trailing
    punctuation strip, no heading handling. Duplicates removed the same way (first
    occurrence wins) so only the MATCHING is being compared.
    """
    found: list[str] = []
    seen: set[str] = set()
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("http://", index) or text.startswith("https://", index):
            end = index
            while end < length and text[end] not in _WHITESPACE:
                end += 1
            url = text[index:end]
            if url not in seen:
                seen.add(url)
                found.append(url)
            index = end
        else:
            index += 1
    return found


# --- the generator vs. an independent reading of the real file -----------------------


def test_the_generator_agrees_with_an_independent_extraction_of_the_real_sources_md():
    text = _sources_text()

    assert sub.extract_source_urls(text) == _independent_urls(text), (
        "the generator's extraction of the real sources.md differs from a plain "
        "whitespace-terminated scan of the same file. One of the extra rules (the "
        "structural-delimiter set or the trailing-punctuation strip) is now firing on a "
        "real URL, so the block may carry a URL STRING that differs from the one the agent "
        "will try to fetch -- which the drift test cannot see, because both carriers would "
        "still match the generator."
    )


def test_every_url_rendered_into_the_block_came_from_sources_md():
    """The reverse direction of `test_every_extracted_url_appears_exactly_once...`:
    nothing in the block is a URL the skill's source list does not contain."""
    text = _sources_text()
    block = sub.render_source_url_block(text)
    expected = set(sub.extract_source_urls(text))

    rendered = {line for line in block.splitlines() if line.startswith(("http://", "https://"))}

    assert rendered == expected, {
        "in the block but not in sources.md": sorted(rendered - expected),
        "in sources.md but not on their own line in the block": sorted(expected - rendered),
    }


def test_no_block_line_mixes_a_url_with_anything_else():
    """A URL sharing its line with a label, a separator or prose is a URL the agent can
    copy wrong -- and re-emitting the outlet labels is what D4 explicitly rules out. So
    every line that mentions a URL at all must BE one of the extracted URLs, exactly."""
    text = _sources_text()
    block = sub.render_source_url_block(text)
    expected = set(sub.extract_source_urls(text))

    offenders = [
        line
        for line in block.splitlines()
        if ("http://" in line or "https://" in line) and line not in expected
    ]

    assert not offenders, offenders


# --- forward-looking hazard guards on the real sources.md ----------------------------


def test_no_extracted_url_hazard_from_a_parenthesis_inside_a_url_path():
    """D4 stops a match at `)` so a markdown `[label](url)` cannot swallow its closing
    delimiter -- the cost is that a URL whose own path contains `(...)` (a Wikipedia
    `..._(model)` style link) is truncated at the `(`. An unbalanced `(` in an extracted
    URL is the fingerprint of that truncation."""
    for url in sub.extract_source_urls(_sources_text()):
        assert url.count("(") == url.count(")") == 0, (
            f"{url!r} carries a parenthesis. Extraction stops at `)`, so a URL whose path "
            "contains parentheses is TRUNCATED and the block would hand the agent a "
            "different string than the real URL. Verify this URL by hand before shipping it."
        )


def test_no_extracted_url_hazard_from_a_middle_dot_glued_to_a_url():
    """`·` is `sources.md`'s own feed/site separator and is stripped only from the END of
    a match, so `https://x.example/blog/·feed:` would extract as `...blog/·feed`. Every
    real line spaces the separator today; this fails if one stops doing so."""
    for url in sub.extract_source_urls(_sources_text()):
        assert "·" not in url, (
            f"{url!r} contains the `·` separator, so it is two fields glued into one match. "
            "Put whitespace around the `·` in sources.md and regenerate both carriers."
        )


def test_no_url_hides_on_a_heading_line_in_the_real_sources_md():
    """A `## ` line is consumed as a group heading and its URLs are NOT scanned, so a URL
    written into a tier heading is silently dropped from the block -- no test failure, no
    fetchable URL. Guard the real file rather than change the documented rule."""
    offenders = [
        line
        for line in _sources_text().splitlines()
        if line.startswith("## ") and ("http://" in line or "https://" in line)
    ]

    assert not offenders, (
        f"sources.md heading line(s) contain a URL that the generator drops: {offenders}. "
        "Move the URL onto its own bullet under the heading."
    )


def test_a_url_on_a_heading_line_is_dropped_today_which_is_why_the_guard_above_exists():
    """Documents the current behaviour explicitly, so the guard above is understood as a
    guard and not mistaken for redundancy."""
    assert sub.extract_source_urls("## Tier X — see https://heading.example/\n- https://body.example/\n") == [
        "https://body.example/"
    ]


# --- determinism across processes ----------------------------------------------------


def test_the_block_is_byte_identical_across_processes_with_different_hash_seeds():
    """`render_source_url_block()` uses a set for de-duplication; the in-process
    determinism test cannot see a set-iteration dependency because PYTHONHASHSEED is
    fixed within a process. Two subprocesses with different seeds can."""
    outputs = []
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, str(sub.__file__)],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        outputs.append(result.stdout)

    assert len(set(outputs)) == 1
    assert outputs[0].rstrip("\n") == sub.render_source_url_block(_sources_text())


# --- the block must not leak onto a surface the drift scan does not cover ------------


def test_the_skill_itself_never_carries_the_block():
    """The block is DERIVED from the skill. A copy inside the skill would be a cycle --
    and the whole point of ADR-0018 is that a URL reaching the agent through the skill's
    files does not satisfy the platform rule anyway."""
    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert sub.BLOCK_BEGIN_MARKER not in text, path
        assert sub.BLOCK_END_MARKER not in text, path


def test_no_system_prompt_surface_carries_the_block():
    """D7 rule 2's marker scan covers `deployment*.json` and candidate `task-prompt.md`
    only. A block pasted into a SYSTEM prompt would be both unscanned (free to rot) and
    useless (the platform does not count the system prompt as prior context -- ADR-0018
    alternative (c)), so assert it is absent from every system-prompt surface."""
    surfaces = [MANAGED_AGENT_DIR / "agent.json", *sorted(CANDIDATES_DIR.glob("*/system-prompt.md"))]
    assert len(surfaces) >= 5, surfaces

    for path in surfaces:
        text = path.read_text(encoding="utf-8")
        assert sub.BLOCK_BEGIN_MARKER not in text, (
            f"{path} carries the source-URL block. The system prompt does NOT satisfy the "
            "platform's prior-context rule, and this surface is not covered by the drift "
            "scan -- the block belongs in the first USER message only."
        )
        assert sub.BLOCK_END_MARKER not in text, path
