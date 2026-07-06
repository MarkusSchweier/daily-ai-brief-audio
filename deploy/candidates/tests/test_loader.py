"""Tests for candidate_sync.loader -- reading a candidate directory's tracked files
into a structured, comparable declaration. No HTTP involved."""

from __future__ import annotations

import pytest

from candidate_sync.loader import CandidateLoadError, load_candidate

from conftest import FIXTURES_DIR


def test_load_single_agent_fixture_reads_all_dimensions():
    candidate = load_candidate(FIXTURES_DIR / "example-single-agent")

    assert candidate.slug == "example-single-agent"
    assert candidate.agent.agent_id is None  # never synced yet
    assert candidate.agent.model == "claude-example-model"
    assert "EXAMPLE SYSTEM PROMPT" in candidate.agent.system_prompt
    assert "EXAMPLE TASK PROMPT" in candidate.agent.task_prompt
    assert candidate.agent.parameters == {"effort": "example-low", "thinking_budget_tokens": 1}
    assert candidate.agent.tools == [{"type": "agent_toolset_20260401"}]
    assert not candidate.is_multi_agent
    assert candidate.sub_agents == []
    assert candidate.skill_source_dir is None  # no skill/ subdirectory in this fixture


def test_load_multi_agent_fixture_reads_coordinator_and_sub_agents():
    candidate = load_candidate(FIXTURES_DIR / "example-multi-agent")

    assert candidate.is_multi_agent
    assert candidate.agent.model == "claude-example-coordinator-model"
    assert len(candidate.sub_agents) == 1
    sub_agent = candidate.sub_agents[0]
    assert sub_agent.name == "example-sub-agent-researcher-EXAMPLE"
    assert sub_agent.model == "claude-example-sub-agent-model"
    assert sub_agent.agent_id is None
    assert candidate.skill_source_dir is not None
    assert (candidate.skill_source_dir / "SKILL.md").exists()


def test_independently_diffable_dimensions_are_separate_files(tmp_path):
    """FR-9/AC-9: changing ONLY the model file must not require touching any other
    file, and the loader must reflect that isolated change."""
    import shutil

    candidate_dir = tmp_path / "example-single-agent"
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)

    (candidate_dir / "model.txt").write_text("claude-example-model-v2\n")

    candidate = load_candidate(candidate_dir)
    assert candidate.agent.model == "claude-example-model-v2"
    # Every other dimension is untouched.
    assert "EXAMPLE SYSTEM PROMPT" in candidate.agent.system_prompt
    assert candidate.agent.parameters == {"effort": "example-low", "thinking_budget_tokens": 1}


def test_missing_required_file_raises_candidate_load_error(tmp_path):
    import shutil

    candidate_dir = tmp_path / "broken-candidate"
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)
    (candidate_dir / "candidate.json").unlink()

    with pytest.raises(CandidateLoadError, match="candidate.json"):
        load_candidate(candidate_dir)


def test_malformed_json_raises_candidate_load_error(tmp_path):
    import shutil

    candidate_dir = tmp_path / "broken-candidate"
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)
    (candidate_dir / "agent.json").write_text("{ not valid json")

    with pytest.raises(CandidateLoadError, match="not valid JSON"):
        load_candidate(candidate_dir)


def test_empty_model_file_raises_candidate_load_error(tmp_path):
    import shutil

    candidate_dir = tmp_path / "broken-candidate"
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)
    (candidate_dir / "model.txt").write_text("")

    with pytest.raises(CandidateLoadError, match="model.txt"):
        load_candidate(candidate_dir)


def test_load_candidate_with_existing_agent_id(tmp_path):
    """Once a candidate has been synced, candidate.json carries a stable agent_id --
    the loader must surface it (this is how the sync script distinguishes first-sync
    from update-in-place)."""
    import json
    import shutil

    candidate_dir = tmp_path / "already-synced"
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)
    candidate_json_path = candidate_dir / "candidate.json"
    data = json.loads(candidate_json_path.read_text())
    data["agent_id"] = "agent_ALREADY_SYNCED_EXAMPLE"
    candidate_json_path.write_text(json.dumps(data))

    candidate = load_candidate(candidate_dir)
    assert candidate.agent.agent_id == "agent_ALREADY_SYNCED_EXAMPLE"
