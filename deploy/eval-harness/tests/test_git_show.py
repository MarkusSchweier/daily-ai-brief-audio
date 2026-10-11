"""Tests for harness/git_show.py.

Two halves, deliberately:

1. **Against the REAL repo's real committed history** (read-only `git show`, no
   checkout/mutation, no network) rather than a synthetic git repo fixture,
   mirroring test_run_store.py's `test_current_git_ref_returns_a_real_commit_sha`'s
   own "safe to run for real" reasoning. `deploy/candidates/production-baseline/`
   and `deploy/candidates/multiagent-aggressive-haiku/` are real,
   already-committed candidates this repo's own history contains.
2. **Against a FAKE subprocess** (`_FakeGit` below) for the executable-probe and
   fail-soft paths (reviewer H2, 2026-10-11) -- "the PATH's git is the
   Xcode-license-blocked macOS shim", "a git that exits 0 but prints prose", "a
   git that can't resolve this repo", and "no git at all" cannot be produced on a
   working developer machine, so they are simulated at the `subprocess.run`
   boundary.
"""

from __future__ import annotations

import subprocess

import pytest
from harness import git_show


@pytest.fixture(autouse=True)
def _reset_git_probe_cache():
    """`git_show` caches its executable probe for the process's lifetime (both the
    success and the failure), so every test here starts and ends with a clean
    cache -- otherwise a faked "no git anywhere" test would poison the real-repo
    tests that follow it (and vice versa)."""
    git_show.reset_git_cache()
    yield
    git_show.reset_git_cache()


def _current_head() -> str:
    return subprocess.run(
        [git_show.git_executable(), "rev-parse", "HEAD"],
        cwd=str(git_show.repo_root()),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# --- Real repo, real history --------------------------------------------------------


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
    assert declaration["read_error"] is None  # a readable declaration reports no problem


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
    # Nothing readable at all -> a displayable reason, never an exception.
    assert declaration["read_error"]
    assert "returned nothing" in declaration["read_error"]


def test_real_git_resolution_finds_a_working_executable_and_this_repos_root():
    executable = git_show.git_executable()
    assert git_show.git_is_available() is True
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, check=True)
    assert version.stdout.startswith("git version")
    assert (git_show.repo_root() / "deploy" / "eval-harness").is_dir()


# --- Faked git binaries: the executable probe (reviewer H2) --------------------------

_XCODE_LICENSE_MESSAGE = (
    "You have not agreed to the Xcode license agreements. Please run "
    "'sudo xcodebuild -license' from within a Terminal window to review and agree to "
    "the Xcode and Apple SDKs license."
)

WORKING = "working"
XCODE_SHIM = "xcode-shim"  # prints the license notice, non-zero exit
XCODE_SHIM_EXIT_ZERO = "xcode-shim-exit-zero"  # same prose, exit code 0
NO_REPO = "no-repo"  # a real git that cannot resolve this directory's repo


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeGit:
    """A fake `subprocess.run` modelling a machine with a specific set of git
    binaries. Any path not named in `behaviors` raises `FileNotFoundError`, exactly
    as `subprocess.run` does for a binary that isn't installed."""

    def __init__(self, behaviors: dict[str, str]):
        self.behaviors = behaviors
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cwd=None, capture_output=False, text=False, **kwargs):
        self.calls.append(list(argv))
        executable, args = argv[0], list(argv[1:])
        behavior = self.behaviors.get(executable)
        if behavior is None:
            raise FileNotFoundError(2, "No such file or directory", executable)
        if behavior == XCODE_SHIM:
            return _FakeCompleted(69, stdout=_XCODE_LICENSE_MESSAGE)
        if behavior == XCODE_SHIM_EXIT_ZERO:
            return _FakeCompleted(0, stdout=_XCODE_LICENSE_MESSAGE)
        if args == ["--version"]:
            return _FakeCompleted(0, stdout="git version 2.54.0\n")
        if behavior == NO_REPO:
            return _FakeCompleted(128, stderr="fatal: not a git repository")
        if args == ["rev-parse", "--show-toplevel"]:
            return _FakeCompleted(0, stdout="/fake/repo\n")
        if args and args[0] == "show":
            return _FakeCompleted(0, stdout=f"content of {args[1]}\n")
        return _FakeCompleted(1, stderr=f"unexpected fake git call: {args}")


def _install_fake_git(monkeypatch, behaviors: dict[str, str], *, which: str | None) -> _FakeGit:
    fake = _FakeGit(behaviors)
    monkeypatch.setattr(git_show.subprocess, "run", fake)
    monkeypatch.setattr(git_show.shutil, "which", lambda name: which)
    return fake


def test_the_paths_git_is_preferred_when_it_actually_works(monkeypatch):
    _install_fake_git(
        monkeypatch,
        {"/opt/homebrew/bin/git": WORKING, "/usr/bin/git": WORKING},
        which="/opt/homebrew/bin/git",
    )

    assert git_show.git_executable() == "/opt/homebrew/bin/git"


def test_falls_back_to_homebrew_when_the_paths_git_is_the_xcode_blocked_shim(monkeypatch):
    """The exact launchd failure this fix exists for: the service's PATH resolves
    `git` to /usr/bin/git, which refuses to run until the Xcode license is
    accepted, while /opt/homebrew/bin/git works fine."""
    _install_fake_git(
        monkeypatch,
        {"/usr/bin/git": XCODE_SHIM, "/opt/homebrew/bin/git": WORKING},
        which="/usr/bin/git",
    )

    assert git_show.git_executable() == "/opt/homebrew/bin/git"
    assert git_show.repo_root().as_posix() == "/fake/repo"


def test_a_git_that_exits_zero_but_prints_the_license_notice_is_rejected(monkeypatch):
    """Exit code alone is NOT trusted -- the output must actually look like a
    version string, or a shim that prints prose while exiting 0 would be accepted
    and then fail on every real call."""
    fake = _install_fake_git(
        monkeypatch,
        {"/usr/bin/git": XCODE_SHIM_EXIT_ZERO, "/usr/local/bin/git": WORKING},
        which="/usr/bin/git",
    )

    assert git_show.git_executable() == "/usr/local/bin/git"
    assert ["/usr/bin/git", "--version"] in fake.calls  # it WAS probed, and rejected on output


def test_a_git_that_cannot_resolve_this_repo_is_rejected(monkeypatch):
    """The probe's second check: `git --version` looking right is not enough -- a
    real repository call must succeed too."""
    _install_fake_git(
        monkeypatch,
        {"/usr/bin/git": NO_REPO, "/opt/homebrew/bin/git": WORKING},
        which="/usr/bin/git",
    )

    assert git_show.git_executable() == "/opt/homebrew/bin/git"


def test_git_executable_raises_and_names_every_tried_path_when_nothing_works(monkeypatch):
    _install_fake_git(monkeypatch, {"/usr/bin/git": XCODE_SHIM}, which="/usr/bin/git")

    with pytest.raises(git_show.GitUnavailableError) as excinfo:
        git_show.git_executable()

    message = str(excinfo.value)
    assert "/usr/bin/git" in message
    assert "/opt/homebrew/bin/git" in message
    assert git_show.git_is_available() is False


def test_the_probe_result_is_cached_for_the_process(monkeypatch):
    fake = _install_fake_git(monkeypatch, {"/opt/homebrew/bin/git": WORKING}, which="/opt/homebrew/bin/git")

    git_show.git_executable()
    calls_after_first = len(fake.calls)
    git_show.git_executable()
    git_show.repo_root()

    assert len(fake.calls) == calls_after_first  # no re-probing


def test_a_failed_probe_is_cached_too(monkeypatch):
    fake = _install_fake_git(monkeypatch, {}, which=None)

    with pytest.raises(git_show.GitUnavailableError):
        git_show.git_executable()
    calls_after_first = len(fake.calls)
    with pytest.raises(git_show.GitUnavailableError):
        git_show.git_executable()

    assert len(fake.calls) == calls_after_first  # a broken machine re-probes once, not per call


# --- Faked git binaries: failing SOFT (reviewer H2) ---------------------------------


def test_show_file_at_ref_returns_none_when_no_working_git_exists(monkeypatch):
    _install_fake_git(monkeypatch, {"/usr/bin/git": XCODE_SHIM}, which="/usr/bin/git")

    assert git_show.show_file_at_ref("a" * 40, "deploy/candidates/production-baseline/model.txt") is None


def test_read_candidate_declaration_reports_a_read_error_when_no_working_git_exists(monkeypatch):
    _install_fake_git(monkeypatch, {"/usr/bin/git": XCODE_SHIM}, which="/usr/bin/git")

    declaration = git_show.read_candidate_declaration_at_ref("a" * 40, "production-baseline")

    assert declaration["read_error"]
    assert "no working 'git' executable" in declaration["read_error"]
    # Same keys as a successful read, every value empty -- the template renders the
    # note instead of the panel's content, and nothing raises.
    assert declaration["slug"] == "production-baseline"
    assert declaration["model"] == ""
    assert declaration["system_prompt"] == ""
    assert declaration["sub_agents"] == []
    assert declaration["is_multi_agent"] is False


def test_read_candidate_declaration_reports_a_read_error_for_malformed_json_at_the_ref(monkeypatch):
    """A historical commit may carry an `agent.json` this code can no longer parse
    -- that must degrade the display panel, not raise."""

    class _MalformedJsonGit(_FakeGit):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if len(argv) > 1 and argv[1] == "show" and argv[2].endswith("agent.json"):
                return _FakeCompleted(0, stdout="{not json at all")
            return result

    fake = _MalformedJsonGit({"/opt/homebrew/bin/git": WORKING})
    monkeypatch.setattr(git_show.subprocess, "run", fake)
    monkeypatch.setattr(git_show.shutil, "which", lambda name: "/opt/homebrew/bin/git")

    declaration = git_show.read_candidate_declaration_at_ref("a" * 40, "production-baseline")

    assert declaration["read_error"]
    assert "not valid JSON" in declaration["read_error"]
    assert declaration["model"]  # the readable files still came through
