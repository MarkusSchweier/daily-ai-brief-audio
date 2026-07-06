"""Load a candidate's declaration from a `deploy/candidates/<slug>/` directory into a
structured, comparable representation.

Per Decision 2c / PRD FR-9, each dimension (model, system prompt, task prompt, tools/
mcp_servers, skill references, parameters) lives in its own small, independently-
diffable file. This module's job is ONLY to read those files into memory -- it makes
no network calls and mutates nothing. See `deploy/candidates/README.md` for the full
schema documentation of each file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CandidateLoadError(ValueError):
    """Raised when a candidate directory is missing a required file or a required
    file's content doesn't parse -- a clear, fail-loud error rather than a confusing
    KeyError/FileNotFoundError deep in sync logic."""


@dataclass(frozen=True)
class AgentDeclaration:
    """One agent's full declaration -- used for both a single-agent candidate's sole
    agent and, within a multi-agent candidate, both the coordinator and each
    sub-agent. Each carries its own model/prompt(s)/skills/parameters, per PRD
    FR-10/AC-10 ("no fundamentally different structure required to add a sub-agent")."""

    name: str
    description: str
    model: str
    system_prompt: str
    task_prompt: str  # the deployment initial_prompt / run task; "" for a sub-agent that has none of its own
    tools: list[dict[str, Any]]
    mcp_servers: list[dict[str, Any]]
    skills: list[dict[str, Any]]  # [{skill_id, version}, ...] concrete pinned versions
    parameters: dict[str, Any]
    agent_id: str | None  # None until this agent's first sync

    def to_agent_body(self) -> dict[str, Any]:
        """The POST /v1/agents (create) or POST /v1/agents/{id} (update) request body
        for this agent's own declaration -- everything except `multiagent` (a
        coordinator's body additionally sets `multiagent`, added by the caller) and
        except `version` (added by the caller only for an update)."""
        body: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "model": self.model,
            "system": self.system_prompt,
            "tools": self.tools,
            "mcp_servers": self.mcp_servers,
        }
        if self.skills:
            body["skills"] = self.skills
        if self.parameters:
            body["parameters"] = self.parameters
        return body

    def declaration_fingerprint(self) -> dict[str, Any]:
        """Everything that constitutes a "did this agent's declaration change since
        its last sync" comparison -- deliberately EXCLUDES `agent_id` (an id is never
        part of the declaration's own content, per Decision 2c: "an unchanged agent id
        means no diff"). Task prompt is included because a sub-agent may have none
        (""), and a genuinely-empty task prompt must compare equal to itself, not be
        treated as "missing"."""
        return {
            "name": self.name,
            "description": self.description,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "task_prompt": self.task_prompt,
            "tools": self.tools,
            "mcp_servers": self.mcp_servers,
            "skills": self.skills,
            "parameters": self.parameters,
        }


@dataclass(frozen=True)
class CandidateDeclaration:
    """A fully-loaded candidate: its own directory path, its `candidate.json` metadata,
    the coordinator/sole agent's declaration, and (for a multi-agent candidate) the
    ordered list of sub-agent declarations plus the raw `multiagent.json` roster dict
    (kept so the sync script can write back updated per-entry fields without having to
    reconstruct roster-only keys the loader doesn't model, e.g. `entry.type`)."""

    slug: str
    directory: Path
    candidate_json: dict[str, Any]
    agent: AgentDeclaration
    sub_agents: list[AgentDeclaration] = field(default_factory=list)
    multiagent_json: dict[str, Any] | None = None
    skill_source_dir: Path | None = None  # the optional candidate-owned skill/ dir, if present

    @property
    def is_multi_agent(self) -> bool:
        return bool(self.sub_agents)

    @property
    def candidate_json_path(self) -> Path:
        return self.directory / "candidate.json"

    @property
    def multiagent_json_path(self) -> Path:
        return self.directory / "multiagent.json"

    @property
    def skills_json_path(self) -> Path:
        return self.directory / "skills.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CandidateLoadError(f"required file is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CandidateLoadError(f"{path} is not valid JSON: {e}") from e


def _read_text(path: Path, *, required: bool = True, default: str = "") -> str:
    if not path.exists():
        if required:
            raise CandidateLoadError(f"required file is missing: {path}")
        return default
    return path.read_text(encoding="utf-8")


def _load_top_level_agent(directory: Path, *, agent_id: str | None) -> AgentDeclaration:
    """Load the top-level agent's own files: agent.json, model.txt, system-prompt.md,
    task-prompt.md, skills.json, parameters.json. Used for BOTH a single-agent
    candidate's sole agent AND a multi-agent candidate's coordinator (the coordinator
    is the top-level agent per Decision 2c: "the coordinator is the top-level agent,
    the sub-agents are its agents[].entry list")."""
    agent_json = _read_json(directory / "agent.json")
    model = _read_text(directory / "model.txt").strip()
    system_prompt = _read_text(directory / "system-prompt.md")
    task_prompt = _read_text(directory / "task-prompt.md", required=False)
    skills_json = _read_json(directory / "skills.json") if (directory / "skills.json").exists() else []
    parameters_json = (
        _read_json(directory / "parameters.json") if (directory / "parameters.json").exists() else {}
    )

    if not model:
        raise CandidateLoadError(f"{directory / 'model.txt'} is empty -- the model id is required")

    return AgentDeclaration(
        name=agent_json.get("name", ""),
        description=agent_json.get("description", ""),
        model=model,
        system_prompt=system_prompt,
        task_prompt=task_prompt,
        tools=agent_json.get("tools", []),
        mcp_servers=agent_json.get("mcp_servers", []),
        skills=skills_json,
        parameters=parameters_json,
        agent_id=agent_id,
    )


def _load_sub_agent(entry: dict[str, Any]) -> AgentDeclaration:
    """Load ONE sub-agent's declaration directly from its `multiagent.json` roster
    entry (inline fields), rather than a separate directory -- a sub-agent's
    model/prompt/skills/parameters are declared inline in the roster per Decision 2c's
    "each entry carrying its own model/system-prompt/skills/parameters"."""
    return AgentDeclaration(
        name=entry.get("name", ""),
        description=entry.get("description", ""),
        model=entry.get("model", ""),
        system_prompt=entry.get("system_prompt", ""),
        task_prompt=entry.get("task_prompt", ""),
        tools=entry.get("tools", []),
        mcp_servers=entry.get("mcp_servers", []),
        skills=entry.get("skills", []),
        parameters=entry.get("parameters", {}),
        agent_id=entry.get("agent_id"),
    )


def load_candidate(directory: Path) -> CandidateDeclaration:
    """Load a full candidate declaration from `directory` (e.g.
    `deploy/candidates/example-single-agent/`). Makes no network calls; raises
    `CandidateLoadError` on any missing-required-file or malformed-JSON condition."""
    directory = Path(directory)
    if not directory.is_dir():
        raise CandidateLoadError(f"candidate directory does not exist: {directory}")

    candidate_json = _read_json(directory / "candidate.json")
    slug = candidate_json.get("slug") or directory.name
    top_level_agent = _load_top_level_agent(directory, agent_id=candidate_json.get("agent_id"))

    multiagent_json_path = directory / "multiagent.json"
    sub_agents: list[AgentDeclaration] = []
    multiagent_json: dict[str, Any] | None = None
    if multiagent_json_path.exists():
        multiagent_json = _read_json(multiagent_json_path)
        roster = multiagent_json.get("agents", [])
        if not roster:
            raise CandidateLoadError(f"{multiagent_json_path} has a 'multiagent' declaration but no agents in its roster")
        sub_agents = [_load_sub_agent(entry) for entry in roster]

    skill_source_dir = directory / "skill"
    if not skill_source_dir.is_dir():
        skill_source_dir = None

    return CandidateDeclaration(
        slug=slug,
        directory=directory,
        candidate_json=candidate_json,
        agent=top_level_agent,
        sub_agents=sub_agents,
        multiagent_json=multiagent_json,
        skill_source_dir=skill_source_dir,
    )
