"""Proves FR-12(a)/AC-12's "no repo rollback" claim FOR REAL, using a real, throwaway
git repository -- not a mock, not an assertion in a docstring.

This test shells out to a real `git` binary against a TEMPORARY directory it creates
and `git init`s itself. It never touches this repo's own git history, HEAD, or
working tree -- a completely isolated sandbox, cleaned up automatically by pytest's
`tmp_path` fixture.

What it demonstrates: commit a candidate file, modify it, commit again, then read the
FIRST commit's version of the file via `git show <hash>:<path>` and confirm (a) it
returns the OLD content, (b) the working tree still shows the NEW content throughout,
and (c) no `git checkout`/`git reset`/`git switch` of any kind was needed to read the
historical version -- exactly the mechanism Decision 2c and PRD FR-12 rely on.
"""

from __future__ import annotations

import subprocess


def _git(*args: str, cwd) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result


def test_git_show_reads_historical_content_without_checkout_or_reset(tmp_path):
    repo_dir = tmp_path / "throwaway-candidate-repo"
    repo_dir.mkdir()

    _git("init", "--initial-branch=main", cwd=repo_dir)
    _git("config", "user.email", "test@example.com", cwd=repo_dir)
    _git("config", "user.name", "Test Runner", cwd=repo_dir)

    system_prompt_path = repo_dir / "system-prompt.md"
    system_prompt_path.write_text("OLD PROMPT -- version 1\n")
    _git("add", "system-prompt.md", cwd=repo_dir)
    _git("commit", "-m", "feat(candidates): add example-candidate v1 system prompt", cwd=repo_dir)
    first_commit_hash = _git("rev-parse", "HEAD", cwd=repo_dir).stdout.strip()

    system_prompt_path.write_text("NEW PROMPT -- version 2, a completely different prompt\n")
    _git("add", "system-prompt.md", cwd=repo_dir)
    _git("commit", "-m", "feat(candidates): update example-candidate system prompt to v2", cwd=repo_dir)
    second_commit_hash = _git("rev-parse", "HEAD", cwd=repo_dir).stdout.strip()

    assert first_commit_hash != second_commit_hash

    # THE key call: read the FIRST commit's version of the file via `git show`.
    historical_content = _git("show", f"{first_commit_hash}:system-prompt.md", cwd=repo_dir).stdout

    assert historical_content == "OLD PROMPT -- version 1\n"
    # The working tree is UNCHANGED throughout -- still shows the latest (v2) content,
    # proving `git show` never touched HEAD or the working tree.
    assert system_prompt_path.read_text() == "NEW PROMPT -- version 2, a completely different prompt\n"

    # Confirm HEAD itself never moved as a side effect of the `git show` call.
    head_after = _git("rev-parse", "HEAD", cwd=repo_dir).stdout.strip()
    assert head_after == second_commit_hash

    # Confirm the working tree is clean (no dirty/staged changes) -- `git show` is
    # read-only and left nothing behind, unlike a `git checkout <hash> -- <path>`
    # would (which stages a change against HEAD).
    status = _git("status", "--porcelain", cwd=repo_dir).stdout
    assert status == ""


def test_git_show_works_against_an_annotated_tag_too(tmp_path):
    """Decision 2c also allows referring to history via a ref name, not only a raw
    commit hash -- confirm `git show <tag>:<path>` behaves identically."""
    repo_dir = tmp_path / "throwaway-candidate-repo-tag"
    repo_dir.mkdir()

    _git("init", "--initial-branch=main", cwd=repo_dir)
    _git("config", "user.email", "test@example.com", cwd=repo_dir)
    _git("config", "user.name", "Test Runner", cwd=repo_dir)

    model_path = repo_dir / "model.txt"
    model_path.write_text("claude-example-model-v1\n")
    _git("add", "model.txt", cwd=repo_dir)
    _git("commit", "-m", "feat(candidates): pin example-candidate to v1 model", cwd=repo_dir)
    _git("tag", "-a", "example-sync-1", "-m", "first sync of example-candidate", cwd=repo_dir)

    model_path.write_text("claude-example-model-v2\n")
    _git("add", "model.txt", cwd=repo_dir)
    _git("commit", "-m", "feat(candidates): bump example-candidate to v2 model", cwd=repo_dir)

    historical_content = _git("show", "example-sync-1:model.txt", cwd=repo_dir).stdout
    assert historical_content == "claude-example-model-v1\n"
    assert model_path.read_text() == "claude-example-model-v2\n"
