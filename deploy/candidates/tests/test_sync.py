"""Tests for candidate_sync.sync -- the core sync algorithm (Decision 2c, PRD
FR-11/FR-12, AC-11/AC-12). Every Anthropic API interaction is mocked via
`FakeHttpxClient` (this repo's own established fake-client pattern, see
`fake_httpx_client.py`'s docstring for why this was chosen over `httpx.MockTransport`
or `respx`) -- NO real network calls, NO real Agent/Skill resource is ever created.

Covers, per the task brief's required scenarios:
  - first-sync create (single-agent)
  - first-sync create (multi-agent: both coordinator and sub-agent(s) created, ids
    written to the right files)
  - update-in-place on a changed declaration
  - the 409-then-retry path
  - the ordered two-step multi-agent update (sub-agent(s) then coordinator, in that
    order -- asserted via the mock's recorded call order, not just "both happened")
  - the full no-op path (unchanged declaration -> zero create/update calls)
  - partial-failure resumption (skill-push succeeds, agent-creation then fails, then
    re-running picks up correctly without re-pushing the skill)
  - partial-failure resumption where sub-agent creation succeeds but the SUBSEQUENT
    coordinator creation fails -- a retry must reuse the already-created sub-agent
    (not recreate a second, orphaned duplicate) and issue exactly one more
    POST /v1/agents call (the coordinator only)
"""

from __future__ import annotations

import json
import shutil

import httpx
import pytest

from candidate_sync.sync import _compute_skill_content_hash, _iter_skill_source_files, _zip_skill_source, sync_candidate

from conftest import FIXTURES_DIR
from fake_httpx_client import FakeHttpxClient, FakeResponse

# Computed directly from the ACTUAL (unmodified) multi-agent fixture's skill/
# directory -- used by tests that need to represent "this candidate's skill was
# already pushed and is UNCHANGED," so they exercise the agent-update logic they're
# actually testing without ALSO (accidentally) exercising the skill-push path.
_FIXTURE_SKILL_CONTENT_HASH = _compute_skill_content_hash(FIXTURES_DIR / "example-multi-agent" / "skill")

# Same idea, for the dedicated "brand-new candidate-owned skill" fixture below.
_FIXTURE_NEW_SKILL_CONTENT_HASH = _compute_skill_content_hash(FIXTURES_DIR / "example-single-agent-new-skill" / "skill")


def _agent_response(agent_id: str, *, version: int = 1, **overrides) -> FakeResponse:
    body = {"id": agent_id, "version": version, **overrides}
    return FakeResponse(200, body)


@pytest.fixture
def single_agent_dir(tmp_path):
    candidate_dir = tmp_path / "example-single-agent"
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)
    return candidate_dir


@pytest.fixture
def single_agent_new_skill_dir(tmp_path):
    """A single-agent candidate whose skills.json starts EMPTY (no skill_id at all
    yet) -- exercises api_client.create_skill() (POST /v1/skills), the genuinely
    NEW brand-new-skill-creation path this phase added (distinct from
    create_skill_version(), an existing skill's version push)."""
    candidate_dir = tmp_path / "example-single-agent-new-skill"
    shutil.copytree(FIXTURES_DIR / "example-single-agent-new-skill", candidate_dir)
    return candidate_dir


@pytest.fixture
def multi_agent_dir(tmp_path):
    candidate_dir = tmp_path / "example-multi-agent"
    shutil.copytree(FIXTURES_DIR / "example-multi-agent", candidate_dir)
    return candidate_dir


# --- First sync (create): single-agent -------------------------------------------


def test_first_sync_single_agent_creates_and_writes_agent_id(single_agent_dir):
    agents_client = FakeHttpxClient()
    agents_client.when("POST", "/v1/agents", _agent_response("agent_SINGLE_NEW", version=1))
    skills_client = FakeHttpxClient()

    result = sync_candidate(single_agent_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.created == ["agent example-single-agent-EXAMPLE"]
    assert not result.updated
    assert not result.no_op

    updated_candidate_json = json.loads((single_agent_dir / "candidate.json").read_text())
    assert updated_candidate_json["agent_id"] == "agent_SINGLE_NEW"
    # No skill/ subdirectory in the single-agent fixture -> no skill-version push.
    assert agents_client.call_signature() == [("POST", "/v1/agents")]


def test_first_sync_single_agent_sends_correct_body(single_agent_dir):
    agents_client = FakeHttpxClient()
    agents_client.when("POST", "/v1/agents", _agent_response("agent_SINGLE_NEW"))
    skills_client = FakeHttpxClient()

    sync_candidate(single_agent_dir, agents_client=agents_client, skills_client=skills_client)

    sent_body = agents_client.calls[0].kwargs["json"]
    assert sent_body["model"] == "claude-example-model"
    assert "EXAMPLE SYSTEM PROMPT" in sent_body["system"]
    assert sent_body["tools"] == [{"type": "agent_toolset_20260401"}]
    assert sent_body["parameters"] == {"effort": "example-low", "thinking_budget_tokens": 1}
    assert "multiagent" not in sent_body
    assert "version" not in sent_body  # create never sends a version


# --- First sync (create): multi-agent ---------------------------------------------


def test_first_sync_multi_agent_creates_sub_agent_then_coordinator_and_writes_both_ids(multi_agent_dir):
    agents_client = FakeHttpxClient()
    agents_client.when("POST", "/v1/agents", _agent_response("agent_SUB_NEW"), _agent_response("agent_COORD_NEW"))
    skills_client = FakeHttpxClient()
    skills_client.when(
        "POST",
        "/v1/skills/skill_EXAMPLE_NOT_REAL/versions",
        FakeResponse(200, {"id": "skill_EXAMPLE_NOT_REAL", "version": 1783096569199829}),
    )

    result = sync_candidate(multi_agent_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.created == [
        "sub-agent[0] example-sub-agent-researcher-EXAMPLE",
        "agent (coordinator) example-multi-agent-coordinator-EXAMPLE",
    ]
    assert result.skill_versions_pushed == ["skill_EXAMPLE_NOT_REAL -> version 1783096569199829"]

    # The sub-agent's id is written into multiagent.json's roster...
    updated_multiagent = json.loads((multi_agent_dir / "multiagent.json").read_text())
    assert updated_multiagent["agents"][0]["agent_id"] == "agent_SUB_NEW"
    # ...and the coordinator's id is written into candidate.json.
    updated_candidate = json.loads((multi_agent_dir / "candidate.json").read_text())
    assert updated_candidate["agent_id"] == "agent_COORD_NEW"

    # Sub-agent MUST be created before the coordinator (the coordinator's create body
    # needs to reference the sub-agent's newly-minted id).
    assert agents_client.call_signature() == [("POST", "/v1/agents"), ("POST", "/v1/agents")]
    sub_agent_create_body = agents_client.calls[0].kwargs["json"]
    coordinator_create_body = agents_client.calls[1].kwargs["json"]
    assert sub_agent_create_body["model"] == "claude-example-sub-agent-model"
    assert "multiagent" not in sub_agent_create_body
    assert coordinator_create_body["model"] == "claude-example-coordinator-model"
    assert coordinator_create_body["multiagent"]["type"] == "coordinator"
    # Platform-confirmed roster shape: each agents[] item is DIRECTLY {"type":"agent","id":<id>}
    # (no `entry` wrapper) -- platform.claude.com/docs/en/managed-agents/multi-agent.
    assert coordinator_create_body["multiagent"]["agents"][0] == {"type": "agent", "id": "agent_SUB_NEW"}

    # The skill was pushed on the skills client, not the agents client.
    skill_push_calls = [c for c in skills_client.calls if c.path.startswith("/v1/skills/")]
    assert len(skill_push_calls) == 1

    # skills.json now carries the concrete pinned version (not a bare skill_id) AND
    # a content_hash of the pushed skill/ directory, so a LATER update-sync can
    # detect a further skill-content change with no network call.
    updated_skills_json = json.loads((multi_agent_dir / "skills.json").read_text())
    assert updated_skills_json == [
        {"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1783096569199829, "content_hash": _FIXTURE_SKILL_CONTENT_HASH}
    ]


# --- First sync (create): a BRAND-NEW candidate-owned skill (no skill_id at all yet) --
# Phase 3 addition: distinct from the multi-agent test above, which references an
# ALREADY-EXISTING skill_id (skill_EXAMPLE_NOT_REAL) and only pushes a VERSION to it.
# This covers api_client.create_skill() (POST /v1/skills) -- a genuinely new resource.


def test_first_sync_creates_a_brand_new_skill_resource_via_post_v1_skills(single_agent_new_skill_dir):
    agents_client = FakeHttpxClient()
    agents_client.when("POST", "/v1/agents", _agent_response("agent_NEW_SKILL_EXAMPLE"))
    skills_client = FakeHttpxClient()
    skills_client.when("POST", "/v1/skills", FakeResponse(200, {"id": "skill_BRAND_NEW", "version": 1783337264004829}))

    result = sync_candidate(single_agent_new_skill_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.skill_versions_pushed == ["skill_BRAND_NEW (new) -> version 1783337264004829"]
    assert result.created == ["agent example-single-agent-new-skill-EXAMPLE"]

    # The skill push hit the TOP-LEVEL /v1/skills collection, NOT a /versions
    # sub-resource -- confirming create_skill(), not create_skill_version(), was used.
    assert skills_client.call_signature() == [("POST", "/v1/skills")]

    # skills.json now carries the newly-minted skill_id, its version, AND a
    # content_hash -- the "type": "custom" field matches the confirmed production
    # agent.json skills[] entry shape.
    updated_skills_json = json.loads((single_agent_new_skill_dir / "skills.json").read_text())
    assert updated_skills_json == [
        {
            "type": "custom",
            "skill_id": "skill_BRAND_NEW",
            "version": 1783337264004829,
            "content_hash": _FIXTURE_NEW_SKILL_CONTENT_HASH,
        }
    ]

    # The agent-create body references the freshly-pinned skill (re-loaded AFTER the
    # skill push, per _first_sync()'s re-load discipline) -- confirming the create
    # call actually used the NEW skill_id/version, not a stale in-memory value.
    agent_create_body = agents_client.calls[0].kwargs["json"]
    assert agent_create_body["skills"] == [{"type": "custom", "skill_id": "skill_BRAND_NEW", "version": 1783337264004829}]
    # content_hash must NEVER be sent to the Agents API -- it's a local-only
    # bookkeeping field (to_agent_body() strips it).
    assert "content_hash" not in agent_create_body["skills"][0]


def test_first_sync_new_skill_failure_does_not_repush_on_retry(single_agent_new_skill_dir):
    """Partial-failure resumption for the BRAND-NEW-skill path: the skill push
    succeeds and skills.json is updated, but agent creation then fails. Re-running
    must NOT re-create a second, duplicate skill resource (skills.json already
    carries the pinned skill_id/version by then)."""
    agents_client_first_attempt = FakeHttpxClient()
    agents_client_first_attempt.when("POST", "/v1/agents", FakeResponse(500, {"error": "simulated failure"}))
    skills_client = FakeHttpxClient()
    skills_client.when("POST", "/v1/skills", FakeResponse(200, {"id": "skill_BRAND_NEW", "version": 1783337264004829}))

    with pytest.raises(httpx.HTTPStatusError):
        sync_candidate(single_agent_new_skill_dir, agents_client=agents_client_first_attempt, skills_client=skills_client)

    assert skills_client.call_signature() == [("POST", "/v1/skills")]
    updated_skills_json = json.loads((single_agent_new_skill_dir / "skills.json").read_text())
    assert updated_skills_json[0]["skill_id"] == "skill_BRAND_NEW"

    # --- Re-run, simulating a fixed/retried invocation ---
    agents_client_retry = FakeHttpxClient()
    agents_client_retry.when("POST", "/v1/agents", _agent_response("agent_NEW_SKILL_EXAMPLE"))
    skills_client_retry = FakeHttpxClient()  # deliberately given NO scripted skill-creation response

    result = sync_candidate(single_agent_new_skill_dir, agents_client=agents_client_retry, skills_client=skills_client_retry)

    assert result.created == ["agent example-single-agent-new-skill-EXAMPLE"]
    # The retry made ZERO calls against the skills client -- confirming a second
    # skill resource was NOT created (skills_client_retry has no registered handler
    # at all; if the sync script had tried to create again, FakeHttpxClient.post()
    # would have raised "no scripted response registered", which it did not).
    assert skills_client_retry.calls == []


# --- Update-in-place: a candidate-owned skill's CONTENT changes (Phase 3, AC-5) ------
# The direct, sharp proof mechanism for AC-5 ("a Skills-API push alone must reach a
# running candidate, no image rebuild"): a LOCAL skill/ content edit is detected via
# content_hash (no network call), a new Skills-API version is pushed, and the
# REFERENCING AGENT is then updated in place to point at the new version -- all
# without recreating the agent_id.


def test_update_pushes_new_skill_version_when_content_hash_differs_and_updates_agent(single_agent_new_skill_dir):
    # Simulate: this candidate was ALREADY synced once (agent_id + a pinned skill
    # version + the ORIGINAL content_hash all present)...
    _mark_synced(single_agent_new_skill_dir, agent_id="agent_ALREADY_SYNCED")
    original_skills_json = [
        {"type": "custom", "skill_id": "skill_EXISTING", "version": 1783096569199829, "content_hash": "STALE_HASH_FROM_BEFORE_THE_EDIT"}
    ]
    (single_agent_new_skill_dir / "skills.json").write_text(json.dumps(original_skills_json))

    # ...then the skill/ directory's ACTUAL content changed (a real edit) -- the
    # fixture's real content_hash will now differ from "STALE_HASH_FROM_BEFORE_THE_EDIT".
    agents_client = FakeHttpxClient()
    skills_client = FakeHttpxClient()
    skills_client.when(
        "POST",
        "/v1/skills/skill_EXISTING/versions",
        FakeResponse(200, {"id": "skill_EXISTING", "version": 1783337264004829}),
    )
    # The agent's live state still references the OLD skill version -- differs from
    # the (freshly re-loaded, post-push) local declaration, which now references the
    # NEW version -- so an update call is expected.
    agent_json = json.loads((single_agent_new_skill_dir / "agent.json").read_text())
    model = (single_agent_new_skill_dir / "model.txt").read_text().strip()
    system_prompt = (single_agent_new_skill_dir / "system-prompt.md").read_text()
    parameters = json.loads((single_agent_new_skill_dir / "parameters.json").read_text())
    agents_client.when(
        "GET",
        "/v1/agents/agent_ALREADY_SYNCED",
        _agent_response(
            "agent_ALREADY_SYNCED",
            version=1,
            name=agent_json["name"],
            description=agent_json["description"],
            model=model,
            system=system_prompt,
            tools=agent_json["tools"],
            mcp_servers=agent_json["mcp_servers"],
            skills=[{"type": "custom", "skill_id": "skill_EXISTING", "version": 1783096569199829}],  # the OLD version
            parameters=parameters,
        ),
    )
    agents_client.when("POST", "/v1/agents/agent_ALREADY_SYNCED", _agent_response("agent_ALREADY_SYNCED", version=2))

    result = sync_candidate(single_agent_new_skill_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.skill_versions_pushed == ["skill_EXISTING -> version 1783337264004829"]
    assert result.updated == ["agent example-single-agent-new-skill-EXAMPLE"]
    assert not result.no_op

    # skills.json now carries the NEW version and the FRESH content_hash.
    updated_skills_json = json.loads((single_agent_new_skill_dir / "skills.json").read_text())
    assert updated_skills_json == [
        {
            "type": "custom",
            "skill_id": "skill_EXISTING",
            "version": 1783337264004829,
            "content_hash": _FIXTURE_NEW_SKILL_CONTENT_HASH,
        }
    ]

    # The agent-update body references the NEW skill version -- confirming the
    # agent was actually re-pinned, closing the ADR-0008 failure mode (a skill push
    # alone reaching the running candidate only once the referencing agent is ALSO
    # updated).
    update_body = agents_client.calls[1].kwargs["json"]
    assert update_body["skills"] == [{"type": "custom", "skill_id": "skill_EXISTING", "version": 1783337264004829}]
    # The agent_id itself is UNCHANGED -- no recreation, matching AC-5's "no image
    # rebuild, no agent recreation" requirement.
    assert json.loads((single_agent_new_skill_dir / "candidate.json").read_text())["agent_id"] == "agent_ALREADY_SYNCED"


def test_update_is_a_no_op_when_skill_content_hash_is_unchanged(single_agent_new_skill_dir):
    """The counterpart to the test above: if the skill/ directory's content_hash
    matches what's already pinned, NO skill push and NO agent update happen at
    all -- confirming the sync script doesn't push a spurious new version on every
    single sync just because a skill/ directory exists."""
    _mark_synced(single_agent_new_skill_dir, agent_id="agent_ALREADY_SYNCED")
    skills_json = [
        {
            "type": "custom",
            "skill_id": "skill_EXISTING",
            "version": 1783096569199829,
            "content_hash": _FIXTURE_NEW_SKILL_CONTENT_HASH,  # matches the fixture's ACTUAL, unchanged content
        }
    ]
    (single_agent_new_skill_dir / "skills.json").write_text(json.dumps(skills_json))

    agent_json = json.loads((single_agent_new_skill_dir / "agent.json").read_text())
    model = (single_agent_new_skill_dir / "model.txt").read_text().strip()
    system_prompt = (single_agent_new_skill_dir / "system-prompt.md").read_text()
    parameters = json.loads((single_agent_new_skill_dir / "parameters.json").read_text())

    agents_client = FakeHttpxClient()
    agents_client.when(
        "GET",
        "/v1/agents/agent_ALREADY_SYNCED",
        _agent_response(
            "agent_ALREADY_SYNCED",
            version=1,
            name=agent_json["name"],
            description=agent_json["description"],
            model=model,
            system=system_prompt,
            tools=agent_json["tools"],
            mcp_servers=agent_json["mcp_servers"],
            skills=[{"type": "custom", "skill_id": "skill_EXISTING", "version": 1783096569199829}],
            parameters=parameters,
        ),
    )
    skills_client = FakeHttpxClient()  # deliberately given NO scripted response -- must not be called at all

    result = sync_candidate(single_agent_new_skill_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.no_op is True
    assert not result.skill_versions_pushed
    assert not result.updated
    assert skills_client.calls == []
    # Only the one read-only GET (to detect "unchanged") was made against the agent.
    assert agents_client.call_signature() == [("GET", "/v1/agents/agent_ALREADY_SYNCED")]


def test_update_skips_skill_push_when_content_hash_was_never_recorded(single_agent_new_skill_dir):
    """Regression test for the reviewer-found README/code mismatch: a pinned skill
    entry with NO `content_hash` field at all (e.g. a candidate first synced before
    this field existed) must be treated as "cannot determine, skip" -- NOT
    "changed" -- so it does NOT trigger a surprise skill-version push. The
    ORIGINAL code compared `current_hash == pinned_entry.get("content_hash")`,
    which is `current_hash == None` when the field is absent -- ALWAYS unequal for
    a real hash, so it silently pushed a version every time. This test would have
    FAILED against that code (skills_client would have received an unscripted
    POST /v1/skills/{id}/versions call and raised)."""
    _mark_synced(single_agent_new_skill_dir, agent_id="agent_ALREADY_SYNCED")
    # Deliberately NO content_hash field at all -- distinct from the no-op test
    # above, which has a content_hash that happens to match.
    skills_json = [{"type": "custom", "skill_id": "skill_EXISTING", "version": 1783096569199829}]
    (single_agent_new_skill_dir / "skills.json").write_text(json.dumps(skills_json))

    agent_json = json.loads((single_agent_new_skill_dir / "agent.json").read_text())
    model = (single_agent_new_skill_dir / "model.txt").read_text().strip()
    system_prompt = (single_agent_new_skill_dir / "system-prompt.md").read_text()
    parameters = json.loads((single_agent_new_skill_dir / "parameters.json").read_text())

    agents_client = FakeHttpxClient()
    agents_client.when(
        "GET",
        "/v1/agents/agent_ALREADY_SYNCED",
        _agent_response(
            "agent_ALREADY_SYNCED",
            version=1,
            name=agent_json["name"],
            description=agent_json["description"],
            model=model,
            system=system_prompt,
            tools=agent_json["tools"],
            mcp_servers=agent_json["mcp_servers"],
            skills=[{"type": "custom", "skill_id": "skill_EXISTING", "version": 1783096569199829}],
            parameters=parameters,
        ),
    )
    skills_client = FakeHttpxClient()  # deliberately given NO scripted response -- must not be called at all

    result = sync_candidate(single_agent_new_skill_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.no_op is True
    assert not result.skill_versions_pushed
    assert not result.updated
    assert skills_client.calls == []
    # skills.json is left completely untouched -- no content_hash was written in
    # (that would happen only on an ACTUAL push, which correctly did not occur).
    assert json.loads((single_agent_new_skill_dir / "skills.json").read_text()) == skills_json


# --- Update-in-place: single-agent -------------------------------------------------


def test_update_single_agent_on_changed_declaration(single_agent_dir):
    _mark_synced(single_agent_dir, agent_id="agent_ALREADY_SYNCED")
    (single_agent_dir / "model.txt").write_text("claude-example-model-v2\n")

    agents_client = FakeHttpxClient()
    agents_client.when("GET", "/v1/agents/agent_ALREADY_SYNCED", _agent_response("agent_ALREADY_SYNCED", version=3, model="claude-example-model"))
    agents_client.when("POST", "/v1/agents/agent_ALREADY_SYNCED", _agent_response("agent_ALREADY_SYNCED", version=4))
    skills_client = FakeHttpxClient()

    result = sync_candidate(single_agent_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.updated == ["agent example-single-agent-EXAMPLE"]
    assert not result.no_op
    assert agents_client.call_signature() == [
        ("GET", "/v1/agents/agent_ALREADY_SYNCED"),
        ("POST", "/v1/agents/agent_ALREADY_SYNCED"),
    ]
    update_body = agents_client.calls[1].kwargs["json"]
    assert update_body["version"] == 3  # the version read from the fresh GET, not a cached/assumed value
    assert update_body["model"] == "claude-example-model-v2"


def test_update_single_agent_unchanged_declaration_is_a_full_no_op(single_agent_dir):
    """FR-12/AC-12: re-running against an unchanged declaration must make no create
    or update call at all -- the live agent (as returned by GET) already matches the
    local declaration exactly."""
    _mark_synced(single_agent_dir, agent_id="agent_ALREADY_SYNCED")
    candidate_json = json.loads((single_agent_dir / "candidate.json").read_text())
    agent_json = json.loads((single_agent_dir / "agent.json").read_text())
    model = (single_agent_dir / "model.txt").read_text().strip()
    system_prompt = (single_agent_dir / "system-prompt.md").read_text()
    parameters = json.loads((single_agent_dir / "parameters.json").read_text())

    agents_client = FakeHttpxClient()
    agents_client.when(
        "GET",
        "/v1/agents/agent_ALREADY_SYNCED",
        _agent_response(
            "agent_ALREADY_SYNCED",
            version=1,
            name=agent_json["name"],
            description=agent_json["description"],
            model=model,
            system=system_prompt,
            tools=agent_json["tools"],
            mcp_servers=agent_json["mcp_servers"],
            skills=[],
            parameters=parameters,
        ),
    )
    skills_client = FakeHttpxClient()

    result = sync_candidate(single_agent_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.no_op is True
    assert not result.created
    assert not result.updated
    # No create/update call was made -- the ONLY call made at all was the read-only
    # GET used to detect "unchanged" (see sync.py's module docstring for why a GET is
    # still necessary: Decision 2c's own algorithm requires knowing "changed since
    # last sync," and the live agent resource IS that record -- there is no bespoke
    # local side-file duplicating it).
    assert agents_client.call_signature() == [("GET", "/v1/agents/agent_ALREADY_SYNCED")]
    assert candidate_json["agent_id"] == "agent_ALREADY_SYNCED"  # unchanged, no rewrite needed


def test_update_is_a_full_no_op_against_the_REAL_live_nested_model_shape(single_agent_dir):
    """Regression test for a real bug this phase's live production-baseline sync
    found (agent-system-redesign epic, Phase 5) -- NOT caught by the test above,
    which (like every other "unchanged" fixture in this file) echoes `model` back
    as the SAME plain string `to_agent_body()` sends, not the shape the REAL
    Anthropic API actually returns.

    CONFIRMED LIVE (2026-07-06): a real `GET /v1/agents/{id}` -- both on the
    freshly-created `production-baseline` candidate AND, independently, on the
    real live production agent (`agent_01EswBTose8dnTAUDbGvzdLq`) -- returns
    `model` as a NESTED OBJECT, `{"id": "claude-sonnet-5", "speed": "standard"}`,
    never a bare string, even though `to_agent_body()` (correctly) SENDS a plain
    string on write. Before `_normalize_live_field_for_comparison()` existed,
    `_live_declaration_differs()` compared these two shapes directly
    (`{"id": ..., "speed": ...} != "claude-sonnet-5"`) and always found them
    unequal -- meaning EVERY re-sync of EVERY candidate in this repo would issue a
    spurious `POST /v1/agents/{id}` update, forever, even when nothing had
    actually changed, silently breaking FR-12/AC-12's "an unchanged declaration
    is a full no-op at the mutation level" guarantee. This test was confirmed,
    directly, to FAIL against the pre-fix code (asserting `result.no_op is True`
    failed; an update call was made) and PASS against the fix."""
    _mark_synced(single_agent_dir, agent_id="agent_ALREADY_SYNCED")
    agent_json = json.loads((single_agent_dir / "agent.json").read_text())
    model = (single_agent_dir / "model.txt").read_text().strip()
    system_prompt = (single_agent_dir / "system-prompt.md").read_text()
    parameters = json.loads((single_agent_dir / "parameters.json").read_text())

    agents_client = FakeHttpxClient()
    agents_client.when(
        "GET",
        "/v1/agents/agent_ALREADY_SYNCED",
        _agent_response(
            "agent_ALREADY_SYNCED",
            version=1,
            name=agent_json["name"],
            description=agent_json["description"],
            # The REAL, live-confirmed shape -- a nested object, NOT the bare
            # string model.txt holds. This is the one thing this test changes
            # versus the test above.
            model={"id": model, "speed": "standard"},
            system=system_prompt,
            tools=agent_json["tools"],
            mcp_servers=agent_json["mcp_servers"],
            skills=[],
            parameters=parameters,
        ),
    )
    skills_client = FakeHttpxClient()

    result = sync_candidate(single_agent_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.no_op is True
    assert not result.created
    assert not result.updated
    assert agents_client.call_signature() == [("GET", "/v1/agents/agent_ALREADY_SYNCED")]


def test_update_is_a_full_no_op_using_the_real_confirmed_live_tools_and_mcp_servers_shapes(single_agent_dir):
    """Reviewer follow-up (Phase 5): confirms `tools`/`mcp_servers`/`skills` do NOT
    share the `model`-field read-shape-vs-write-shape mismatch above -- checked
    directly against the REAL API, not assumed from the fixture's own synthetic
    placeholder values (which happen to already match what `to_agent_body()`
    sends, so they could never have caught a real mismatch either way).

    CONFIRMED LIVE (2026-07-06): a live, field-by-field diff of a fresh
    `GET /v1/agents/{id}` against `to_agent_body()`'s own output was performed for
    BOTH `production-baseline` (the newly-created Phase 5 candidate) AND,
    independently and read-only, the real live production agent
    (`agent_01EswBTose8dnTAUDbGvzdLq`) -- confirming `tools`, `mcp_servers`, and
    `skills` are each structurally IDENTICAL on read vs. write for both agents.
    This test pins that confirmed result using the REAL live-observed values
    (the actual production agent's `tools`/`mcp_servers`/`skills` shape, not a
    synthetic placeholder), so a future accidental change to
    `_normalize_live_field_for_comparison()` that started ALSO normalizing one of
    these three fields (masking a genuine future divergence, or introducing a
    spurious no-op where a real change should be detected) would be caught here."""
    _mark_synced(single_agent_dir, agent_id="agent_ALREADY_SYNCED")
    agent_json = json.loads((single_agent_dir / "agent.json").read_text())
    model = (single_agent_dir / "model.txt").read_text().strip()
    system_prompt = (single_agent_dir / "system-prompt.md").read_text()
    parameters = json.loads((single_agent_dir / "parameters.json").read_text())

    # The REAL, live-confirmed shape of the production agent's own tools/
    # mcp_servers/skills fields (captured 2026-07-06 via a real GET
    # /v1/agents/agent_01EswBTose8dnTAUDbGvzdLq) -- overriding the fixture's own
    # bare-bones synthetic tools/mcp_servers with these real, richer values so
    # this test genuinely exercises the real shape, not a placeholder that
    # happens to already match.
    real_tools = [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            "configs": [],
        }
    ]
    real_mcp_servers: list = []
    real_skills = [{"type": "custom", "skill_id": "skill_01H2qu83NwnJ5zqcbrqsCcJ6", "version": "1783340601977967"}]

    (single_agent_dir / "agent.json").write_text(
        json.dumps({**agent_json, "tools": real_tools, "mcp_servers": real_mcp_servers})
    )
    (single_agent_dir / "skills.json").write_text(json.dumps(real_skills))

    agents_client = FakeHttpxClient()
    agents_client.when(
        "GET",
        "/v1/agents/agent_ALREADY_SYNCED",
        _agent_response(
            "agent_ALREADY_SYNCED",
            version=1,
            name=agent_json["name"],
            description=agent_json["description"],
            model={"id": model, "speed": "standard"},  # the confirmed live model shape too
            system=system_prompt,
            tools=real_tools,
            mcp_servers=real_mcp_servers,
            skills=real_skills,
            parameters=parameters,
        ),
    )
    skills_client = FakeHttpxClient()

    result = sync_candidate(single_agent_dir, agents_client=agents_client, skills_client=skills_client)

    assert result.no_op is True
    assert not result.created
    assert not result.updated
    assert agents_client.call_signature() == [("GET", "/v1/agents/agent_ALREADY_SYNCED")]


# --- 409-then-retry -----------------------------------------------------------------


def test_update_retries_once_on_409_stale_version(single_agent_dir):
    _mark_synced(single_agent_dir, agent_id="agent_ALREADY_SYNCED")
    (single_agent_dir / "model.txt").write_text("claude-example-model-v2\n")

    agents_client = FakeHttpxClient()
    agents_client.when("GET", "/v1/agents/agent_ALREADY_SYNCED", _agent_response("agent_ALREADY_SYNCED", version=3, model="claude-example-model"))
    agents_client.when("POST", "/v1/agents/agent_ALREADY_SYNCED", FakeResponse(409, {"error": "stale version"}))
    # Re-read after the 409 returns a NEWER version than what was first fetched --
    # simulating someone/something else having updated the agent in between.
    agents_client.when("GET", "/v1/agents/agent_ALREADY_SYNCED", _agent_response("agent_ALREADY_SYNCED", version=5, model="claude-example-model"))
    agents_client.when("POST", "/v1/agents/agent_ALREADY_SYNCED", _agent_response("agent_ALREADY_SYNCED", version=6))

    result = sync_candidate(single_agent_dir, agents_client=agents_client, skills_client=FakeHttpxClient())

    assert result.updated == ["agent example-single-agent-EXAMPLE"]
    # GET, POST (409), GET again, POST again (succeeds) -- confirms the retry
    # re-reads the version rather than blindly resubmitting the stale one.
    assert agents_client.call_signature() == [
        ("GET", "/v1/agents/agent_ALREADY_SYNCED"),
        ("POST", "/v1/agents/agent_ALREADY_SYNCED"),
        ("GET", "/v1/agents/agent_ALREADY_SYNCED"),
        ("POST", "/v1/agents/agent_ALREADY_SYNCED"),
    ]
    first_post_body = agents_client.calls[1].kwargs["json"]
    retry_post_body = agents_client.calls[3].kwargs["json"]
    assert first_post_body["version"] == 3
    assert retry_post_body["version"] == 5  # the FRESHLY re-read version, never the stale 3


def test_update_propagates_non_409_http_errors(single_agent_dir):
    _mark_synced(single_agent_dir, agent_id="agent_ALREADY_SYNCED")
    (single_agent_dir / "model.txt").write_text("claude-example-model-v2\n")

    agents_client = FakeHttpxClient()
    agents_client.when("GET", "/v1/agents/agent_ALREADY_SYNCED", _agent_response("agent_ALREADY_SYNCED", version=3, model="claude-example-model"))
    agents_client.when("POST", "/v1/agents/agent_ALREADY_SYNCED", FakeResponse(500, {"error": "server error"}))

    with pytest.raises(httpx.HTTPStatusError):
        sync_candidate(single_agent_dir, agents_client=agents_client, skills_client=FakeHttpxClient())


# --- Multi-agent update ordering: THE critical ordering test -----------------------


def test_multi_agent_update_orders_sub_agent_before_coordinator(multi_agent_dir):
    """Decision 2c's critical nuance: a coordinator does NOT automatically pick up a
    new sub-agent version, so when a sub-agent changes, the correct order is
    (i) update the sub-agent(s) first, (ii) THEN update the coordinator so its
    roster re-pins. This test asserts the actual recorded call ORDER, not merely that
    both calls eventually happened."""
    _mark_synced(multi_agent_dir, agent_id="agent_COORD_SYNCED", sub_agent_ids=["agent_SUB_SYNCED"])
    # Change ONLY the sub-agent's declaration (its system prompt).
    multiagent_json_path = multi_agent_dir / "multiagent.json"
    multiagent_data = json.loads(multiagent_json_path.read_text())
    multiagent_data["agents"][0]["system_prompt"] = "EXAMPLE SUB-AGENT SYSTEM PROMPT V2 -- NOT A REAL CANDIDATE."
    multiagent_json_path.write_text(json.dumps(multiagent_data))

    agents_client = FakeHttpxClient()
    # Sub-agent: GET (returns the OLD prompt, i.e. differs from the new local file) then POST update.
    agents_client.when(
        "GET",
        "/v1/agents/agent_SUB_SYNCED",
        _agent_response(
            "agent_SUB_SYNCED",
            version=1,
            model="claude-example-sub-agent-model",
            system="EXAMPLE SUB-AGENT SYSTEM PROMPT -- NOT A REAL CANDIDATE. You are a fake example researcher sub-agent used only to exercise deploy/candidates/sync.py's multi-agent logic in tests.",
            name="example-sub-agent-researcher-EXAMPLE",
            description="EXAMPLE -- NOT A REAL CANDIDATE. A synthetic sub-agent test fixture.",
            tools=[{"type": "agent_toolset_20260401"}],
            mcp_servers=[],
            skills=[],
            parameters={"effort": "example-medium"},
        ),
    )
    agents_client.when("POST", "/v1/agents/agent_SUB_SYNCED", _agent_response("agent_SUB_SYNCED", version=2))
    # Coordinator: unchanged declaration, but STILL gets a follow-up update because
    # the sub-agent changed (Decision 2c's re-pinning requirement).
    coordinator_json = json.loads((multi_agent_dir / "agent.json").read_text())
    coordinator_model = (multi_agent_dir / "model.txt").read_text().strip()
    coordinator_system = (multi_agent_dir / "system-prompt.md").read_text()
    coordinator_parameters = json.loads((multi_agent_dir / "parameters.json").read_text())
    agents_client.when(
        "GET",
        "/v1/agents/agent_COORD_SYNCED",
        _agent_response(
            "agent_COORD_SYNCED",
            version=1,
            name=coordinator_json["name"],
            description=coordinator_json["description"],
            model=coordinator_model,
            system=coordinator_system,
            tools=coordinator_json["tools"],
            mcp_servers=coordinator_json["mcp_servers"],
            skills=[{"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1}],
            parameters=coordinator_parameters,
        ),
    )
    agents_client.when("POST", "/v1/agents/agent_COORD_SYNCED", _agent_response("agent_COORD_SYNCED", version=2))
    # skills.json in this fixture has no version pinned yet at copy time -- pin one,
    # WITH the content_hash matching the fixture's actual (unchanged) skill/
    # directory, so this test isn't ALSO exercising the skill-push path (that's
    # covered elsewhere, in the dedicated skill-content-change tests below).
    skills_json_path = multi_agent_dir / "skills.json"
    skills_json_path.write_text(
        json.dumps([{"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1, "content_hash": _FIXTURE_SKILL_CONTENT_HASH}])
    )

    result = sync_candidate(multi_agent_dir, agents_client=agents_client, skills_client=FakeHttpxClient())

    assert result.updated == [
        "sub-agent[0] example-sub-agent-researcher-EXAMPLE",
        "agent (coordinator) example-multi-agent-coordinator-EXAMPLE",
    ]

    # THE assertion that matters: the actual recorded HTTP call order has the
    # sub-agent's GET+POST BEFORE the coordinator's GET+POST -- not just that all
    # four calls eventually happened in some order.
    assert agents_client.call_signature() == [
        ("GET", "/v1/agents/agent_SUB_SYNCED"),
        ("POST", "/v1/agents/agent_SUB_SYNCED"),
        ("GET", "/v1/agents/agent_COORD_SYNCED"),
        ("POST", "/v1/agents/agent_COORD_SYNCED"),
    ]

    # And the coordinator's update body re-pins its roster to reference the
    # (unchanged, in this test) sub-agent id -- the actual re-pinning mechanism.
    coordinator_update_body = agents_client.calls[3].kwargs["json"]
    assert coordinator_update_body["multiagent"]["agents"][0] == {"type": "agent", "id": "agent_SUB_SYNCED"}


def test_multi_agent_update_is_a_full_no_op_when_nothing_changed(multi_agent_dir):
    _mark_synced(multi_agent_dir, agent_id="agent_COORD_SYNCED", sub_agent_ids=["agent_SUB_SYNCED"])
    skills_json_path = multi_agent_dir / "skills.json"
    # content_hash matches the fixture's ACTUAL (unchanged) skill/ directory -- see
    # _FIXTURE_SKILL_CONTENT_HASH -- so this genuinely exercises the "nothing
    # changed, including the skill" no-op path, not an accidental skill-push.
    skills_json_path.write_text(
        json.dumps([{"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1, "content_hash": _FIXTURE_SKILL_CONTENT_HASH}])
    )

    sub_agent_json = json.loads((multi_agent_dir / "multiagent.json").read_text())["agents"][0]
    coordinator_json = json.loads((multi_agent_dir / "agent.json").read_text())
    coordinator_model = (multi_agent_dir / "model.txt").read_text().strip()
    coordinator_system = (multi_agent_dir / "system-prompt.md").read_text()
    coordinator_parameters = json.loads((multi_agent_dir / "parameters.json").read_text())

    agents_client = FakeHttpxClient()
    agents_client.when(
        "GET",
        "/v1/agents/agent_SUB_SYNCED",
        _agent_response(
            "agent_SUB_SYNCED",
            version=1,
            name=sub_agent_json["name"],
            description=sub_agent_json["description"],
            model=sub_agent_json["model"],
            system=sub_agent_json["system_prompt"],
            tools=sub_agent_json["tools"],
            mcp_servers=sub_agent_json["mcp_servers"],
            skills=sub_agent_json["skills"],
            parameters=sub_agent_json["parameters"],
        ),
    )
    agents_client.when(
        "GET",
        "/v1/agents/agent_COORD_SYNCED",
        _agent_response(
            "agent_COORD_SYNCED",
            version=1,
            name=coordinator_json["name"],
            description=coordinator_json["description"],
            model=coordinator_model,
            system=coordinator_system,
            tools=coordinator_json["tools"],
            mcp_servers=coordinator_json["mcp_servers"],
            skills=[{"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1}],
            parameters=coordinator_parameters,
        ),
    )

    result = sync_candidate(multi_agent_dir, agents_client=agents_client, skills_client=FakeHttpxClient())

    assert result.no_op is True
    assert not result.updated
    # Only the two read-only GETs (one per agent) were made -- no POST at all.
    assert agents_client.call_signature() == [
        ("GET", "/v1/agents/agent_SUB_SYNCED"),
        ("GET", "/v1/agents/agent_COORD_SYNCED"),
    ]


# --- Partial-failure resumption ------------------------------------------------------


def test_partial_failure_resumption_does_not_repush_skill(multi_agent_dir):
    """Simulates: skill-push succeeds and skills.json is updated, but agent creation
    then fails outright. Re-running must pick up correctly WITHOUT re-pushing the
    skill (skills.json already carries the pinned version by then)."""
    agents_client_first_attempt = FakeHttpxClient()
    agents_client_first_attempt.when("POST", "/v1/agents", FakeResponse(500, {"error": "simulated failure"}))
    skills_client = FakeHttpxClient()
    skills_client.when(
        "POST",
        "/v1/skills/skill_EXAMPLE_NOT_REAL/versions",
        FakeResponse(200, {"id": "skill_EXAMPLE_NOT_REAL", "version": 1783096569199829}),
    )

    with pytest.raises(httpx.HTTPStatusError):
        sync_candidate(multi_agent_dir, agents_client=agents_client_first_attempt, skills_client=skills_client)

    # The skill push DID happen and DID get recorded, even though the overall sync failed.
    assert len(skills_client.calls) == 1
    updated_skills_json = json.loads((multi_agent_dir / "skills.json").read_text())
    assert updated_skills_json == [
        {"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1783096569199829, "content_hash": _FIXTURE_SKILL_CONTENT_HASH}
    ]
    # Neither agent_id was written (creation never succeeded).
    assert "agent_id" not in json.loads((multi_agent_dir / "candidate.json").read_text())

    # --- Re-run, simulating a fixed/retried invocation ---
    agents_client_retry = FakeHttpxClient()
    agents_client_retry.when("POST", "/v1/agents", _agent_response("agent_SUB_NEW"), _agent_response("agent_COORD_NEW"))
    skills_client_retry = FakeHttpxClient()  # deliberately given NO scripted skill-push response

    result = sync_candidate(multi_agent_dir, agents_client=agents_client_retry, skills_client=skills_client_retry)

    assert result.created == [
        "sub-agent[0] example-sub-agent-researcher-EXAMPLE",
        "agent (coordinator) example-multi-agent-coordinator-EXAMPLE",
    ]
    # The retry made ZERO calls against the skills client -- confirming the skill was
    # NOT re-pushed (skills_client_retry has no registered handler at all; if the
    # sync script had tried to push again, FakeHttpxClient.post() would have raised
    # "no scripted response registered", which it did not).
    assert skills_client_retry.calls == []


def test_partial_failure_resumption_does_not_recreate_sub_agent(multi_agent_dir):
    """Regression test for the bug the reviewer found: sub-agent creation succeeds
    (and its id is written into multiagent.json's roster), but the SUBSEQUENT
    coordinator-create call then fails. Re-running must NOT recreate a second,
    duplicate sub-agent -- it must reuse the already-created one and issue exactly
    ONE more `POST /v1/agents` call (the coordinator only).

    Both the sub-agent's create and the coordinator's create hit the identical
    (method, path) key `("POST", "/v1/agents")` -- they're distinguished only by
    request body, not URL -- so this test relies on FakeHttpxClient.when()'s
    documented "multiple registered responses are returned in order across repeated
    calls" behavior: [success, failure] queues the sub-agent's create to succeed and
    the very next call to that same key (the coordinator's create) to fail.
    """
    agents_client_first_attempt = FakeHttpxClient()
    agents_client_first_attempt.when(
        "POST",
        "/v1/agents",
        _agent_response("agent_SUB_NEW"),  # 1st call: the sub-agent's create -- succeeds
        FakeResponse(500, {"error": "simulated coordinator create failure"}),  # 2nd call: the coordinator's create -- fails
    )
    skills_client = FakeHttpxClient()
    skills_client.when(
        "POST",
        "/v1/skills/skill_EXAMPLE_NOT_REAL/versions",
        FakeResponse(200, {"id": "skill_EXAMPLE_NOT_REAL", "version": 1783096569199829}),
    )

    with pytest.raises(httpx.HTTPStatusError):
        sync_candidate(multi_agent_dir, agents_client=agents_client_first_attempt, skills_client=skills_client)

    # THE assertion that proves this test genuinely reaches the "sub-agent succeeded,
    # coordinator failed" branch (not just "some failure happened somewhere"): exactly
    # two POST /v1/agents calls were made in the first attempt (sub-agent, then
    # coordinator) -- not one (which would mean the sub-agent's own create failed
    # instead, the bug the reviewer found in the OLD test).
    assert agents_client_first_attempt.call_signature() == [
        ("POST", "/v1/agents"),
        ("POST", "/v1/agents"),
    ]
    # And the sub-agent's id WAS persisted before the coordinator's create failed --
    # this is the exact state a real partial failure leaves behind.
    persisted_multiagent = json.loads((multi_agent_dir / "multiagent.json").read_text())
    assert persisted_multiagent["agents"][0]["agent_id"] == "agent_SUB_NEW"
    assert "agent_id" not in json.loads((multi_agent_dir / "candidate.json").read_text())

    # --- Re-run, simulating a fixed/retried invocation ---
    agents_client_retry = FakeHttpxClient()
    agents_client_retry.when("POST", "/v1/agents", _agent_response("agent_COORD_NEW"))
    skills_client_retry = FakeHttpxClient()  # deliberately given NO scripted skill-push response

    result = sync_candidate(multi_agent_dir, agents_client=agents_client_retry, skills_client=skills_client_retry)

    # Only the COORDINATOR was created this time -- the sub-agent was reused, not recreated.
    assert result.created == ["agent (coordinator) example-multi-agent-coordinator-EXAMPLE"]
    assert agents_client_retry.call_signature() == [("POST", "/v1/agents")]

    coordinator_create_body = agents_client_retry.calls[0].kwargs["json"]
    # The coordinator's create body references the ORIGINAL sub-agent id, never a
    # second, freshly-minted one -- confirming no duplicate sub-agent was created.
    # Platform-confirmed roster shape: each agents[] item is DIRECTLY {"type":"agent","id":<id>}
    # (no `entry` wrapper) -- platform.claude.com/docs/en/managed-agents/multi-agent.
    assert coordinator_create_body["multiagent"]["agents"][0] == {"type": "agent", "id": "agent_SUB_NEW"}

    updated_multiagent = json.loads((multi_agent_dir / "multiagent.json").read_text())
    assert updated_multiagent["agents"][0]["agent_id"] == "agent_SUB_NEW"  # unchanged, no rewrite needed
    updated_candidate = json.loads((multi_agent_dir / "candidate.json").read_text())
    assert updated_candidate["agent_id"] == "agent_COORD_NEW"

    # The skill was also NOT re-pushed on retry (same resumability guarantee as the
    # sibling test above, exercised together here since both partial-failure paths
    # apply to the same multi-agent first-sync).
    assert skills_client_retry.calls == []


def test_first_sync_single_agent_failure_leaves_no_agent_id_written(single_agent_dir):
    """A single-agent candidate has no skill to push, so a create failure should
    simply leave candidate.json untouched (nothing partial to clean up)."""
    agents_client = FakeHttpxClient()
    agents_client.when("POST", "/v1/agents", FakeResponse(500, {"error": "simulated failure"}))

    with pytest.raises(httpx.HTTPStatusError):
        sync_candidate(single_agent_dir, agents_client=agents_client, skills_client=FakeHttpxClient())

    assert "agent_id" not in json.loads((single_agent_dir / "candidate.json").read_text())


# --- Symlink hardening (security-engineer, Low severity) --------------------------
# _iter_skill_source_files() (shared by _compute_skill_content_hash() and
# _zip_skill_source()) must skip symlinks entirely, so a symlink inside skill/
# cannot smuggle in content from OUTSIDE skill_source_dir into either the hash or
# the pushed zip.


def test_iter_skill_source_files_skips_a_symlink_pointing_outside_the_skill_dir(tmp_path):
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("this must never be hashed or zipped")

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: symlink-test\n---\nreal content\n")
    (skill_dir / "evil-link.txt").symlink_to(outside_secret)

    files = list(_iter_skill_source_files(skill_dir))

    assert [f.name for f in files] == ["SKILL.md"]
    assert not any(f.is_symlink() for f in files)


def test_compute_skill_content_hash_ignores_a_symlinked_file(tmp_path):
    """The hash of a skill/ directory with a symlink pointing at attacker-
    controlled content must be IDENTICAL to the hash of the same directory with
    the symlink simply absent -- proving the symlink's target content never
    entered the hash at all (not merely that the hash "changed," which alone
    wouldn't prove exclusion)."""
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("attacker-controlled content")

    skill_dir_with_symlink = tmp_path / "skill-with-symlink"
    skill_dir_with_symlink.mkdir()
    (skill_dir_with_symlink / "SKILL.md").write_text("---\nname: symlink-test\n---\nreal content\n")
    (skill_dir_with_symlink / "evil-link.txt").symlink_to(outside_secret)

    skill_dir_without_symlink = tmp_path / "skill-without-symlink"
    skill_dir_without_symlink.mkdir()
    (skill_dir_without_symlink / "SKILL.md").write_text("---\nname: symlink-test\n---\nreal content\n")

    assert _compute_skill_content_hash(skill_dir_with_symlink) == _compute_skill_content_hash(skill_dir_without_symlink)


def test_zip_skill_source_excludes_a_symlinked_file(tmp_path):
    """The zip _zip_skill_source() builds must NOT contain the symlink's target
    content -- confirms the exclusion holds for the actual zip that gets pushed to
    the Skills API, not just the hash."""
    import zipfile

    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("this must never be zipped")

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: symlink-test\n---\nreal content\n")
    (skill_dir / "evil-link.txt").symlink_to(outside_secret)

    zip_path = _zip_skill_source(skill_dir, tmp_path / "skill.zip")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    assert names == ["symlink-test/SKILL.md"]
    assert not any("evil-link" in name for name in names)


# --- Helpers -------------------------------------------------------------------------


def _mark_synced(candidate_dir, *, agent_id: str, sub_agent_ids: list[str] | None = None) -> None:
    """Test helper: simulate "this candidate has already been synced once" by writing
    the given agent_id(s) into candidate.json / multiagent.json, exactly as
    candidate_sync.writer would after a real first sync."""
    candidate_json_path = candidate_dir / "candidate.json"
    data = json.loads(candidate_json_path.read_text())
    data["agent_id"] = agent_id
    candidate_json_path.write_text(json.dumps(data))

    if sub_agent_ids:
        multiagent_json_path = candidate_dir / "multiagent.json"
        multiagent_data = json.loads(multiagent_json_path.read_text())
        for index, sub_agent_id in enumerate(sub_agent_ids):
            multiagent_data["agents"][index]["agent_id"] = sub_agent_id
        multiagent_json_path.write_text(json.dumps(multiagent_data))
