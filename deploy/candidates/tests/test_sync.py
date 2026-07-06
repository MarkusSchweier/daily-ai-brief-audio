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

from candidate_sync.sync import sync_candidate

from conftest import FIXTURES_DIR
from fake_httpx_client import FakeHttpxClient, FakeResponse


def _agent_response(agent_id: str, *, version: int = 1, **overrides) -> FakeResponse:
    body = {"id": agent_id, "version": version, **overrides}
    return FakeResponse(200, body)


@pytest.fixture
def single_agent_dir(tmp_path):
    candidate_dir = tmp_path / "example-single-agent"
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)
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
    assert coordinator_create_body["multiagent"]["agents"][0]["entry"]["agent"] == "agent_SUB_NEW"

    # The skill was pushed on the skills client, not the agents client.
    skill_push_calls = [c for c in skills_client.calls if c.path.startswith("/v1/skills/")]
    assert len(skill_push_calls) == 1

    # skills.json now carries the concrete pinned version, not a bare skill_id.
    updated_skills_json = json.loads((multi_agent_dir / "skills.json").read_text())
    assert updated_skills_json == [{"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1783096569199829}]


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
    # skills.json in this fixture has no version pinned yet at copy time -- pin one so
    # this test isn't ALSO exercising the skill-push path (that's covered elsewhere).
    skills_json_path = multi_agent_dir / "skills.json"
    skills_json_path.write_text(json.dumps([{"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1}]))

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
    assert coordinator_update_body["multiagent"]["agents"][0]["entry"]["agent"] == "agent_SUB_SYNCED"


def test_multi_agent_update_is_a_full_no_op_when_nothing_changed(multi_agent_dir):
    _mark_synced(multi_agent_dir, agent_id="agent_COORD_SYNCED", sub_agent_ids=["agent_SUB_SYNCED"])
    skills_json_path = multi_agent_dir / "skills.json"
    skills_json_path.write_text(json.dumps([{"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1}]))

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
    assert updated_skills_json == [{"skill_id": "skill_EXAMPLE_NOT_REAL", "version": 1783096569199829}]
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
    assert coordinator_create_body["multiagent"]["agents"][0]["entry"]["agent"] == "agent_SUB_NEW"

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
