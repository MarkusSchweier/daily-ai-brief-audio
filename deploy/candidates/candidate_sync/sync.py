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
     retried. Likewise, a first-sync multi-agent create that succeeds for the
     sub-agent(s) but then fails on the COORDINATOR's own create call is resumable:
     `_first_sync()` checks each sub-agent's `agent_id` before creating it and skips
     (reuses) any that already has one from the prior attempt, so a retry issues
     exactly one more `POST /v1/agents` call (the coordinator only) -- never a
     second, duplicate, permanently-orphaned sub-agent (there is no confirmed
     delete/archive primitive for an agent resource once created).
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


class _SkillSourceError(ValueError):
    """Raised when a candidate-owned `skill/` directory's SKILL.md is missing or
    lacks a parseable `name:` front-matter field -- needed by `_zip_skill_source()`
    for BOTH a brand-new skill creation AND a version push to an existing skill,
    since the top-level folder name inside the zip must match it exactly in BOTH
    cases (a live-confirmed constraint on both `POST /v1/skills` and
    `POST /v1/skills/{id}/versions`, see `_zip_skill_source()`'s docstring)."""


def _read_skill_name_from_front_matter(skill_source_dir: Path) -> str:
    """Extract the `name:` field from `skill/SKILL.md`'s YAML front matter (the
    `---`-delimited block at the top of the file) -- a plain, dependency-free parse
    (no `pyyaml` import) since only this one scalar field is needed."""
    skill_md_path = skill_source_dir / "SKILL.md"
    if not skill_md_path.is_file():
        raise _SkillSourceError(f"{skill_md_path} is missing -- required to read the skill's declared name")
    text = skill_md_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise _SkillSourceError(f"{skill_md_path} does not start with a '---' YAML front-matter block")
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise _SkillSourceError(f"{skill_md_path}'s front matter has no 'name:' field")


def _iter_skill_source_files(skill_source_dir: Path):
    """Yield every regular file under `skill_source_dir`, sorted for determinism,
    EXCLUDING `.DS_Store` and, deliberately, any symlink -- shared by
    `_compute_skill_content_hash()` and `_zip_skill_source()` so both agree on
    exactly which files constitute "this skill's content."

    SECURITY HARDENING (security-engineer, Low severity -- not exploitable today,
    since only the repo owner authors these directories, but cheap to close): a
    symlink inside `skill/` could otherwise point OUTSIDE `skill_source_dir`
    (e.g. `skill/evil -> /etc/passwd` or a sibling directory), and
    `Path.rglob("*")` + `path.is_file()` (the ORIGINAL implementation) follows
    symlinks by default, both hashing and zipping whatever the link resolves to --
    letting a symlink change what's hashed/zipped without that content actually
    living inside the git-tracked `skill/` directory. `path.is_symlink()` is
    checked explicitly and any symlink is skipped entirely (rather than merely
    resolved-and-containment-checked), the simplest correct fix: this repo's
    skill directories are plain files (`SKILL.md`, `sources.md`, ...), so there is
    no legitimate reason for a symlink to appear inside one."""
    for path in sorted(skill_source_dir.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_file() and path.name != ".DS_Store":
            yield path


def _compute_skill_content_hash(skill_source_dir: Path) -> str:
    """A stable SHA-256 hash over a candidate-owned `skill/` directory's file
    contents (path + bytes, sorted for determinism) -- lets the sync script detect
    "has this candidate's skill source changed since I last pushed it" purely from
    LOCAL files, with NO network call. Recorded as a `content_hash` field alongside
    the pinned `skill_id`/`version` in the candidate's own `skills.json` -- an extra
    field on an ALREADY-tracked file (not a new, separate bespoke side-file/index),
    consistent with Decision 2c's "no duplicate-of-git index" principle: this hash
    is itself derived from, and travels with, the git-tracked `skill/` content, it
    doesn't duplicate anything git doesn't already version."""
    import hashlib

    hasher = hashlib.sha256()
    for path in _iter_skill_source_files(skill_source_dir):
        hasher.update(str(path.relative_to(skill_source_dir)).encode("utf-8"))
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _zip_skill_source(skill_source_dir: Path, dest_zip_path: Path) -> Path:
    """Zip a candidate-owned `skill/` directory into the single archive shape BOTH
    the Skills API's creation endpoint (`POST /v1/skills`) AND its version-push
    endpoint (`POST /v1/skills/{id}/versions`) require: every entry sitting inside
    ONE top-level folder, whose name matches `SKILL.md`'s own `name:` front-matter
    field EXACTLY.

    CONFIRMED LIVE (2026-07-06, agent-system-redesign epic Phase 3 -- two real,
    deliberate probes, NOT assumed): a first attempt at a version push using a
    flattened zip (no wrapping folder -- the shape this function used to produce,
    before this fix) failed with a REAL 400:
    `"Zip must contain a top-level folder with all files inside it, including
    SKILL.md"` -- proving the version-push endpoint enforces the identical
    constraint the CREATION endpoint does, contrary to this module's original
    (wrong) assumption that they differed. A follow-up probe with a
    deliberately-MISMATCHED folder name against the version-push endpoint then
    confirmed the SAME folder-name-must-match-SKILL.md's-name check applies there
    too: `"The folder name '<x>' must match the skill name '<y>' in SKILL.md."`
    (`deploy/managed-agent/README.md`'s own documented version-push zip command,
    `zip -r -q ... daily-ai-brief -x "*.DS_Store"`, run from ONE DIRECTORY ABOVE
    the `daily-ai-brief/` folder, already produced exactly this wrapping-folder
    shape by construction -- this function's ORIGINAL flattening behavior was
    simply never exercised against the real API before now, per Phase 2's own
    README note that no real Skill resource was created during that phase's
    development.) This one function is therefore now used for BOTH
    `create_skill()` and `create_skill_version()` -- there is no genuine
    difference between the two endpoints' zip-shape requirements after all.

    Uses `_iter_skill_source_files()` (shared with `_compute_skill_content_hash()`)
    so both agree on exactly which files are zipped/hashed -- including skipping
    symlinks, so a symlink inside `skill/` cannot smuggle in content from outside
    `skill_source_dir` (security hardening; see that function's docstring)."""
    import zipfile

    skill_name = _read_skill_name_from_front_matter(skill_source_dir)
    with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _iter_skill_source_files(skill_source_dir):
            arcname = Path(skill_name) / path.relative_to(skill_source_dir)
            zf.write(path, arcname=str(arcname))
    return dest_zip_path


def _push_candidate_skill_if_needed(candidate: CandidateDeclaration, skills_client: httpx.Client, result: SyncResult) -> None:
    """First-sync-only: if the candidate owns its own skill source (a `skill/`
    subdirectory), push it and record the result into `skills.json` IMMEDIATELY --
    BEFORE agent creation is attempted. This ordering is exactly what makes a partial
    failure (skill pushed, then agent creation fails) resumable: on retry,
    `skills.json` already has the pinned id/version, so this function finds nothing
    left to push and does nothing (no duplicate skill resource or version).

    Two distinct cases, both first-sync-only:
      1. NO `skill_id` recorded anywhere in `skills.json` yet (an empty list, or an
         entry naming a not-yet-created skill with no `skill_id` field at all) -- a
         genuinely BRAND-NEW candidate-owned skill. Calls `api_client.create_skill()`
         (`POST /v1/skills`), which mints BOTH the `skill_id` and its first version in
         one call, and records both into `skills.json`.
      2. An entry WITH a `skill_id` but no `version` pinned yet -- the candidate
         references an ALREADY-EXISTING Skills-API resource and this is its first
         version push from this candidate. Calls `api_client.create_skill_version()`
         (`POST /v1/skills/{id}/versions`) as before, unchanged.
    If neither case applies (skills.json already has every entry fully pinned with
    both `skill_id` and `version`, or there's no candidate-owned `skill/` directory at
    all), this is a no-op."""
    if candidate.skill_source_dir is None:
        return

    existing_skills = candidate.agent.skills

    existing_skill_id = None
    for entry in existing_skills:
        if entry.get("skill_id") and not entry.get("version"):
            existing_skill_id = entry["skill_id"]
            break

    if existing_skill_id is not None:
        import tempfile

        content_hash = _compute_skill_content_hash(candidate.skill_source_dir)
        with tempfile.TemporaryDirectory() as tmp_dir:
            zip_path = _zip_skill_source(candidate.skill_source_dir, Path(tmp_dir) / "skill.zip")
            response = api_client.create_skill_version(skills_client, existing_skill_id, str(zip_path))

        new_version = response.get("version") or response.get("id")
        updated_skills = [
            (
                {**entry, "version": new_version, "content_hash": content_hash}
                if entry.get("skill_id") == existing_skill_id
                else entry
            )
            for entry in existing_skills
        ]
        write_skills_json(candidate.skills_json_path, updated_skills)
        result.skill_versions_pushed.append(f"{existing_skill_id} -> version {new_version}")
        return

    any_skill_id_already_present = any(entry.get("skill_id") for entry in existing_skills)
    if any_skill_id_already_present:
        # Every declared skill already carries both a skill_id AND a version --
        # nothing left to push (already fully pushed in a prior, completed run).
        return

    # No skill_id anywhere yet -- a genuinely brand-new candidate-owned skill. Create
    # the resource (and its first version) in one call.
    import tempfile

    content_hash = _compute_skill_content_hash(candidate.skill_source_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = _zip_skill_source(candidate.skill_source_dir, Path(tmp_dir) / "skill.zip")
        response = api_client.create_skill(skills_client, str(zip_path))

    new_skill_id = api_client.require_field(response, "id", context="creating a brand-new candidate-owned skill")
    new_version = response.get("version") or response.get("latest_version")
    # "type": "custom" matches the confirmed production agent.json skills[] entry
    # shape (deploy/managed-agent/agent.json:27) -- the value POST /v1/agents expects
    # in an agent's `skills` field for a Skills-API-backed (non-Anthropic-builtin) skill.
    # `content_hash` (see _compute_skill_content_hash()'s docstring) lets a LATER
    # update-in-place sync detect a further skill-source change with no network call.
    write_skills_json(
        candidate.skills_json_path,
        [{"type": "custom", "skill_id": new_skill_id, "version": new_version, "content_hash": content_hash}],
    )
    result.skill_versions_pushed.append(f"{new_skill_id} (new) -> version {new_version}")


def _push_updated_candidate_skill_if_changed(
    candidate: CandidateDeclaration, skills_client: httpx.Client, result: SyncResult
) -> bool:
    """UPDATE-path counterpart to `_push_candidate_skill_if_needed()` (which only
    ever runs on a candidate's FIRST sync): if this candidate owns a `skill/`
    directory whose content has changed since the LAST push (detected via the
    `content_hash` recorded in `skills.json` by the function above -- a purely
    LOCAL, no-network-call comparison), push a new Skills-API version
    (`POST /v1/skills/{id}/versions`) and update `skills.json`'s `version` +
    `content_hash` fields to match.

    Returns True if a new version was actually pushed (so the caller knows the
    agent's OWN declaration -- which embeds `skills.json`'s pinned version via
    `to_agent_body()` -- now differs from what's live, and must be updated in
    place too, per Decision 2c: a skill-content change reaches a running candidate
    only once the referencing agent is ALSO updated to point at the new version).
    False if there was nothing to push (no `skill/` directory, no pinned skill yet
    -- this only applies post-first-sync, so that shouldn't normally happen --
    the content hash is unchanged, OR no `content_hash` was ever recorded at all --
    see below for why that last case is deliberately "skip," not "changed").

    CORRECTED (reviewer, README/code mismatch): a missing `content_hash` (e.g. a
    candidate first synced before this field existed) must be treated as "cannot
    determine, skip" -- NOT "changed" -- per this module's own stated design (and
    README.md's "Skill-content changes on an update sync" section): pushing a
    surprise new skill version the next time an already-synced, unedited candidate
    happens to be re-synced would be wrong. The ORIGINAL code
    (`if current_hash == pinned_entry.get("content_hash"): return False`) did not
    actually implement this: `.get(...)` returns `None` when the field is absent,
    and a real SHA-256 hex digest can never equal `None`, so the comparison was
    ALWAYS unequal on a missing hash -- silently pushing a version every time,
    exactly the surprise-push behavior this design exists to avoid. Fixed with an
    explicit early check below."""
    if candidate.skill_source_dir is None:
        return False

    existing_skills = candidate.agent.skills
    pinned_entry = next((entry for entry in existing_skills if entry.get("skill_id") and entry.get("version")), None)
    if pinned_entry is None:
        return False

    recorded_hash = pinned_entry.get("content_hash")
    if recorded_hash is None:
        # No content_hash recorded at all -- cannot determine whether skill/ changed
        # since we have no prior hash to compare against. Deliberately "skip," not
        # "changed" (see the corrected docstring above).
        return False

    current_hash = _compute_skill_content_hash(candidate.skill_source_dir)
    if current_hash == recorded_hash:
        return False

    skill_id = pinned_entry["skill_id"]
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = _zip_skill_source(candidate.skill_source_dir, Path(tmp_dir) / "skill.zip")
        response = api_client.create_skill_version(skills_client, skill_id, str(zip_path))

    new_version = response.get("version") or response.get("id")
    updated_skills = [
        ({**entry, "version": new_version, "content_hash": current_hash} if entry.get("skill_id") == skill_id else entry)
        for entry in existing_skills
    ]
    write_skills_json(candidate.skills_json_path, updated_skills)
    result.skill_versions_pushed.append(f"{skill_id} -> version {new_version}")
    return True


def _create_agent(agents_client: httpx.Client, declaration: AgentDeclaration, *, multiagent: dict[str, Any] | None) -> str:
    body = declaration.to_agent_body()
    if multiagent is not None:
        body["multiagent"] = multiagent
    created = api_client.create_agent(agents_client, body)
    return created["id"]


def _normalize_live_field_for_comparison(field_name: str, live_value: Any) -> Any:
    """Normalize ONE field of a live `GET /v1/agents/{id}` response into the same
    shape `AgentDeclaration.to_agent_body()` sends on write, so `_live_declaration_
    differs()` compares like with like.

    CORRECTED (agent-system-redesign epic, Phase 5, found on the FIRST real
    production-baseline sync -- not assumed, not caught by the mocked test suite):
    `model` is a field this repo's write-side (`to_agent_body()`) and read-side (a
    live GET) disagree on in SHAPE, not just value. `to_agent_body()` sends `model`
    as a plain string (`"claude-sonnet-5"`, per `model.txt`), which `POST
    /v1/agents` accepts -- but a live `GET /v1/agents/{id}` echoes `model` back as
    a NESTED OBJECT, `{"id": "claude-sonnet-5", "speed": "standard"}` (confirmed
    live on BOTH the newly-created `production-baseline` candidate AND,
    independently, the real live production `daily-ai-brief-agent`
    (`agent_01EswBTose8dnTAUDbGvzdLq`) -- so this is a universal API behavior, not
    a fluke of one agent). Comparing the raw values directly
    (`{"id": "claude-sonnet-5", ...} != "claude-sonnet-5"`) is therefore ALWAYS
    unequal, even when the model genuinely hasn't changed -- silently breaking
    FR-12/AC-12's "an unchanged declaration is a full no-op at the mutation level"
    guarantee for `model` specifically: EVERY re-sync of EVERY existing candidate
    (this bug predates Phase 5; it was simply never exercised against the real API
    until now, since the mocked test fixtures for the "unchanged" case
    (`tests/test_sync.py`'s `_agent_response(..., model=model)`) echoed `model` back
    as the SAME plain string the loader produces, not the real nested-object
    shape) would have silently issued a spurious `POST /v1/agents/{id}` update on
    every single sync, forever -- never a hard failure, just an unnecessary
    mutation call and an unwanted version bump on Claude Platform's own side. This
    function extracts `live_value["id"]` when `live_value` is a dict with an `id`
    key (the confirmed live shape), so the comparison in
    `_live_declaration_differs()` below is between two plain model-id strings on
    BOTH sides -- correcting the comparison, not the request body `to_agent_body()`
    sends (that string-only shape is what the write side of the API actually
    requires; only the READ side nests it).

    CHECKED, NOT ASSUMED (reviewer follow-up, Phase 5): whether `tools`,
    `mcp_servers`, or `skills` -- each a richer, nested shape than `model` where a
    live GET could plausibly echo back filled-in defaults, reordered keys, or
    extra fields the write side never sent -- have the SAME class of read-shape-
    vs-write-shape mismatch was an open question this docstring's earlier revision
    left unchecked (its "model is THE ONE FIELD" phrasing overclaimed a guarantee
    that was never actually verified for these three). A live, field-by-field
    diff of a fresh `GET /v1/agents/{id}` against `to_agent_body()`'s own output
    was performed for BOTH `production-baseline` (the newly-created candidate)
    AND, independently and read-only, the real live production agent
    (`agent_01EswBTose8dnTAUDbGvzdLq`) -- confirming `tools`, `mcp_servers`, and
    `skills` are each structurally IDENTICAL on read vs. write for both agents (no
    reordering, no filled-in defaults, no extra/missing fields). `model` remains
    the only field confirmed to need normalization; this function's `if` branch is
    therefore still correctly scoped to `model` alone, not a sign of an
    incomplete fix -- see `tests/test_sync.py`'s
    `test_update_is_a_full_no_op_using_the_real_confirmed_live_tools_and_mcp_servers_shapes`
    for the regression test pinning this confirmed-identical shape, using the
    REAL live-observed `tools`/`mcp_servers` values captured during this
    check (not synthetic placeholders)."""
    if field_name == "model" and isinstance(live_value, dict):
        return live_value.get("id", live_value)
    return live_value


def _live_declaration_differs(live_agent: dict[str, Any], declaration: AgentDeclaration) -> bool:
    """Compare the CURRENT live agent (from a fresh GET) against the local
    declaration on exactly the fields the local declaration controls. This is the
    sync script's own "did this change" check -- performed in addition to (not
    instead of) the platform's own no-op detection on update, so an unchanged
    declaration never even attempts an update call (per the task brief: "Only call
    update for a genuinely changed declaration -- an unchanged one is a no-op at the
    script level, not just relying on the platform's own no-op detection").

    Each live field is normalized via `_normalize_live_field_for_comparison()`
    before comparing -- see that function's docstring for the real, live-confirmed
    `model` shape mismatch this closes (Phase 5's production-baseline sync)."""
    local_body = declaration.to_agent_body()
    for field_name, local_value in local_body.items():
        live_value = _normalize_live_field_for_comparison(field_name, live_agent.get(field_name))
        if live_value != local_value:
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
        retry_version = api_client.require_field(current, "version", context=f"re-reading agent {agent_id} after a 409")
        api_client.update_agent(agents_client, agent_id, version=retry_version, agent_definition=body)


def _build_multiagent_config(candidate: CandidateDeclaration, sub_agent_ids: list[str | None]) -> dict[str, Any]:
    """Build the coordinator's `multiagent` field from the candidate's
    `multiagent.json` roster, substituting each sub-agent's live id (freshly created
    or already-known) so the coordinator always references real agent ids."""
    assert candidate.multiagent_json is not None
    roster = candidate.multiagent_json.get("agents", [])
    agents_field: list[dict[str, Any]] = []
    for index, roster_entry in enumerate(roster):
        # Platform-confirmed shape (platform.claude.com/docs/en/managed-agents/multi-agent):
        # each roster item is DIRECTLY the reference object -- `{"type":"agent","id":<id>}` --
        # with NO `entry` wrapper, type == "agent", and the id field named "id". An optional
        # per-entry `version` pins a specific sub-agent version; omitting it pins to the
        # sub-agent's latest version at coordinator create/update time.
        ref: dict[str, Any] = {"type": "agent", "id": sub_agent_ids[index]}
        version = roster_entry.get("version")
        if version is not None:
            ref["version"] = version
        agents_field.append(ref)
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
        _update_multi_agent(candidate_dir, candidate, agents_client, skills_client, result)
    else:
        _update_single_agent(candidate_dir, candidate, agents_client, skills_client, result)
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

    # Resumability (the bug this fixes): a PRIOR, partially-failed first-sync attempt
    # may already have created some sub-agent(s) and written their id(s) into
    # multiagent.json's roster (candidate.json's agent_id is still None, though, or
    # we wouldn't be in _first_sync at all -- that's exactly the "coordinator create
    # failed after sub-agent create succeeded" scenario). Re-running must NOT
    # recreate a sub-agent that already has an id -- same "don't recreate what's
    # already there" discipline the update path applies -- or the original resource
    # becomes a permanently orphaned duplicate (no delete/archive primitive exists).
    sub_agent_ids: list[str | None] = []
    newly_created_by_index: dict[int, str] = {}
    for index, sub_agent in enumerate(candidate.sub_agents):
        if sub_agent.agent_id is not None:
            # Already created in a prior attempt -- reuse it, don't recreate.
            sub_agent_ids.append(sub_agent.agent_id)
            continue
        agent_id = _create_agent(agents_client, sub_agent, multiagent=None)
        sub_agent_ids.append(agent_id)
        newly_created_by_index[index] = agent_id
        result.created.append(f"sub-agent[{index}] {sub_agent.name or '(unnamed)'}")
    if newly_created_by_index:
        write_sub_agent_ids(candidate.multiagent_json_path, newly_created_by_index)

    multiagent_config = _build_multiagent_config(candidate, sub_agent_ids)
    coordinator_id = _create_agent(agents_client, candidate.agent, multiagent=multiagent_config)
    result.created.append(f"agent (coordinator) {candidate.agent.name or '(unnamed)'}")
    write_candidate_agent_id(candidate.candidate_json_path, coordinator_id)


def _update_single_agent(
    candidate_dir: Path,
    candidate: CandidateDeclaration,
    agents_client: httpx.Client,
    skills_client: httpx.Client,
    result: SyncResult,
) -> None:
    skill_changed = _push_updated_candidate_skill_if_changed(candidate, skills_client, result)
    if skill_changed:
        # Re-load: the skill push rewrote skills.json's pinned version, and the
        # agent-update body below (to_agent_body()) must send the FRESH pinned
        # version, not the stale in-memory one -- otherwise the agent would be
        # re-pinned right back to the OLD skill version, defeating the whole point
        # (this is the direct mechanism that closes the ADR-0008 image-rebuild
        # failure mode: here, a skill push is followed through to an agent update
        # that actually references it, not left as a dangling Skills-API-only push).
        candidate = load_candidate(candidate_dir)

    assert candidate.agent.agent_id is not None
    live_agent = api_client.get_agent(agents_client, candidate.agent.agent_id)
    if _live_declaration_differs(live_agent, candidate.agent):
        current_version = api_client.require_field(
            live_agent, "version", context=f"reading agent {candidate.agent.agent_id} before an update"
        )
        _update_agent_with_retry(
            agents_client,
            candidate.agent.agent_id,
            candidate.agent,
            multiagent=None,
            current_version=current_version,
        )
        result.updated.append(f"agent {candidate.agent.name or '(unnamed)'}")
    elif not result.skill_versions_pushed:
        result.no_op = True


def _update_multi_agent(
    candidate_dir: Path,
    candidate: CandidateDeclaration,
    agents_client: httpx.Client,
    skills_client: httpx.Client,
    result: SyncResult,
) -> None:
    skill_changed = _push_updated_candidate_skill_if_changed(candidate, skills_client, result)
    if skill_changed:
        # Same re-load discipline as the single-agent path above -- the coordinator's
        # (or a sub-agent's) OWN skills.json pin must reflect the freshly-pushed
        # version before either is compared/updated below.
        candidate = load_candidate(candidate_dir)

    # Step (i): update whichever sub-agent(s) changed, FIRST -- and ONLY those.
    any_sub_agent_updated = False
    current_sub_agent_ids: list[str | None] = []
    for index, sub_agent in enumerate(candidate.sub_agents):
        assert sub_agent.agent_id is not None
        current_sub_agent_ids.append(sub_agent.agent_id)
        live_sub_agent = api_client.get_agent(agents_client, sub_agent.agent_id)
        if _live_declaration_differs(live_sub_agent, sub_agent):
            current_version = api_client.require_field(
                live_sub_agent, "version", context=f"reading sub-agent {sub_agent.agent_id} before an update"
            )
            _update_agent_with_retry(
                agents_client,
                sub_agent.agent_id,
                sub_agent,
                multiagent=None,
                current_version=current_version,
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
        current_version = api_client.require_field(
            live_coordinator, "version", context=f"reading coordinator {candidate.agent.agent_id} before an update"
        )
        _update_agent_with_retry(
            agents_client,
            candidate.agent.agent_id,
            candidate.agent,
            multiagent=multiagent_config,
            current_version=current_version,
        )
        result.updated.append(f"agent (coordinator) {candidate.agent.name or '(unnamed)'}")

    if not result.updated and not result.skill_versions_pushed:
        result.no_op = True
