"""Tests for harness/git_show.py -- deliberately run against the REAL repo's real
committed history (read-only `git show`, no checkout/mutation, no network) rather
than a synthetic git repo fixture, mirroring test_run_store.py's
`test_current_git_ref_returns_a_real_commit_sha`'s own "safe to run for real"
reasoning. `deploy/candidates/production-baseline/` and
`deploy/candidates/multiagent-aggressive-haiku/` are real, already-committed
candidates this repo's own history contains.
"""

from __future__ import annotations

import subprocess

from harness import git_show


def _current_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(git_show.repo_root()), capture_output=True, text=True, check=True
    ).stdout.strip()


def test_show_file_at_ref_reads_a_real_committed_files_content():
    content = git_show.show_file_at_ref(_current_head(), "deploy/candidates/production-baseline/model.txt")
    assert content is not None
    assert content.strip() == "claude-sonnet-5"


def test_show_file_at_ref_returns_none_for_a_path_that_never_existed():
    content = git_show.show_file_at_ref(_current_head(), "deploy/candidates/this-path-does-not-exist/model.txt")
    assert content is None


def test_read_candidate_declaration_at_ref_for_a_single_agent_candidate():
    declaration = git_show.read_candidate_declaration_at_ref(_current_head(), "production-baseline")

    assert declaration["slug"] == "production-baseline"
    assert declaration["model"] == "claude-sonnet-5"
    assert declaration["is_multi_agent"] is False
    assert declaration["sub_agents"] == []
    assert declaration["system_prompt"]  # non-empty


def test_read_candidate_declaration_at_ref_for_a_multi_agent_candidate():
    declaration = git_show.read_candidate_declaration_at_ref(_current_head(), "multiagent-aggressive-haiku")

    assert declaration["is_multi_agent"] is True
    assert len(declaration["sub_agents"]) == 4
    names = {sa["name"] for sa in declaration["sub_agents"]}
    assert any("research" in n for n in names)
    assert any("selection" in n for n in names)
    assert any("writing" in n for n in names)
    assert any("listening-script" in n for n in names)
    for sub_agent in declaration["sub_agents"]:
        assert sub_agent["system_prompt"]  # non-empty for every sub-agent


def test_read_candidate_declaration_at_ref_for_a_nonexistent_slug_degrades_gracefully():
    declaration = git_show.read_candidate_declaration_at_ref(_current_head(), "no-such-candidate")

    assert declaration["model"] == ""
    assert declaration["system_prompt"] == ""
    assert declaration["sub_agents"] == []
