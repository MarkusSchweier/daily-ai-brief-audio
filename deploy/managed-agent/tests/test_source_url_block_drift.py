"""Drift guard for the generated source-URL block (docs/adr/0018, D7; PRD FR-5/AC-1).

The block duplicates the skill's `sources.md` into two prompt surfaces. That duplication
is only acceptable because this test makes `sources.md` the enforced single source of
truth: edit it without regenerating the prompts and the suite fails loudly here, instead
of production quietly shipping a stale list of URLs with an instruction to fetch them.
**A stale block is worse than no block** -- it puts dead URLs in front of the agent -- so
rule 1 below must never be weakened to "the markers are present".

Three rules, deliberately in one place (D7), next to the generator they all import:

  1. **Required carriers.** `deployment.json`'s decoded `agent.initial_prompt` and
     `deploy/candidates/haiku-5-5-source-urls/task-prompt.md` each carry exactly one
     block, byte-equal to the generator's current output.
  2. **No stale copies.** ANY other scanned prompt surface that contains either marker
     must also carry exactly one byte-current block -- marker-driven rather than an
     allow-list, so a block pasted somewhere new can never rot unnoticed.
  3. **Hardened-candidate guard.** The four hardened candidates must contain NEITHER
     marker: `haiku-5-5-hardened` is the matched eval's control arm, and all four are
     bound by `deploy/candidates/tests/test_model_generation_candidates.py`'s
     byte-identity contract.

Reading the other deploy unit's files by repo-relative path is the established cross-unit
precedent of `deploy/subscribers/tests/test_send_time_consistency.py` (which validates
this same `deployment.json` against the canonical send time).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import source_url_block as sub  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
MANAGED_AGENT_DIR = REPO_ROOT / "deploy" / "managed-agent"
CANDIDATES_DIR = REPO_ROOT / "deploy" / "candidates"

DEPLOYMENT_JSON_PATH = MANAGED_AGENT_DIR / "deployment.json"
SOURCE_URLS_CANDIDATE_TASK_PROMPT = CANDIDATES_DIR / "haiku-5-5-source-urls" / "task-prompt.md"

# The candidates whose prompts are contractually byte-identical to each other (the
# cost-optimization epic's single-variable discipline). The block must stay OUT of them.
HARDENED_CANDIDATE_SLUGS = (
    "haiku-swap-hardened",
    "haiku-5-5-hardened",
    "sonnet-5-5-hardened",
    "opus-5-5-hardened",
)

REGENERATE_HINT = (
    "Regenerate the block and paste it into BOTH carriers: "
    "`python3 deploy/managed-agent/tools/source_url_block.py` (add `--json-string` for "
    "deployment.json). Note that regenerating is not enough for production: deployments are "
    "immutable, so a sources.md edit also needs a NEW scheduled deployment -- see "
    "deploy/managed-agent/README.md."
)


def _current_block() -> str:
    """The generator's output for the skill's `sources.md` as it is committed today."""
    return sub.render_source_url_block(sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8"))


def _extract_block(text: str, *, where: str) -> str:
    """The one block in `text`, from the first character of the begin marker to the last
    character of the end marker (D3). Fails if there is not exactly one well-ordered
    marker pair -- whatever separates the block from the prose around it is the prompt
    author's business, but a duplicated or half-pasted block is not."""
    begin_count = text.count(sub.BLOCK_BEGIN_MARKER)
    end_count = text.count(sub.BLOCK_END_MARKER)
    assert begin_count == 1, f"{where}: expected exactly 1 begin marker, found {begin_count}"
    assert end_count == 1, f"{where}: expected exactly 1 end marker, found {end_count}"

    begin = text.index(sub.BLOCK_BEGIN_MARKER)
    end = text.index(sub.BLOCK_END_MARKER) + len(sub.BLOCK_END_MARKER)
    assert begin < end, f"{where}: the end marker precedes the begin marker"
    return text[begin:end]


def _json_string_surfaces(path: Path) -> list[tuple[str, str]]:
    """Every string value anywhere in a JSON document, labelled by its path.

    Every string is its own surface rather than one concatenated blob, so a block pasted
    into a `_comment` (or into a second prompt field a future deployment shape adds) is
    checked on its own terms and cannot be mistaken for part of the real prompt."""
    surfaces: list[tuple[str, str]] = []

    def walk(node: object, label: str) -> None:
        if isinstance(node, str):
            surfaces.append((label, node))
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{label}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{label}[{index}]")

    walk(json.loads(path.read_text(encoding="utf-8")), path.relative_to(REPO_ROOT).as_posix())
    return surfaces


def _scanned_surfaces() -> list[tuple[str, str]]:
    """Every prompt surface this test knows how to scan: the managed-agent deployment
    payloads and every candidate task prompt."""
    surfaces: list[tuple[str, str]] = []
    for path in sorted(MANAGED_AGENT_DIR.glob("deployment*.json")):
        surfaces.extend(_json_string_surfaces(path))
    for path in sorted(CANDIDATES_DIR.glob("*/task-prompt.md")):
        surfaces.append((path.relative_to(REPO_ROOT).as_posix(), path.read_text(encoding="utf-8")))
    return surfaces


def _deployment_initial_prompt() -> str:
    return json.loads(DEPLOYMENT_JSON_PATH.read_text(encoding="utf-8"))["agent"]["initial_prompt"]


# --- rule 1: required carriers -------------------------------------------------------


def test_the_required_carriers_exist_where_this_test_expects_them():
    """Fails loudly (not silently) if a file moves, rather than letting the equality
    assertions below run against nothing."""
    assert DEPLOYMENT_JSON_PATH.is_file(), DEPLOYMENT_JSON_PATH
    assert SOURCE_URLS_CANDIDATE_TASK_PROMPT.is_file(), SOURCE_URLS_CANDIDATE_TASK_PROMPT
    assert sub.DEFAULT_SOURCES_PATH.is_file(), sub.DEFAULT_SOURCES_PATH


def test_the_scheduled_deployments_initial_prompt_carries_a_byte_current_block():
    prompt = _deployment_initial_prompt()

    assert _extract_block(prompt, where="deployment.json agent.initial_prompt") == _current_block(), (
        "deploy/managed-agent/deployment.json's agent.initial_prompt carries a source-URL block that "
        "is NOT the generator's current output for the skill's sources.md -- production would research "
        f"from a stale URL list. {REGENERATE_HINT}"
    )


def test_the_source_urls_candidates_task_prompt_carries_a_byte_current_block():
    prompt = SOURCE_URLS_CANDIDATE_TASK_PROMPT.read_text(encoding="utf-8")

    assert _extract_block(prompt, where=str(SOURCE_URLS_CANDIDATE_TASK_PROMPT)) == _current_block(), (
        "deploy/candidates/haiku-5-5-source-urls/task-prompt.md carries a source-URL block that is NOT "
        "the generator's current output -- the eval arm would measure a different prompt than the one "
        f"production runs. {REGENERATE_HINT}"
    )


def test_both_required_carriers_carry_the_identical_block():
    """The point of the eval arm: the candidate must run the SAME block production does,
    byte for byte. (Implied by the two equalities above; asserted directly so a failure
    says which property broke.)"""
    deployment_block = _extract_block(_deployment_initial_prompt(), where="deployment.json")
    candidate_block = _extract_block(
        SOURCE_URLS_CANDIDATE_TASK_PROMPT.read_text(encoding="utf-8"),
        where=str(SOURCE_URLS_CANDIDATE_TASK_PROMPT),
    )

    assert deployment_block == candidate_block


def test_the_block_is_appended_at_the_end_of_both_required_carriers():
    """D3: appended after the final numbered step, so every existing byte of both prompts
    is untouched and the eval stays single-variable. Anything after the end marker but
    whitespace would mean the block was inserted, not appended."""
    for where, text in (
        ("deployment.json agent.initial_prompt", _deployment_initial_prompt()),
        (str(SOURCE_URLS_CANDIDATE_TASK_PROMPT), SOURCE_URLS_CANDIDATE_TASK_PROMPT.read_text(encoding="utf-8")),
    ):
        tail = text[text.index(sub.BLOCK_END_MARKER) + len(sub.BLOCK_END_MARKER) :]
        assert tail.strip() == "", f"{where}: content follows the block's end marker: {tail[:80]!r}"
        head = text[: text.index(sub.BLOCK_BEGIN_MARKER)]
        assert head.endswith("\n\n"), (
            f"{where}: the begin marker must follow a blank line, not be glued to the prompt's "
            f"last sentence: {head[-60:]!r}"
        )


# --- rule 2: no stale copies anywhere else -------------------------------------------


def test_every_scanned_surface_that_carries_a_marker_carries_a_byte_current_block():
    """Marker-driven, so a block pasted into a surface nobody listed here (a second
    deployment payload, another candidate's prompt, a JSON `_comment`) is held to the
    same currency rule instead of rotting unnoticed."""
    block = _current_block()
    stale = []
    checked = []

    for label, text in _scanned_surfaces():
        if sub.BLOCK_BEGIN_MARKER not in text and sub.BLOCK_END_MARKER not in text:
            continue
        checked.append(label)
        if _extract_block(text, where=label) != block:
            stale.append(label)

    assert not stale, (
        "prompt surfaces carrying a source-URL block that is not the generator's current output: "
        f"{', '.join(stale)}. {REGENERATE_HINT}"
    )
    # The two required carriers must be among the surfaces actually scanned -- otherwise
    # this test could pass by scanning nothing (a moved file, a changed glob).
    assert "deploy/managed-agent/deployment.json.agent.initial_prompt" in checked, checked
    assert "deploy/candidates/haiku-5-5-source-urls/task-prompt.md" in checked, checked


def test_the_surface_scan_actually_finds_the_prompt_files_it_claims_to_scan():
    labels = [label for label, _text in _scanned_surfaces()]

    assert any(label.startswith("deploy/managed-agent/deployment.json") for label in labels)
    assert any(label.startswith("deploy/managed-agent/deployment-validation.json") for label in labels)
    # Every real candidate has a task-prompt.md; the count is a floor, not a fixed number.
    assert sum(label.endswith("/task-prompt.md") for label in labels) >= 10, labels


# --- rule 3: the hardened candidates must stay block-free ----------------------------


@pytest.mark.parametrize("slug", HARDENED_CANDIDATE_SLUGS)
def test_the_hardened_candidates_carry_no_block(slug):
    """`haiku-5-5-hardened` is the control arm the source-URL candidate is measured
    against, and all four are bound to be byte-identical to one another. A block in any
    of them would silently destroy both guarantees -- so this negative assertion is as
    load-bearing as the positive ones above."""
    task_prompt_path = CANDIDATES_DIR / slug / "task-prompt.md"
    assert task_prompt_path.is_file(), task_prompt_path

    task_prompt = task_prompt_path.read_text(encoding="utf-8")

    assert sub.BLOCK_BEGIN_MARKER not in task_prompt, (
        f"{slug}/task-prompt.md carries a source-URL block. The hardened four must stay block-free "
        "(ADR-0018 D6): the block goes in a NEW candidate, never in the control arm."
    )
    assert sub.BLOCK_END_MARKER not in task_prompt
