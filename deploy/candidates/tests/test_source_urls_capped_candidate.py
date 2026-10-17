"""Tests for the `haiku-5-5-source-urls-capped` candidate -- the 2026-10-17 cost
mitigation arm of the `web_fetch` restoration (ADR-0018 D8).

`haiku-5-5-source-urls` restored first-party source reading (first eval repetition: 27
successful fetches, 19 of them curated sources, 0 rejections) and nearly doubled the cost
per run ($1.35 vs $0.71 Platform list cost), because a large RSS feed lands in context in
full -- ~100K characters, ~25K tokens, for a single feed. This candidate applies D8's
named mitigation, the `web_fetch` toolset setting `max_content_tokens: 8000`.

It only measures that mitigation if NOTHING ELSE differs from the arm it is mitigating.
So the single-variable discipline here is one level tighter than its sibling's: the
comparison is against `haiku-5-5-source-urls` (not the hardened baseline), every declared
file must be byte-identical to it, and `agent.json`'s only behavioural difference must be
the one config entry -- asserted structurally below, not by eyeball.

Division of labour, unchanged: ADR-0018 D7's drift rules (every prompt surface carrying a
marker carries a byte-current block) live in
`deploy/managed-agent/tests/test_source_url_block_drift.py`, next to the generator. How
`sync.py` compares the new config entry against the live echo is
`test_sync_toolset_configs.py`.
"""

from __future__ import annotations

import json
import sys

import pytest

from candidate_sync.loader import load_candidate

from conftest import CANDIDATES_DIR

REPO_ROOT = CANDIDATES_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "deploy" / "managed-agent" / "tools"))

import source_url_block as sub  # noqa: E402

SLUG = "haiku-5-5-source-urls-capped"
SIBLING_SLUG = "haiku-5-5-source-urls"

CANDIDATE_DIR = CANDIDATES_DIR / SLUG
SIBLING_DIR = CANDIDATES_DIR / SIBLING_SLUG

# Every declared file except the two that legitimately differ: `agent.json` (the one
# config entry, plus the agent's own name/description -- it is its own agent resource)
# and `candidate.json` (slug/description/agent_id, i.e. metadata about the candidate
# rather than its behaviour).
IDENTICAL_FILES = ("model.txt", "parameters.json", "system-prompt.md", "skills.json", "task-prompt.md")

# The mitigation, exactly: the one setting, on the one tool, at the one value.
EXPECTED_WEB_FETCH_CONFIG = {"name": "web_fetch", "max_content_tokens": 8000}


def _agent_json(directory):
    return json.loads((directory / "agent.json").read_text(encoding="utf-8"))


def test_the_candidate_and_the_arm_it_mitigates_both_exist():
    """Fails loudly if either directory moves, rather than letting the comparisons below
    silently run against nothing."""
    assert (CANDIDATE_DIR / "candidate.json").is_file(), CANDIDATE_DIR
    assert (SIBLING_DIR / "candidate.json").is_file(), SIBLING_DIR


@pytest.mark.parametrize("filename", IDENTICAL_FILES)
def test_every_file_except_agent_and_candidate_json_is_byte_identical_to_the_sibling(filename):
    assert (CANDIDATE_DIR / filename).read_bytes() == (SIBLING_DIR / filename).read_bytes(), (
        f"{SLUG}/{filename} has drifted from {SIBLING_SLUG}/{filename}. This candidate may differ from "
        "it in agent.json's web_fetch config ONLY -- any other difference stops the batch from "
        "attributing its result to max_content_tokens."
    )


def test_the_candidate_directory_contains_no_unexpected_files():
    """A `multiagent.json`, a candidate-owned `skill/` dir or a stray tunable file would
    change what gets synced and run without any of the comparisons above noticing."""
    expected = {"candidate.json", "agent.json", *IDENTICAL_FILES}

    assert {p.name for p in CANDIDATE_DIR.iterdir() if p.name != "__pycache__"} == expected


def test_the_task_prompt_still_carries_exactly_one_source_url_block():
    """Implied by the byte-identity above, asserted directly so a failure says which
    property broke: this arm must run the SAME block its sibling (and production's
    `initial_prompt`) carries. Its CURRENCY against the skill's `sources.md` is the
    managed-agent drift test's rule 2, which scans every candidate task prompt."""
    prompt = (CANDIDATE_DIR / "task-prompt.md").read_text(encoding="utf-8")

    assert prompt.count(sub.BLOCK_BEGIN_MARKER) == 1
    assert prompt.count(sub.BLOCK_END_MARKER) == 1


def test_the_only_agent_json_difference_is_the_web_fetch_config_entry():
    """The core assertion. Structural rather than textual: strip the new `configs` entry
    back to the sibling's empty list and the two `tools` fields must be identical, with
    `name`/`description` the only other keys allowed to differ."""
    candidate = _agent_json(CANDIDATE_DIR)
    sibling = _agent_json(SIBLING_DIR)

    assert candidate.keys() == sibling.keys()
    assert candidate["mcp_servers"] == sibling["mcp_servers"]
    assert sibling["tools"][0]["configs"] == [], "the arm being mitigated must keep an empty configs list"
    assert candidate["tools"][0]["configs"] == [EXPECTED_WEB_FETCH_CONFIG]

    without_configs = json.loads(json.dumps(candidate))
    without_configs["tools"][0]["configs"] = []
    assert without_configs["tools"] == sibling["tools"], (
        "the two arms' toolsets differ in more than the web_fetch config entry -- e.g. a changed "
        "default_config or toolset type, which would make this a comparison of something else."
    )

    assert candidate["name"] == f"daily-ai-brief-cost-{SLUG}-candidate"
    assert candidate["name"] != sibling["name"]
    assert candidate["description"] != sibling["description"]


def test_the_candidate_loads_with_the_production_model_and_effort():
    candidate = load_candidate(CANDIDATE_DIR)
    sibling = load_candidate(SIBLING_DIR)

    assert candidate.slug == SLUG
    assert candidate.agent.model == sibling.agent.model == "claude-haiku-5-5"
    assert candidate.agent.effort == sibling.agent.effort == "high"
    assert candidate.agent.parameters == {"effort": "high"}
    assert not candidate.is_multi_agent
    assert candidate.skill_source_dir is None  # references the shared daily-ai-brief skill
    assert candidate.agent.skills == sibling.agent.skills  # same pinned skill version
    assert candidate.agent.task_prompt == sibling.agent.task_prompt


def test_the_request_body_actually_carries_the_cap():
    """A setting that sits inert in a file nothing sends is the failure mode worth
    testing for: this candidate exists to make `max_content_tokens` reach the API."""
    body = load_candidate(CANDIDATE_DIR).agent.to_agent_body()

    assert body["tools"][0]["configs"] == [EXPECTED_WEB_FETCH_CONFIG]
    assert body["model"] == {"id": "claude-haiku-5-5", "effort": "high"}
    assert "parameters" not in body  # the cap travels on the toolset, effort on the model


def test_the_request_body_is_the_siblings_body_plus_the_cap():
    """Byte arithmetic on the two request bodies: the only difference the API will see
    (beyond the agent's own name/description) is the config entry."""
    candidate_body = load_candidate(CANDIDATE_DIR).agent.to_agent_body()
    sibling_body = load_candidate(SIBLING_DIR).agent.to_agent_body()

    candidate_body["tools"][0]["configs"] = []
    for key in ("name", "description"):
        candidate_body.pop(key)
        sibling_body.pop(key)

    assert candidate_body == sibling_body


def test_no_other_candidates_request_body_gained_a_toolset_config():
    """The invariant for the other candidates: a declaration with `configs: []` must
    produce exactly the request body it produced before this change, or its next
    "unchanged declaration" re-sync would issue a spurious update and bump its live
    Platform version for nothing (the FR-12/AC-12 no-op guarantee)."""
    checked = 0
    for candidate_dir in sorted(p for p in CANDIDATES_DIR.iterdir() if (p / "candidate.json").is_file()):
        candidate = load_candidate(candidate_dir)
        if candidate.slug == SLUG:
            continue
        for agent in [candidate.agent, *candidate.sub_agents]:
            for entry in agent.to_agent_body()["tools"]:
                assert not entry.get("configs"), f"{candidate.slug} now declares a toolset config: {entry}"
            checked += 1

    # Sanity: 11 candidates, 24 agent bodies including sub-agents, at the time of writing.
    assert checked >= 24


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
    """Production carries neither the block nor the cap yet; applying the cap there would
    additionally cost a new agent version (D8), which is an owner-gated decision. The
    harness's marker (`deploy/eval-harness/harness/production_config.py`) is the one place
    that records what production runs."""
    raw = json.loads((CANDIDATE_DIR / "candidate.json").read_text(encoding="utf-8"))

    assert "is_production_config" not in raw
