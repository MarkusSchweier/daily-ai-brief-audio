"""Tests for candidate_sync.writer -- rewriting a candidate's tracked files with the
sync script's outputs (a newly-minted agent_id, an updated skills.json). Confirms the
writer NEVER runs git commands (an ordinary filesystem edit only, left for the
operator to commit -- see the module's own docstring for why)."""

from __future__ import annotations

import json
import shutil

from candidate_sync.writer import write_candidate_agent_id, write_skills_json, write_sub_agent_ids

from conftest import FIXTURES_DIR


def test_write_candidate_agent_id_preserves_other_fields(tmp_path):
    candidate_dir = tmp_path / "example-single-agent"
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)
    candidate_json_path = candidate_dir / "candidate.json"
    original = json.loads(candidate_json_path.read_text())

    write_candidate_agent_id(candidate_json_path, "agent_NEWLY_CREATED_EXAMPLE")

    updated = json.loads(candidate_json_path.read_text())
    assert updated["agent_id"] == "agent_NEWLY_CREATED_EXAMPLE"
    assert updated["slug"] == original["slug"]
    assert updated["description"] == original["description"]


def test_write_sub_agent_ids_by_roster_index(tmp_path):
    candidate_dir = tmp_path / "example-multi-agent"
    shutil.copytree(FIXTURES_DIR / "example-multi-agent", candidate_dir)
    multiagent_json_path = candidate_dir / "multiagent.json"

    write_sub_agent_ids(multiagent_json_path, {0: "agent_SUB_AGENT_0_EXAMPLE"})

    updated = json.loads(multiagent_json_path.read_text())
    assert updated["agents"][0]["agent_id"] == "agent_SUB_AGENT_0_EXAMPLE"
    # The rest of the roster entry (name, model, etc.) is untouched.
    assert updated["agents"][0]["name"] == "example-sub-agent-researcher-EXAMPLE"
    assert updated["agents"][0]["entry"] == {"type": "custom"}


def test_write_skills_json_overwrites_with_pinned_version(tmp_path):
    skills_json_path = tmp_path / "skills.json"
    skills_json_path.write_text(json.dumps([{"skill_id": "skill_EXAMPLE"}]))

    write_skills_json(skills_json_path, [{"skill_id": "skill_EXAMPLE", "version": 1783096569199829}])

    updated = json.loads(skills_json_path.read_text())
    assert updated == [{"skill_id": "skill_EXAMPLE", "version": 1783096569199829}]


def test_writer_functions_never_shell_out_to_git(tmp_path, monkeypatch):
    """Confirms the "never git commit" contract at the code level, not just by
    reading the docstring: patch subprocess.run/os.system to fail loudly if called,
    then exercise every writer function and confirm none of them touch git."""
    import os
    import subprocess

    def _fail_if_called(*args, **kwargs):
        raise AssertionError(f"writer function unexpectedly shelled out: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    monkeypatch.setattr(subprocess, "call", _fail_if_called)
    monkeypatch.setattr(subprocess, "Popen", _fail_if_called)
    monkeypatch.setattr(os, "system", _fail_if_called)

    candidate_dir = tmp_path / "example-multi-agent"
    shutil.copytree(FIXTURES_DIR / "example-multi-agent", candidate_dir)

    write_candidate_agent_id(candidate_dir / "candidate.json", "agent_EXAMPLE")
    write_sub_agent_ids(candidate_dir / "multiagent.json", {0: "agent_SUB_EXAMPLE"})
    write_skills_json(candidate_dir / "skills.json", [{"skill_id": "skill_EXAMPLE", "version": 1}])

    # If we got here without the monkeypatched functions raising, no writer function
    # shelled out to git (or anything else) at all.
