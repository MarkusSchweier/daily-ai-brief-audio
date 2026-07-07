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
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """The repo's top-level directory, via `git rev-parse --show-toplevel` run
    from this file's own directory (works regardless of the Flask process's cwd)."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(Path(__file__).resolve().parent),
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def show_file_at_ref(ref: str, relative_path: str) -> str | None:
    """`git show <ref>:<relative_path>` -- returns the file's content at that
    commit, or None if the path didn't exist at that ref (a candidate directory
    layout that has since changed, or an optional file like `multiagent.json` that
    only exists for multi-agent candidates). Fails soft by design -- this module
    is used only for DISPLAY, never for anything that must fail loud."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{relative_path}"],
        cwd=str(repo_root()),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def read_candidate_declaration_at_ref(ref: str, slug: str) -> dict[str, Any]:
    """Reconstruct a display-oriented view of candidate `slug`'s declaration as it
    was at `ref` -- model, description, system/task prompts, and (for a multi-agent
    candidate) each sub-agent's own name/model/prompts, read straight out of
    `multiagent.json`'s inline roster (the same source
    `candidate_sync.loader.load_candidate()` reads today, just historically)."""
    base = f"deploy/candidates/{slug}"

    model = (show_file_at_ref(ref, f"{base}/model.txt") or "").strip()
    system_prompt = show_file_at_ref(ref, f"{base}/system-prompt.md") or ""
    task_prompt = show_file_at_ref(ref, f"{base}/task-prompt.md") or ""

    agent_json_raw = show_file_at_ref(ref, f"{base}/agent.json")
    agent_json = json.loads(agent_json_raw) if agent_json_raw else {}

    sub_agents: list[dict[str, Any]] = []
    multiagent_raw = show_file_at_ref(ref, f"{base}/multiagent.json")
    if multiagent_raw:
        multiagent = json.loads(multiagent_raw)
        for entry in multiagent.get("agents", []):
            sub_agents.append(
                {
                    "name": entry.get("name", ""),
                    "description": entry.get("description", ""),
                    "model": entry.get("model", ""),
                    "system_prompt": entry.get("system_prompt", ""),
                    "task_prompt": entry.get("task_prompt", ""),
                }
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
    }


__all__ = ["repo_root", "show_file_at_ref", "read_candidate_declaration_at_ref"]
