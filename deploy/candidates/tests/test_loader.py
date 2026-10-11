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
    # The synthetic fixtures carry `{}` -- matching every real candidate in this repo,
    # and the state `to_agent_body()` must keep rendering as a bare-string `model`.
    # (They USED to carry a made-up `{"effort": "example-low", "thinking_budget_tokens": 1}`,
    # which was harmless while `parameters.json` was declaration-only, but is now a
    # genuine load-time error -- see test_parameters_validation_* below.)
    assert candidate.agent.parameters == {}
    assert candidate.agent.effort is None
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
    assert "EXAMPLE TASK PROMPT" in candidate.agent.task_prompt
    assert candidate.agent.tools == [{"type": "agent_toolset_20260401"}]
    assert candidate.agent.parameters == {}


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


# --- parameters.json: the `effort` key, validated AT LOAD ---------------------------
# Added 2026-10-11 (model-generation comparison epic). Until the Agents API documented
# effort on the model object, `parameters.json` was declaration-only and a non-empty
# one would simply have 400'd at sync. Now `effort` is a REAL knob mapped onto
# `model.effort`, so the loader validates it: an unsupported key or a bogus level is a
# load-time error, NOT something discovered (or, worse, silently discarded) at the API.


def _single_agent_copy(tmp_path, *, parameters=None, name="candidate-under-test"):
    """A writable copy of the single-agent fixture, optionally with a given
    `parameters.json` content written into it."""
    import json
    import shutil

    candidate_dir = tmp_path / name
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)
    if parameters is not None:
        (candidate_dir / "parameters.json").write_text(json.dumps(parameters))
    return candidate_dir


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_parameters_json_accepts_every_documented_effort_level(tmp_path, level):
    candidate_dir = _single_agent_copy(tmp_path, parameters={"effort": level}, name=f"effort-{level}")

    candidate = load_candidate(candidate_dir)

    assert candidate.agent.parameters == {"effort": level}
    assert candidate.agent.effort == level


def test_parameters_json_with_an_unsupported_key_raises_candidate_load_error(tmp_path):
    """`thinking_budget_tokens` is the concrete historical example: the July 2026 probe
    confirmed `model.thinking` is rejected and an unknown model-object key is accepted
    and SILENTLY DISCARDED, so a candidate declaring it would read as tuned and run at
    default -- silent experiment corruption. Must fail at load instead."""
    candidate_dir = _single_agent_copy(tmp_path, parameters={"effort": "high", "thinking_budget_tokens": 1024})

    with pytest.raises(CandidateLoadError, match="unsupported parameter key"):
        load_candidate(candidate_dir)


def test_parameters_json_with_an_invalid_effort_level_raises_candidate_load_error(tmp_path):
    candidate_dir = _single_agent_copy(tmp_path, parameters={"effort": "example-low"})

    with pytest.raises(CandidateLoadError, match="not a supported level"):
        load_candidate(candidate_dir)


def test_parameters_json_that_is_not_an_object_raises_candidate_load_error(tmp_path):
    candidate_dir = _single_agent_copy(tmp_path, parameters=["high"])

    with pytest.raises(CandidateLoadError, match="must be a JSON object"):
        load_candidate(candidate_dir)


def test_empty_parameters_json_means_no_effort(tmp_path):
    candidate_dir = _single_agent_copy(tmp_path, parameters={})

    candidate = load_candidate(candidate_dir)

    assert candidate.agent.effort is None


def test_absent_parameters_json_means_no_effort(tmp_path):
    candidate_dir = _single_agent_copy(tmp_path)
    (candidate_dir / "parameters.json").unlink()

    candidate = load_candidate(candidate_dir)

    assert candidate.agent.parameters == {}
    assert candidate.agent.effort is None


def _multi_agent_copy_with_roster_parameters(tmp_path, parameters, *, name="multi-under-test"):
    import json
    import shutil

    candidate_dir = tmp_path / name
    shutil.copytree(FIXTURES_DIR / "example-multi-agent", candidate_dir)
    multiagent_json_path = candidate_dir / "multiagent.json"
    data = json.loads(multiagent_json_path.read_text())
    data["agents"][0]["parameters"] = parameters
    multiagent_json_path.write_text(json.dumps(data))
    return candidate_dir


def test_roster_entry_parameters_accept_a_valid_effort_level(tmp_path):
    """A sub-agent is declared exactly as richly as the coordinator, inline in
    multiagent.json -- so its `parameters` gets the same validation and the same
    `effort` support (a per-sub-agent level is a real lever for the decomposition
    candidates)."""
    candidate_dir = _multi_agent_copy_with_roster_parameters(tmp_path, {"effort": "low"})

    candidate = load_candidate(candidate_dir)

    assert candidate.sub_agents[0].effort == "low"
    assert candidate.agent.effort is None  # the coordinator's own parameters.json is still {}


def test_roster_entry_parameters_with_an_unsupported_key_raises_candidate_load_error(tmp_path):
    candidate_dir = _multi_agent_copy_with_roster_parameters(tmp_path, {"reasoning": "max"})

    with pytest.raises(CandidateLoadError, match="unsupported parameter key"):
        load_candidate(candidate_dir)


def test_roster_entry_parameters_with_an_invalid_effort_level_raises_candidate_load_error(tmp_path):
    candidate_dir = _multi_agent_copy_with_roster_parameters(tmp_path, {"effort": "example-medium"})

    with pytest.raises(CandidateLoadError, match="not a supported level"):
        load_candidate(candidate_dir)


def test_roster_entry_validation_error_names_the_offending_entry(tmp_path):
    """The message must point at WHICH roster entry is broken (index + name), not just
    "a parameters block somewhere" -- a coordinator can carry several sub-agents."""
    candidate_dir = _multi_agent_copy_with_roster_parameters(tmp_path, {"effort": "turbo"})

    with pytest.raises(CandidateLoadError, match=r"roster entry \[0\] \(example-sub-agent-researcher-EXAMPLE\)"):
        load_candidate(candidate_dir)


# --- to_agent_body(): how a declared effort reaches the API ------------------------


def test_to_agent_body_sends_a_bare_string_model_when_no_effort_is_declared(tmp_path):
    """THE invariant for every pre-existing candidate (all carry `{}`): adding effort
    support must not change their request body at all."""
    candidate = load_candidate(_single_agent_copy(tmp_path, parameters={}))

    body = candidate.agent.to_agent_body()

    assert body["model"] == "claude-example-model"
    assert "parameters" not in body


def test_to_agent_body_sends_the_model_object_when_effort_is_declared(tmp_path):
    candidate = load_candidate(_single_agent_copy(tmp_path, parameters={"effort": "xhigh"}))

    body = candidate.agent.to_agent_body()

    assert body["model"] == {"id": "claude-example-model", "effort": "xhigh"}
    # Never a top-level `parameters` field -- the Agents API has no such field
    # (`unknown field "parameters"`, live-probed July 2026).
    assert "parameters" not in body


def test_to_agent_body_only_changes_the_model_field_when_effort_is_declared(tmp_path):
    """Everything else about the body must be identical with and without effort --
    effort is a model-object concern only, not a licence to reshape the request."""
    without = load_candidate(_single_agent_copy(tmp_path, parameters={}, name="without")).agent.to_agent_body()
    with_effort = load_candidate(
        _single_agent_copy(tmp_path, parameters={"effort": "high"}, name="with")
    ).agent.to_agent_body()

    assert set(without) == set(with_effort)
    assert {k: v for k, v in without.items() if k != "model"} == {
        k: v for k, v in with_effort.items() if k != "model"
    }


def test_sub_agent_to_agent_body_sends_the_model_object_when_its_roster_entry_declares_effort(tmp_path):
    candidate = load_candidate(_multi_agent_copy_with_roster_parameters(tmp_path, {"effort": "medium"}))

    sub_agent_body = candidate.sub_agents[0].to_agent_body()
    coordinator_body = candidate.agent.to_agent_body()

    assert sub_agent_body["model"] == {"id": "claude-example-sub-agent-model", "effort": "medium"}
    # The coordinator declares no effort of its own -> still the bare string.
    assert coordinator_body["model"] == "claude-example-coordinator-model"
