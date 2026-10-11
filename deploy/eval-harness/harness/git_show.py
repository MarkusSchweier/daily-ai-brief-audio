"""Read a candidate declaration's file content at a HISTORICAL git ref, via plain
`git show <ref>:<path>` -- no checkout, no rollback (ADR-0014's git-native
principle, reused here for the UI's deep-dive page: "candidate config... at the
recorded git ref", PRD §4.1).

Every eval run records the repo's `git rev-parse HEAD` at trigger time
(`harness.run_store.current_git_ref()`); this module is how the UI later recovers
EXACTLY what that candidate's declaration said back then, even if the live
`deploy/candidates/<slug>/` files have since changed -- `git show` reads a file's
content at a historical commit directly from the object database, without ever
touching the working tree or HEAD.

## Finding a WORKING git (reviewer H2, 2026-10-11)

This module used to run a bare `"git"`, resolved through whatever `PATH` the
calling process happened to have. That broke the UI's deep-dive page outright
once it ran as a launchd service: launchd's default `PATH` resolves `git` to
`/usr/bin/git`, Apple's command-line-tools SHIM, which on a machine whose Xcode
license has not been accepted refuses to do anything and prints "You have not
agreed to the Xcode license agreements" instead (observed exiting 69, but the
exit code is NOT a reliable signal for this class of shim) -- so EVERY run detail
page returned HTTP 500 while `/opt/homebrew/bin/git` sat right there, working.

`git_executable()` therefore PROBES for a git that actually works, in order:
the `PATH`'s own `git` first (an operator's deliberate choice, and the right
answer on any normally-configured machine), then the well-known absolute
locations in `FALLBACK_GIT_EXECUTABLES`. A candidate counts as working only if
BOTH of these hold:

  1. `git --version` exits 0 **and** its stdout actually looks like a version
     string (`"git version ..."`) -- the exit code alone is explicitly not
     trusted, precisely because the macOS shim's failure mode is to print prose;
  2. a real repository call (`git rev-parse --show-toplevel`) from this file's
     own directory exits 0 and names a directory -- a git binary that cannot read
     THIS repo is of no use to the only thing this module does.

The successful probe's executable AND repo root are cached for the process's
lifetime (a failed probe is cached too, so a broken-git machine doesn't re-probe
three binaries on every page render); `reset_git_cache()` exists for tests.

## Failing SOFT (reviewer H2)

Nothing here is load-bearing for correctness -- it feeds a read-only display
panel. So no function in this module raises on a git problem except the explicit
`git_executable()`/`repo_root()` accessors: `show_file_at_ref()` returns None and
`read_candidate_declaration_at_ref()` returns an otherwise-empty declaration
carrying a human-readable `read_error` string, which the deep-dive template
renders as a visible note in the candidate-configuration section. A missing or
broken git degrades that ONE panel, never the scores, costs or brief render
around it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent

# Tried in order AFTER the PATH's own `git`. `/usr/bin/git` is last deliberately:
# on macOS it is the command-line-tools shim that may be the broken one (see the
# module docstring), but on a machine with no Homebrew it is also the only git
# there is -- and the probe below decides on evidence, not on the path.
FALLBACK_GIT_EXECUTABLES: tuple[str, ...] = (
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/usr/bin/git",
)

# What a real `git --version` stdout starts with. The ONE string that
# distinguishes a working git from a binary that exits 0 (or 69, or anything
# else) while printing prose such as the Xcode-license notice.
_VERSION_OUTPUT_PREFIX = "git version"

# Process-lifetime probe cache: the resolved (executable, repo_root) pair, or a
# remembered failure message so a machine with no usable git re-probes once, not
# once per rendered page.
_resolved: tuple[str, Path] | None = None
_failure_message: str | None = None


class GitUnavailableError(RuntimeError):
    """No `git` executable that actually works could be found (see
    `FALLBACK_GIT_EXECUTABLES` and the module docstring's probe rules).

    Raised ONLY by the explicit `git_executable()`/`repo_root()` accessors --
    `show_file_at_ref()` and `read_candidate_declaration_at_ref()` catch it and
    degrade, because this module only ever feeds a display panel."""


def reset_git_cache() -> None:
    """Forget the cached probe result (both success and failure) -- for tests that
    monkeypatch the probe's `subprocess`/`shutil.which` behavior and must not
    inherit a previous test's resolution."""
    global _resolved, _failure_message
    _resolved = None
    _failure_message = None


def candidate_git_executables() -> list[str]:
    """The probe order: the `PATH`'s own `git` (if any) first, then each
    `FALLBACK_GIT_EXECUTABLES` entry not already covered by it."""
    candidates: list[str] = []
    from_path = shutil.which("git")
    if from_path:
        candidates.append(from_path)
    for fallback in FALLBACK_GIT_EXECUTABLES:
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def _run_git(executable: str, args: list[str], *, cwd: Path) -> "subprocess.CompletedProcess[str] | None":
    """Run one git command, returning None instead of raising if the executable
    cannot be run at all (missing file, not executable, ...)."""
    try:
        return subprocess.run([executable, *args], cwd=str(cwd), capture_output=True, text=True)
    except OSError:
        return None


def _probe_git_executable(executable: str) -> Path | None:
    """Both probe checks from the module docstring. Returns the repo root (the
    second check's own output -- the probe and `repo_root()` need exactly the same
    call, so it is made once) or None if this executable is not usable."""
    version = _run_git(executable, ["--version"], cwd=_THIS_DIR)
    if version is None or version.returncode != 0:
        return None
    # Exit code alone is NOT trusted: the macOS command-line-tools shim's failure
    # mode is to PRINT prose ("You have not agreed to the Xcode license
    # agreements"), and a shim that printed it while exiting 0 would otherwise
    # sail through this check.
    if not (version.stdout or "").strip().startswith(_VERSION_OUTPUT_PREFIX):
        return None
    toplevel = _run_git(executable, ["rev-parse", "--show-toplevel"], cwd=_THIS_DIR)
    if toplevel is None or toplevel.returncode != 0:
        return None
    root = (toplevel.stdout or "").strip()
    if not root:
        return None
    return Path(root)


def _resolve_git() -> tuple[str, Path]:
    """The cached `(executable, repo_root)` pair, probing on first use. Raises
    `GitUnavailableError` -- with every tried path named -- if nothing works."""
    global _resolved, _failure_message
    if _resolved is not None:
        return _resolved
    if _failure_message is not None:
        raise GitUnavailableError(_failure_message)

    tried = candidate_git_executables()
    for executable in tried:
        root = _probe_git_executable(executable)
        if root is not None:
            _resolved = (executable, root)
            return _resolved

    _failure_message = (
        "no working 'git' executable found -- tried "
        + ", ".join(tried)
        + ". Each must both print a 'git version ...' line AND resolve this repo's "
        "top level; on macOS /usr/bin/git fails both when the Xcode license has not "
        "been accepted ('You have not agreed to the Xcode license agreements'). "
        "Install git (e.g. Homebrew) or accept the license."
    )
    raise GitUnavailableError(_failure_message)


def git_executable() -> str:
    """Path to a git binary confirmed to work (see the module docstring). Raises
    `GitUnavailableError` if there is none."""
    return _resolve_git()[0]


def git_is_available() -> bool:
    """True if a working git was found -- the non-raising form of
    `git_executable()`, for callers that only want to decide what to display."""
    try:
        _resolve_git()
    except GitUnavailableError:
        return False
    return True


def repo_root() -> Path:
    """The repo's top-level directory, via `git rev-parse --show-toplevel` run from
    this file's own directory (works regardless of the Flask process's cwd). Comes
    straight from the executable probe, so it is one cached call, not two."""
    return _resolve_git()[1]


def show_file_at_ref(ref: str, relative_path: str) -> str | None:
    """`git show <ref>:<relative_path>` -- returns the file's content at that
    commit, or None if it could not be read: the path didn't exist at that ref (a
    candidate directory layout that has since changed, or an optional file like
    `multiagent.json` that only exists for multi-agent candidates), the ref itself
    is unknown, or no working git exists on this machine at all. Fails soft by
    design -- this module is used only for DISPLAY, never for anything that must
    fail loud (`read_candidate_declaration_at_ref()` below is what turns "nothing
    could be read" into a message for the operator)."""
    try:
        executable, root = _resolve_git()
    except GitUnavailableError:
        return None
    result = _run_git(executable, ["show", f"{ref}:{relative_path}"], cwd=root)
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def _empty_declaration(ref: str, slug: str, *, read_error: str | None) -> dict[str, Any]:
    """The shape `read_candidate_declaration_at_ref()` returns when nothing could
    be read -- identical keys, every value empty, plus `read_error` so the UI can
    say WHY instead of silently showing a blank configuration panel."""
    return {
        "slug": slug,
        "ref": ref,
        "name": "",
        "description": "",
        "model": "",
        "system_prompt": "",
        "task_prompt": "",
        "sub_agents": [],
        "is_multi_agent": False,
        "read_error": read_error,
    }


def _load_json_at_ref(ref: str, path: str, errors: list[str]) -> dict[str, Any]:
    """`json.loads()` of one file read at `ref`, appending to `errors` (and
    returning `{}`) rather than raising if the file is absent or malformed -- a
    historical commit may legitimately carry a file this code can no longer parse,
    and that must degrade the display panel, not 500 the page."""
    raw = show_file_at_ref(ref, path)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        errors.append(f"{path} at this ref is not valid JSON ({e})")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_candidate_declaration_at_ref(ref: str, slug: str) -> dict[str, Any]:
    """Reconstruct a display-oriented view of candidate `slug`'s declaration as it
    was at `ref` -- model, description, system/task prompts, and (for a multi-agent
    candidate) each sub-agent's own name/model/prompts, read straight out of
    `multiagent.json`'s inline roster (the same source
    `candidate_sync.loader.load_candidate()` reads today, just historically).

    NEVER raises (reviewer H2): the returned dict always has the same keys, and
    `read_error` is a human-readable string whenever the declaration could not be
    recovered -- no working git on this machine, an unknown/garbage-collected ref,
    a slug that never existed at that commit, or a file that no longer parses. The
    deep-dive template renders it as a visible note in the candidate-configuration
    section."""
    base = f"deploy/candidates/{slug}"

    try:
        _resolve_git()
    except GitUnavailableError as e:
        return _empty_declaration(ref, slug, read_error=str(e))

    errors: list[str] = []

    model = (show_file_at_ref(ref, f"{base}/model.txt") or "").strip()
    system_prompt = show_file_at_ref(ref, f"{base}/system-prompt.md") or ""
    task_prompt = show_file_at_ref(ref, f"{base}/task-prompt.md") or ""
    agent_json = _load_json_at_ref(ref, f"{base}/agent.json", errors)

    sub_agents: list[dict[str, Any]] = []
    multiagent = _load_json_at_ref(ref, f"{base}/multiagent.json", errors)
    for entry in multiagent.get("agents", []):
        if not isinstance(entry, dict):
            continue
        sub_agents.append(
            {
                "name": entry.get("name", ""),
                "description": entry.get("description", ""),
                "model": entry.get("model", ""),
                "system_prompt": entry.get("system_prompt", ""),
                "task_prompt": entry.get("task_prompt", ""),
            }
        )

    if not (model or system_prompt or task_prompt or agent_json):
        errors.insert(
            0,
            f"`git show {ref}:{base}/...` returned nothing -- the recorded ref may be "
            "unknown to this clone (never fetched, or rewritten), or the candidate did "
            "not exist under that path at that commit",
        )

    return {
        "slug": slug,
        "ref": ref,
        "name": agent_json.get("name", ""),
        "description": agent_json.get("description", ""),
        "model": model,
        "system_prompt": system_prompt,
        "task_prompt": task_prompt,
        "sub_agents": sub_agents,
        "is_multi_agent": bool(sub_agents),
        "read_error": "; ".join(errors) if errors else None,
    }


__all__ = [
    "FALLBACK_GIT_EXECUTABLES",
    "GitUnavailableError",
    "candidate_git_executables",
    "git_executable",
    "git_is_available",
    "read_candidate_declaration_at_ref",
    "repo_root",
    "reset_git_cache",
    "show_file_at_ref",
]
