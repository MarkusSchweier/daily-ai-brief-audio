#!/usr/bin/env python3
"""Generate the source-URL block that goes into the daily brief's FIRST USER MESSAGE.

Why this exists (docs/adr/0018-source-urls-in-initial-prompt.md, D1): since 2026-10-08
the Managed Agents `web_fetch` tool only retrieves a URL that already appeared in the
conversation -- in a user message, in a client-side tool result, or in an earlier
`web_search`/`web_fetch` result. The 62 curated URLs in the `daily-ai-brief` skill's
`sources.md` reach the agent today only because the skill tells it to open that file
with the built-in file/shell tools, and built-in tool output demonstrably does NOT
satisfy the rule (4 of 5 rejected URLs in the 2026-10-16 production run had appeared
verbatim in an earlier bash/read result and were still rejected with
`url_not_in_prior_context`). So the URLs have to be IN the user message, and this module
is what derives that message fragment from `sources.md`.

`sources.md` stays the single source of truth: it is read, never written, by this tool,
and `deploy/managed-agent/tests/test_source_url_block_drift.py` fails if the block
pasted into a prompt is not byte-equal to this module's current output (D7).

Deliberately NOT in `deploy/managed-agent/pipeline/` (D2): the microVM `Dockerfile` does
`COPY pipeline/ /opt/pipeline/`, a whole-directory copy, so a module placed there would
be baked into the production image and every prompt-tooling tweak would imply an image
rebuild -- the ADR-0008 coupling this repo has already paid for once. This is an
AUTHORING-TIME tool; it never runs inside the microVM.

Enforcement is check-only (D2): a human runs the CLI and pastes the output into the two
prompt carriers. Nothing here edits a prompt, so `deployment.json` stays the literal,
reviewable payload that was POSTed -- the property every rollback recipe in its
`_deployment_history` depends on.

Usage:
    # print the block for the skill's current sources.md
    python3 deploy/managed-agent/tools/source_url_block.py

    # print it as a JSON string literal, for pasting into deployment.json
    python3 deploy/managed-agent/tools/source_url_block.py --json-string
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

__all__ = [
    "BLOCK_BEGIN_MARKER",
    "BLOCK_END_MARKER",
    "DEFAULT_SOURCES_PATH",
    "SourceUrlBlockError",
    "extract_source_urls",
    "render_source_url_block",
]


class SourceUrlBlockError(ValueError):
    """Raised when `sources.md` yields no URLs at all.

    Fail loudly rather than emitting an empty block (D4, "Empty result"): a future
    restructure of `sources.md` that this module's extraction no longer understands must
    stop the operator, not silently ship a prompt that promises a source list and
    carries none.
    """


# --- The block's delimiters (D3) ----------------------------------------------------
#
# Exported as constants so the generator, the drift test and `grep` cannot disagree
# about what a block looks like. `render_source_url_block()` returns the markers AS PART
# OF the block, so there is exactly one artifact to generate, paste and compare.
BLOCK_BEGIN_MARKER = (
    "=== BEGIN GENERATED SOURCE URL LIST "
    "(from the daily-ai-brief skill's sources.md; do not edit by hand) ==="
)
BLOCK_END_MARKER = "=== END GENERATED SOURCE URL LIST ==="

# The skill's own source list -- this repo's copy of the ported skill, which is what the
# live skill version is pushed from. Resolved relative to this file so the CLI works from
# any working directory.
DEFAULT_SOURCES_PATH = (
    Path(__file__).resolve().parent.parent / "skills" / "daily-ai-brief" / "sources.md"
)

# --- Extraction rules (D4) -----------------------------------------------------------
#
# Greedy to the first whitespace or structural delimiter, so a markdown `[label](url)` or
# an `<url>` never swallows its closing delimiter.
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'`)\]]+")
# Stripped from the end of a match. `·` (the middle dot) is `sources.md`'s own
# feed/site separator; the rest is ordinary sentence punctuation.
_TRAILING_PUNCTUATION = ".,;:!?·"
_HEADING_PREFIX = "## "
# Where URLs that appear before the first `## ` heading go (none today).
_UNGROUPED_HEADING = "Ungrouped"

# --- What the block says (D5) --------------------------------------------------------
#
# Four things, and only four: (1) why the URLs are here and that reading `sources.md`
# from disk does not count, (2) that fetching a listed feed/index page unlocks the
# article links inside it, (3) that a `url_not_in_prior_context` rejection must not be
# retried, (4) that the fallback is the skill's own chain.
#
# Two deliberate non-statements. It must NOT tell the agent to fetch everything -- 62
# URLs in the message is an invitation to fetch 62 pages, which is the cost risk and also
# contradicts the skill ("budget the fetches... you don't need every source"); fetch
# budgeting stays with the skill. And the fallback sentence names the skill's existing
# chain (that outlet's own site page, then `web_search`) rather than jumping straight to
# search, so the block cannot contradict a skill this change does not touch.
_PREAMBLE = """\
These are the curated source URLs from the daily-ai-brief skill's sources.md, listed here
verbatim and in the skill's own tier order.

Why they are in this message: web_fetch can only retrieve a URL that already appeared in
this conversation -- in this message, or in an earlier web_search/web_fetch result.
Reading sources.md from disk does NOT count: a curated URL that exists only in that file
cannot be fetched. Listing the URLs here is what makes them fetchable.

How to use this list:
- Fetch from it when the skill calls for a known source. This is NOT an instruction to
  fetch everything -- budget your fetches exactly as the skill says, working the top
  tiers first.
- Fetching a feed or index page listed here also makes the article links inside it
  fetchable, because they then count as an earlier web_fetch result. The feed -> article
  chain therefore works from any entry point below.
- If a fetch is rejected with url_not_in_prior_context, do NOT retry that URL. Fall back
  exactly as the skill's Gather step prescribes: that outlet's own site/index page (it is
  listed next to its feed below, and is fetchable for the same reason), and only then
  web_search.
- Fetch in small batches of at most about 8 URLs at a time. Read what came back before you
  fetch the next batch.
- Keep working without stopping. This run is unattended and ends the moment you reply
  without calling a tool, so never pause to summarize progress or to announce what you
  will do next: go straight on to your next tool call until the final step of this message
  is done."""


def _extract_source_url_groups(sources_md_text: str) -> list[tuple[str, list[str]]]:
    """Group the URLs of `sources_md_text` under their `## ` headings, in document order.

    De-duplicated GLOBALLY with first occurrence winning, so the flattened result is
    exactly `extract_source_urls()`'s output. Groups that contain no URL are dropped --
    a bare heading would be tokens in the prompt with nothing under them.
    """
    groups: list[tuple[str, list[str]]] = []
    current_heading = _UNGROUPED_HEADING
    current_urls: list[str] = []
    seen: set[str] = set()

    def flush() -> None:
        if current_urls:
            groups.append((current_heading, list(current_urls)))

    for line in sources_md_text.splitlines():
        if line.startswith(_HEADING_PREFIX):
            flush()
            # Verbatim minus the markup (D4, "Tier structure"); only trailing whitespace
            # is dropped, never a character of the heading's own text.
            current_heading = line[len(_HEADING_PREFIX) :].rstrip()
            current_urls = []
            continue
        for match in _URL_PATTERN.finditer(line):
            url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
            # No normalization at all (D4): scheme, case and trailing slash stay verbatim,
            # because the platform matches URL STRINGS against prior context -- "tidying"
            # a URL would put a different string in the message than the one the agent
            # then tries to fetch.
            if not url or url in seen:
                continue
            seen.add(url)
            current_urls.append(url)
    flush()

    return groups


def extract_source_urls(sources_md_text: str) -> list[str]:
    """Every `http://`/`https://` URL in `sources_md_text`, in document order.

    De-duplicated, first occurrence wins (a guard, not a transform -- today's
    `sources.md` has no duplicates). Maintainer-facing `(verify)` markers are not
    emitted: they are prose about an unconfirmed feed endpoint, and the block's fallback
    rule already covers an endpoint that turns out to be wrong.

    Returns an empty list for text with no URLs; `render_source_url_block()` is where
    that becomes an error.
    """
    return [url for _heading, urls in _extract_source_url_groups(sources_md_text) for url in urls]


def render_source_url_block(sources_md_text: str) -> str:
    """Render the full block -- both markers included -- for `sources_md_text`.

    Deterministic: the same input always yields the same bytes, which is what makes the
    drift test (D7) a meaningful equality check. The returned string carries no trailing
    newline; whatever separates the block from the prose around it in a given prompt is
    the prompt author's business, since the test compares from the first character of the
    begin marker to the last character of the end marker.

    Raises:
        SourceUrlBlockError: if `sources_md_text` contains no URL at all.
    """
    groups = _extract_source_url_groups(sources_md_text)
    if not groups:
        raise SourceUrlBlockError(
            "no http(s) URLs found in the given sources.md text -- refusing to render an "
            "empty source-URL block. Either the wrong file was passed, or sources.md was "
            "restructured in a way this generator no longer understands "
            "(deploy/managed-agent/tools/source_url_block.py)."
        )

    lines: list[str] = [BLOCK_BEGIN_MARKER, ""]
    lines.extend(_PREAMBLE.splitlines())
    for heading, urls in groups:
        lines.append("")
        lines.append(heading)
        lines.extend(urls)
    lines.append("")
    lines.append(BLOCK_END_MARKER)

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: print the block to stdout. Never writes a prompt file."""
    parser = argparse.ArgumentParser(
        description=(
            "Print the generated source-URL block for the daily-ai-brief skill's "
            "sources.md. Paste it, unchanged, at the end of the scheduled deployment's "
            "agent.initial_prompt (deploy/managed-agent/deployment.json) and at the end "
            "of deploy/candidates/haiku-5-5-source-urls/task-prompt.md. See "
            "docs/adr/0018-source-urls-in-initial-prompt.md."
        )
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=DEFAULT_SOURCES_PATH,
        help=f"path to sources.md (default: {DEFAULT_SOURCES_PATH})",
    )
    parser.add_argument(
        "--json-string",
        action="store_true",
        help=(
            "print the block as a JSON string literal (surrounding double quotes "
            "included, non-ASCII escaped) for pasting into deployment.json -- drop the "
            "outer quotes to append it to the existing initial_prompt string"
        ),
    )
    args = parser.parse_args(argv)

    block = render_source_url_block(args.sources.read_text(encoding="utf-8"))
    print(json.dumps(block, ensure_ascii=True) if args.json_string else block)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main() in the tests
    try:
        sys.exit(main())
    except (SourceUrlBlockError, OSError) as error:
        print(f"source_url_block: error: {error}", file=sys.stderr)
        sys.exit(2)
