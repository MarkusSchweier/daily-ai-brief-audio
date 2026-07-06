"""The sync algorithm itself (Decision 2c's numbered steps, PRD FR-11/FR-12,
AC-11/AC-12): turn a loaded candidate declaration into live Claude Platform Agent
resources, idempotently, using exactly one stable `agent_id` per agent for its whole
life.

Required behavior (implemented exactly, per the task brief and Decision 2c):

  1. Read the candidate's tracked ids (`agent_id` from `candidate.json`, each
     sub-agent's `agent_id` from `multiagent.json`'s roster) and pinned skill
     version(s) from `skills.json`.
  2. No `agent_id` yet on the top-level (coordinator/sole) agent -> FIRST SYNC:
     push any candidate-owned skill version first, then create the agent(s), then
     write the returned id(s) back into the tracked files as an ordinary edit (no
     git commit performed by this script).
  3. `agent_id` already present -> UPDATE IN PLACE: for each agent, fetch its CURRENT
     live state (`GET /v1/agents/{id}`) and compare it against the local declaration
     -- "changed" is determined by this live-vs-local diff, not by any local
     memory of a "previous" declaration (there is no side-file recording that; the
     live agent resource itself IS the record of what was last synced). Only a
     genuinely-changed agent gets a POST update, with the version read from that
     SAME fresh GET (never cached/assumed). On a 409 (stale version -- someone/
     something else updated the agent between our GET and our POST), re-read the
     current version once and retry, never blindly overwrite.
  4. Multi-agent ordering: sub-agent(s) first, THEN a follow-up coordinator update
     (only if any sub-agent actually changed, or the coordinator's own declaration
     changed) so its `multiagent.agents` roster re-pins to the new sub-agent
     version(s). A single-agent candidate has no step 4.
  5. Idempotent + resumable: an unchanged declaration makes zero HTTP calls beyond
     the one read-only GET-per-agent needed to detect "unchanged" (no update, no
     create); re-running after a partial failure (skill pushed + skills.json
     updated, but agent creation then failed) does not re-push the skill, because
     `skills.json` already carries the pushed version by the time creation is
     retried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import api_client
from .loader import AgentDeclaration, CandidateDeclaration, load_candidate
from .writer import write_candidate_agent_id, write_skills_json, write_sub_agent_ids

_HTTP_CONFLICT_STATUS = 409


@dataclass
class SyncResult:
    """A human-readable summary of what a `sync_candidate()` call actually did --
    returned so the CLI can print a clear report and tests can assert on outcomes
    without re-deriving them from raw HTTP call logs."""

    slug: str
    created: list[str] = field(default_factory=list)  # e.g. ["agent (coordinator)", "sub-agent[0] some-name"]
    updated: list[str] = field(default_factory=list)
    skill_versions_pushed: list[str] = field(default_factory=list)  # e.g. ["skill_abc123 -> version 1783..."]
    no_op: bool = False

    def __str__(self) -> str:
        if self.no_op:
            return f"candidate '{self.slug}': no-op (declaration unchanged, no create/update call made)"
        parts = []
        if self.skill_versions_pushed:
            parts.append(f"skill versions pushed: {', '.join(self.skill_versions_pushed)}")
        if self.created:
            parts.append(f"created: {', '.join(self.created)}")
        if self.updated:
            parts.append(f"updated: {', '.join(self.updated)}")
        return f"candidate '{self.slug}': " + "; ".join(parts) if parts else f"candidate '{self.slug}': no-op"


def _zip_skill_source(skill_source_dir: Path, dest_zip_path: Path) -> Path:
    """Zip a candidate-owned `skill/` directory into a single archive, mirroring the
    exact packaging shape `deploy/managed-agent/README.md` documents for a skill-
    version push (`zip -r -q ... daily-ai-brief -x "*.DS_Store"`) -- the zip's entries
    are the skill directory's own contents (e.g. `SKILL.md`, `sources.md`), not the
    directory name itself, matching the existing production skill's package shape."""
    import zipfile

    with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(skill_source_dir.rglob("*")):
            if path.is_file() and path.name != ".DS_Store":
                zf.write(path, arcname=path.relative_to(skill_source_dir))
    return dest_zip_path


def _push_candidate_skill_if_needed(candidate: CandidateDeclaration, skills_client: httpx.Client, result: SyncResult) -> None:
    """First-sync-only: if the candidate owns its own skill source (a `skill/`
    subdirectory) and `skills.json` carries a `{skill_id}` entry with no `version`
    pinned yet, push a new skill version and record the result into `skills.json`
    IMMEDIATELY -- BEFORE agent creation is attempted. This ordering is exactly what
    makes a partial failure (skill pushed, then agent creation fails) resumable: on
    retry, `skills.json` already has the pinned version, so this function finds
    nothing left to push and does nothing (no duplicate skill version)."""
    if candidate.skill_source_dir is None:
        return

    existing_skills = candidate.agent.skills
    skill_id = None
    for entry in existing_skills:
        if entry.get("skill_id") and not entry.get("version"):
            skill_id = entry["skill_id"]
            break
    if skill_id is None:
        # No skill-id-without-a-version placeholder found -- either there's no
        # candidate-owned skill declared in skills.json yet (nothing to push
        # against), or a version is already pinned (already pushed in a prior,
        # partially completed run) -- either way, nothing to do here.
        return

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = _zip_skill_source(candidate.skill_source_dir, Path(tmp_dir) / "skill.zip")
        response = api_client.create_skill_version(skills_client, skill_id, str(zip_path))

    new_version = response.get("version") or response.get("id")
    updated_skills = [
        ({**entry, "version": new_version} if entry.get("skill_id") == skill_id else entry) for entry in existing_skills
    ]
    write_skills_json(candidate.skills_json_path, updated_skills)
    result.skill_versions_pushed.append(f"{skill_id} -> version {new_version}")


def _create_agent(agents_client: httpx.Client, declaration: AgentDeclaration, *, multiagent: dict[str, Any] | None) -> str:
    body = declaration.to_agent_body()
    if multiagent is not None:
        body["multiagent"] = multiagent
    created = api_client.create_agent(agents_client, body)
    return created["id"]


def _live_declaration_differs(live_agent: dict[str, Any], declaration: AgentDeclaration) -> bool:
    """Compare the CURRENT live agent (from a fresh GET) against the local
    declaration on exactly the fields the local declaration controls. This is the
    sync script's own "did this change" check -- performed in addition to (not
    instead of) the platform's own no-op detection on update, so an unchanged
    declaration never even attempts an update call (per the task brief: "Only call
    update for a genuinely changed declaration -- an unchanged one is a no-op at the
    script level, not just relying on the platform's own no-op detection")."""
    local_body = declaration.to_agent_body()
    for field_name, local_value in local_body.items():
        if live_agent.get(field_name) != local_value:
            return True
    return False


def _update_agent_with_retry(agents_client: httpx.Client, agent_id: str, declaration: AgentDeclaration, *, multiagent: dict[str, Any] | None, current_version: int) -> None:
    """Update one agent in place using the ALREADY-FETCHED `current_version` (the
    caller obtained it via the same fresh GET used for change detection, so this
    function does not re-GET before the first attempt). Retries EXACTLY once on a 409
    (stale version) by re-reading the current version and retrying -- never blindly
    overwriting."""
    body = declaration.to_agent_body()
    if multiagent is not None:
        body["multiagent"] = multiagent

    try:
        api_client.update_agent(agents_client, agent_id, version=current_version, agent_definition=body)
        return
    except httpx.HTTPStatusError as e:
        if e.response.status_code != _HTTP_CONFLICT_STATUS:
            raise
        # Stale version -- re-read the current version and retry exactly once.
        current = api_client.get_agent(agents_client, agent_id)
        api_client.update_agent(agents_client, agent_id, version=current["version"], agent_definition=body)


def _build_multiagent_config(candidate: CandidateDeclaration, sub_agent_ids: list[str | None]) -> dict[str, Any]:
    """Build the coordinator's `multiagent` field from the candidate's
    `multiagent.json` roster, substituting each sub-agent's live id (freshly created
    or already-known) so the coordinator always references real agent ids."""
    assert candidate.multiagent_json is not None
    roster = candidate.multiagent_json.get("agents", [])
    agents_field = []
    for index, roster_entry in enumerate(roster):
        entry = dict(roster_entry.get("entry", {}))
        entry["agent"] = sub_agent_ids[index]
        agents_field.append({"entry": entry})
    return {
        "type": candidate.multiagent_json.get("type", "coordinator"),
        "agents": agents_field,
    }


def sync_candidate(candidate_dir: Path, *, agents_client: httpx.Client, skills_client: httpx.Client) -> SyncResult:
    """Sync one candidate directory to live Claude Platform Agent resources. See this
    module's docstring for the full algorithm; this function is the single entry
    point both the CLI and the tests drive."""
    candidate = load_candidate(candidate_dir)
    result = SyncResult(slug=candidate.slug)

    is_first_sync = candidate.agent.agent_id is None

    if is_first_sync:
        _first_sync(candidate_dir, candidate, agents_client, skills_client, result)
        return result

    if candidate.is_multi_agent:
        _update_multi_agent(candidate, agents_client, result)
    else:
        _update_single_agent(candidate, agents_client, result)
    return result


def _first_sync(
    candidate_dir: Path,
    candidate: CandidateDeclaration,
    agents_client: httpx.Client,
    skills_client: httpx.Client,
    result: SyncResult,
) -> None:
    _push_candidate_skill_if_needed(candidate, skills_client, result)
    # Re-load: the skill push (if any) may have rewritten skills.json, and the
    # freshly-pinned version must be what's sent to agent-create.
    candidate = load_candidate(candidate_dir)

    if not candidate.is_multi_agent:
        agent_id = _create_agent(agents_client, candidate.agent, multiagent=None)
        result.created.append(f"agent {candidate.agent.name or '(unnamed)'}")
        write_candidate_agent_id(candidate.candidate_json_path, agent_id)
        return

    sub_agent_ids: list[str | None] = []
    for index, sub_agent in enumerate(candidate.sub_agents):
        agent_id = _create_agent(agents_client, sub_agent, multiagent=None)
        sub_agent_ids.append(agent_id)
        result.created.append(f"sub-agent[{index}] {sub_agent.name or '(unnamed)'}")
    write_sub_agent_ids(candidate.multiagent_json_path, dict(enumerate(sub_agent_ids)))

    multiagent_config = _build_multiagent_config(candidate, sub_agent_ids)
    coordinator_id = _create_agent(agents_client, candidate.agent, multiagent=multiagent_config)
    result.created.append(f"agent (coordinator) {candidate.agent.name or '(unnamed)'}")
    write_candidate_agent_id(candidate.candidate_json_path, coordinator_id)


def _update_single_agent(candidate: CandidateDeclaration, agents_client: httpx.Client, result: SyncResult) -> None:
    assert candidate.agent.agent_id is not None
    live_agent = api_client.get_agent(agents_client, candidate.agent.agent_id)
    if _live_declaration_differs(live_agent, candidate.agent):
        _update_agent_with_retry(
            agents_client,
            candidate.agent.agent_id,
            candidate.agent,
            multiagent=None,
            current_version=live_agent["version"],
        )
        result.updated.append(f"agent {candidate.agent.name or '(unnamed)'}")
    else:
        result.no_op = True


def _update_multi_agent(candidate: CandidateDeclaration, agents_client: httpx.Client, result: SyncResult) -> None:
    # Step (i): update whichever sub-agent(s) changed, FIRST -- and ONLY those.
    any_sub_agent_updated = False
    current_sub_agent_ids: list[str | None] = []
    for index, sub_agent in enumerate(candidate.sub_agents):
        assert sub_agent.agent_id is not None
        current_sub_agent_ids.append(sub_agent.agent_id)
        live_sub_agent = api_client.get_agent(agents_client, sub_agent.agent_id)
        if _live_declaration_differs(live_sub_agent, sub_agent):
            _update_agent_with_retry(
                agents_client,
                sub_agent.agent_id,
                sub_agent,
                multiagent=None,
                current_version=live_sub_agent["version"],
            )
            result.updated.append(f"sub-agent[{index}] {sub_agent.name or '(unnamed)'}")
            any_sub_agent_updated = True

    # Step (ii): a follow-up coordinator update -- happens if a sub-agent changed
    # (so the roster re-pins to the new sub-agent version(s), per Decision 2c's
    # "a coordinator does not automatically pick up a new sub-agent version") OR the
    # coordinator's own declaration changed on its own merits.
    assert candidate.agent.agent_id is not None
    live_coordinator = api_client.get_agent(agents_client, candidate.agent.agent_id)
    coordinator_declaration_changed = _live_declaration_differs(live_coordinator, candidate.agent)

    if any_sub_agent_updated or coordinator_declaration_changed:
        multiagent_config = _build_multiagent_config(candidate, current_sub_agent_ids)
        _update_agent_with_retry(
            agents_client,
            candidate.agent.agent_id,
            candidate.agent,
            multiagent=multiagent_config,
            current_version=live_coordinator["version"],
        )
        result.updated.append(f"agent (coordinator) {candidate.agent.name or '(unnamed)'}")

    if not result.updated and not result.skill_versions_pushed:
        result.no_op = True
