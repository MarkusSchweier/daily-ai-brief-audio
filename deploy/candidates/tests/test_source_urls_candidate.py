"""Tests for the `haiku-5-5-source-urls` candidate -- the 2026-10-17 `web_fetch`
restoration eval arm (docs/prd/web-fetch-source-urls.md, docs/adr/0018 D6).

This candidate exists to measure ONE change: the generated curated-source-URL block
appended to the task prompt of the configuration production actually runs. That is only
a measurement if the candidate is otherwise identical to `haiku-5-5-hardened` -- same
model, same effort, same system prompt, same skill pin, same tools -- and if the appended
text is exactly the generator's current output rather than a hand-edited copy that has
drifted from the skill's `sources.md`. Both are asserted here, mechanically.

Note the division of labour with the managed-agent suite: ADR-0018's D7 drift rules (the
deployment prompt and this candidate carry a byte-current block; the four hardened
candidates carry none) live in
`deploy/managed-agent/tests/test_source_url_block_drift.py`, next to the generator. What
is asserted HERE is the candidate-vs-baseline relationship, which is this suite's own
single-variable discipline (see `test_model_generation_candidates.py`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from candidate_sync.loader import load_candidate

from conftest import CANDIDATES_DIR

# The generator is the other deploy unit's authoring-time tool; import it by
# repo-relative path, the established cross-unit precedent of
# deploy/subscribers/tests/test_send_time_consistency.py (which reads deployment.json the
# same way). It has no third-party dependencies.
REPO_ROOT = CANDIDATES_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "deploy" / "managed-agent" / "tools"))

import source_url_block as sub  # noqa: E402

SLUG = "haiku-5-5-source-urls"
BASELINE_SLUG = "haiku-5-5-hardened"

CANDIDATE_DIR = CANDIDATES_DIR / SLUG
BASELINE_DIR = CANDIDATES_DIR / BASELINE_SLUG

# Every file of the declaration except the one that is SUPPOSED to differ.
IDENTICAL_FILES = ("model.txt", "parameters.json", "system-prompt.md", "skills.json")


def _rendered_block() -> str:
    return sub.render_source_url_block(sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8"))


def test_the_candidate_and_its_baseline_both_exist():
    """Fails loudly if either directory moves, rather than letting the comparisons below
    silently run against nothing."""
    assert (CANDIDATE_DIR / "candidate.json").is_file(), CANDIDATE_DIR
    assert (BASELINE_DIR / "candidate.json").is_file(), BASELINE_DIR


@pytest.mark.parametrize("filename", IDENTICAL_FILES)
def test_every_file_except_the_task_prompt_is_byte_identical_to_the_baseline(filename):
    assert (CANDIDATE_DIR / filename).read_bytes() == (BASELINE_DIR / filename).read_bytes(), (
        f"{SLUG}/{filename} has drifted from {BASELINE_SLUG}/{filename}. This candidate may differ "
        "from the baseline in task-prompt.md ONLY -- any other difference stops the matched batch "
        "from attributing its result to the source-URL block."
    )


def test_the_candidate_directory_contains_no_unexpected_files():
    """A `multiagent.json`, a candidate-owned `skill/` dir or a stray tunable file would
    change what gets synced and run without any of the comparisons above noticing."""
    expected = {"candidate.json", "agent.json", "task-prompt.md", *IDENTICAL_FILES}

    assert {p.name for p in CANDIDATE_DIR.iterdir() if p.name != "__pycache__"} == expected


def test_tools_and_mcp_servers_are_identical_to_the_baseline():
    """`agent.json`'s name/description legitimately differ (this is its own agent
    resource), so compare the two behavioral fields rather than the whole file -- the
    same shape `test_model_generation_candidates.py` uses."""
    baseline = json.loads((BASELINE_DIR / "agent.json").read_text(encoding="utf-8"))
    candidate = json.loads((CANDIDATE_DIR / "agent.json").read_text(encoding="utf-8"))

    assert candidate["tools"] == baseline["tools"]
    assert candidate["mcp_servers"] == baseline["mcp_servers"]
    assert candidate["name"] == f"daily-ai-brief-cost-{SLUG}-candidate"
    assert candidate["name"] != baseline["name"]


def test_the_task_prompt_is_the_baseline_prompt_plus_the_rendered_block():
    """The core assertion: an exact prefix match (so not one byte of the production
    wording moved) followed by exactly the generator's current output (so the block is
    not a stale hand-edited copy of the skill's sources.md)."""
    baseline_bytes = (BASELINE_DIR / "task-prompt.md").read_bytes()
    candidate_bytes = (CANDIDATE_DIR / "task-prompt.md").read_bytes()
    block = _rendered_block()

    assert candidate_bytes.startswith(baseline_bytes), (
        f"{SLUG}/task-prompt.md no longer starts with {BASELINE_SLUG}/task-prompt.md byte for byte. "
        "The block is APPENDED (ADR-0018 D3) precisely so the existing prompt stays untouched."
    )

    appended = candidate_bytes[len(baseline_bytes) :].decode("utf-8")
    # Whatever whitespace separates the two is the prompt author's business (D3); the
    # block itself must be byte-exact and the only thing appended.
    assert appended.strip("\n") == block, (
        "the appended text is not byte-equal to source_url_block.render_source_url_block() for the "
        "skill's current sources.md. Regenerate it: "
        "python3 deploy/managed-agent/tools/source_url_block.py"
    )


def test_the_baseline_control_arm_carries_no_block():
    """`haiku-5-5-hardened` is the control arm AND the subject of
    `test_model_generation_candidates.py`'s four-way byte-identity contract. Appending to
    it in place would destroy both."""
    baseline_prompt = (BASELINE_DIR / "task-prompt.md").read_text(encoding="utf-8")

    assert sub.BLOCK_BEGIN_MARKER not in baseline_prompt
    assert sub.BLOCK_END_MARKER not in baseline_prompt


def test_the_candidate_loads_with_the_production_model_and_effort():
    candidate = load_candidate(CANDIDATE_DIR)
    baseline = load_candidate(BASELINE_DIR)

    assert candidate.slug == SLUG
    assert candidate.agent.model == baseline.agent.model == "claude-haiku-5-5"
    assert candidate.agent.effort == baseline.agent.effort == "high"
    assert candidate.agent.parameters == {"effort": "high"}
    assert candidate.agent.to_agent_body()["model"] == {"id": "claude-haiku-5-5", "effort": "high"}
    assert not candidate.is_multi_agent
    assert candidate.skill_source_dir is None  # references the shared daily-ai-brief skill
    assert candidate.agent.skills == baseline.agent.skills  # same pinned skill version


def test_the_candidate_is_declared_but_not_yet_synced():
    """`agent_id` is an explicit null until the first `sync.py` mints a real one (the
    repo convention a MISSING key would hide). The orchestrator syncs this candidate; the
    build step must not."""
    raw = json.loads((CANDIDATE_DIR / "candidate.json").read_text(encoding="utf-8"))

    assert "agent_id" in raw
    assert raw["agent_id"] is None or raw["agent_id"].startswith("agent_")
    assert raw["composition"] == "single-agent"
    assert "never scheduled" in raw["schedule_intent"]


def test_the_candidate_is_not_marked_as_the_production_configuration():
    """D6: `candidate.json` must not claim to be the production config. Production
    carries this prompt only after the owner-gated cut-over, and the harness's marker
    (`deploy/eval-harness/harness/production_config.py`) is what records that -- in one
    place, not duplicated here."""
    raw = json.loads((CANDIDATE_DIR / "candidate.json").read_text(encoding="utf-8"))

    assert "is_production_config" not in raw
