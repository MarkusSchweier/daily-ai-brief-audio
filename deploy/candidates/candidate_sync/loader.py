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


# The ONLY tunable `parameters.json` (or a multi-agent roster entry's inline
# `parameters`) may declare, and the only levels the Agents API accepts for it.
#
# HISTORY -- why this is suddenly real (2026-10-11): when this mechanism was built
# (July 2026, agent-system-redesign epic Phase 2/3), the Managed Agents Agents API
# exposed NO effort/thinking knob at all, probed live: a TOP-LEVEL `parameters` field
# was rejected outright (`unknown field "parameters"`), `model.thinking` was rejected
# the same way, and `model.effort` was ACCEPTED BUT SILENTLY DISCARDED (proven by a
# no-op discriminator -- re-sending the same model with `effort: low`, then
# `effort: high`, returned the SAME agent version both times, i.e. the field was
# parsed and dropped, never stored). `parameters.json` was therefore
# "declaration-only": every candidate in this repo carries `{}`, and a NON-empty one
# would have failed loud at sync (see docs/prd/cost-optimization-candidates.md §3's
# "#7 BLOCKED BY THE PLATFORM" note -- building effort candidates then would have
# produced configurations that LOOK low-effort but run at default, i.e. silent
# experiment corruption).
#
# The API now DOCUMENTS effort on the agent's model object: `model` accepts either a
# bare model-id string OR an object `{"id": ..., "effort": ...}`, where `effort` is a
# level string (or `{"type": <level>}`). So `parameters.json`'s `effort` key is now a
# REAL, honored knob -- mapped onto `model.effort` by `to_agent_body()`, never sent as
# a top-level `parameters` field (which the API still does not accept).
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
_SUPPORTED_PARAMETER_KEYS: tuple[str, ...] = ("effort",)


def validate_parameters(parameters: Any, *, source: str) -> dict[str, Any]:
    """Validate a candidate's declared `parameters` mapping, failing LOUD (a
    `CandidateLoadError`) at LOAD time rather than letting an unsupported key or a
    bogus effort level reach the Agents API.

    Why fail at load, not at the API: the API's own failure modes for this field are
    exactly the two that corrupt an experiment silently. An unsupported key either
    400s with an opaque `unknown field` message (if sent top-level) or -- far worse --
    is accepted and silently DISCARDED (the live-proven behavior of
    `model.effort` before effort support shipped, and of any model-object key the API
    doesn't recognize), producing a candidate that READS as tuned but RUNS at default.
    A cost/quality comparison built on that is invalid and looks fine. So the loader
    allows exactly one key (`effort`) with exactly the documented levels, and anything
    else stops the sync before a single HTTP call is made.

    `{}` (or no `parameters.json` at all) is valid and means "no effort declared" --
    the state every candidate in this repo was in before 2026-10-11, and the state
    `to_agent_body()` must keep rendering as a byte-identical bare-string `model`."""
    if not isinstance(parameters, dict):
        raise CandidateLoadError(
            f"{source} must be a JSON object (e.g. {{}} or {{\"effort\": \"high\"}}), "
            f"got {type(parameters).__name__}"
        )

    unsupported = sorted(key for key in parameters if key not in _SUPPORTED_PARAMETER_KEYS)
    if unsupported:
        raise CandidateLoadError(
            f"{source} declares unsupported parameter key(s): {', '.join(repr(k) for k in unsupported)}. "
            f"The only supported key is 'effort' (one of: {', '.join(EFFORT_LEVELS)}). "
            "Anything else is either rejected outright by the Agents API or -- worse -- accepted and "
            "silently discarded, which would make this candidate look tuned while running at default."
        )

    if "effort" in parameters and parameters["effort"] not in EFFORT_LEVELS:
        raise CandidateLoadError(
            f"{source} declares effort {parameters['effort']!r}, which is not a supported level. "
            f"Must be exactly one of: {', '.join(EFFORT_LEVELS)}."
        )

    return parameters


@dataclass(frozen=True)
class AgentDeclaration:
    """One agent's full declaration -- used for both a single-agent candidate's sole
    agent and, within a multi-agent candidate, both the coordinator and each
    sub-agent. Each carries its own model/prompt(s)/skills/parameters, per PRD
    FR-10/AC-10 ("no fundamentally different structure required to add a sub-agent")."""

    name: str
    description: str
    model: str  # ALWAYS the plain model-id string from model.txt -- never the API's model OBJECT shape
    system_prompt: str
    task_prompt: str  # the deployment initial_prompt / run task; "" for a sub-agent that has none of its own
    tools: list[dict[str, Any]]
    mcp_servers: list[dict[str, Any]]
    skills: list[dict[str, Any]]  # [{skill_id, version}, ...] concrete pinned versions
    parameters: dict[str, Any]
    agent_id: str | None  # None until this agent's first sync

    @property
    def effort(self) -> str | None:
        """The declared effort level (`parameters.json`'s one supported key), or None
        when none is declared. Validated at LOAD (see `validate_parameters()`), so by
        the time this is read the value is either absent or one of `EFFORT_LEVELS`.

        Kept as a derived property rather than its own dataclass field deliberately:
        `parameters` stays the single, raw, git-tracked source of truth (the eval
        harness reads `AgentDeclaration.parameters` wholesale for its run records),
        and `model` stays the plain model-id STRING (the harness's per-thread cost
        attribution matches transcript model ids against it) -- only the request body
        rendered by `to_agent_body()` merges the two into the API's model object."""
        return self.parameters.get("effort")

    def to_agent_body(self) -> dict[str, Any]:
        """The POST /v1/agents (create) or POST /v1/agents/{id} (update) request body
        for this agent's own declaration -- everything except `multiagent` (a
        coordinator's body additionally sets `multiagent`, added by the caller) and
        except `version` (added by the caller only for an update).

        `model` is rendered in ONE of two shapes, and which one matters:

          - **No effort declared** (`parameters.json` == `{}`, i.e. every candidate in
            this repo before 2026-10-11): the bare model-id STRING, byte-identical to
            what this method has always produced. This is a hard invariant -- an
            existing candidate's request body must not change at all just because
            effort support was added, or its next "unchanged declaration" re-sync would
            issue a spurious update and bump its live version for no reason (precisely
            the FR-12/AC-12 no-op bug Phase 5 had to fix for the read side).
          - **Effort declared**: the API's model OBJECT, `{"id": <model>, "effort":
            <level>}`. The level is sent as a plain string; the API also accepts
            `{"type": <level>}` and echoes it back in either form, which is why the
            read-side comparison in `sync.py` normalizes both.

        A top-level `parameters` field is NEVER sent, in either shape. It is not part
        of the agent definition the Agents API accepts (`unknown field "parameters"`,
        live-probed) -- `parameters.json` is this repo's own declaration file, and
        `effort` is its only key, mapped onto `model.effort` here. (An earlier revision
        of this method DID forward `parameters` verbatim as a top-level field; that was
        dead code in practice, since every candidate carried `{}` and a non-empty one
        would simply have 400'd.)"""
        body: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "model": {"id": self.model, "effort": self.effort} if self.effort else self.model,
            "system": self.system_prompt,
            "tools": self.tools,
            "mcp_servers": self.mcp_servers,
        }
        if self.skills:
            # Strip `content_hash` (a purely LOCAL, skills.json-only bookkeeping
            # field the sync script uses to detect a candidate-owned skill's
            # content changing between syncs -- see sync.py's
            # `_compute_skill_content_hash()`) before sending to the Agents API,
            # which expects exactly {type, skill_id, version} per entry (the
            # confirmed production shape, deploy/managed-agent/agent.json:25-31).
            # Leaking this extra field into the request body would also make
            # `_live_declaration_differs()` see a spurious diff on EVERY sync
            # (the live agent, echoed back via GET, never carries content_hash).
            body["skills"] = [{k: v for k, v in entry.items() if k != "content_hash"} for entry in self.skills]
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
    validate_parameters(parameters_json, source=str(directory / "parameters.json"))

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


def _load_sub_agent(entry: dict[str, Any], *, source: str) -> AgentDeclaration:
    """Load ONE sub-agent's declaration directly from its `multiagent.json` roster
    entry (inline fields), rather than a separate directory -- a sub-agent's
    model/prompt/skills/parameters are declared inline in the roster per Decision 2c's
    "each entry carrying its own model/system-prompt/skills/parameters".

    A roster entry's inline `parameters` is validated by the SAME
    `validate_parameters()` rules a top-level `parameters.json` gets -- a sub-agent is
    declared exactly as richly as the coordinator, so an unsupported key or a bogus
    effort level buried in the roster must fail just as loudly (a per-sub-agent effort
    level is a genuinely useful lever for the decomposition candidates: e.g. a cheap
    model at high effort for research, the same model at low effort for the mechanical
    listening-script rewrite)."""
    validate_parameters(entry.get("parameters", {}), source=source)
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
        sub_agents = [
            _load_sub_agent(
                entry,
                source=(
                    f"{multiagent_json_path}'s roster entry [{index}] "
                    f"({entry.get('name') or '(unnamed)'})'s inline 'parameters'"
                ),
            )
            for index, entry in enumerate(roster)
        ]

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
