"""How `sync.py` compares a DECLARED per-tool toolset setting against the live echo
(ADR-0018 D8's `max_content_tokens` mitigation, 2026-10-17).

Until `haiku-5-5-source-urls-capped` every candidate in this repo declared
`"configs": []`, so `tools` was compared raw and that was correct. A declaration that
sets a config entry (`{"name": "web_fetch", "max_content_tokens": 8000}`) is different in
kind: the API infers the entry's `type` from its `name` and documents that it fills in
omitted fields with defaults, so the live echo is expected to carry MORE keys than the
declaration sent. Compared raw, that would make every re-sync of that candidate look
changed -- a pointless `POST /v1/agents/{id}` and a live version bump on every run, which
is exactly the `model`-shape bug Phase 5 had to fix once already (FR-12/AC-12: an
unchanged declaration is a no-op at the MUTATION level, not merely at the platform's).

So the comparison projects each live config entry onto exactly the keys the declaration
sets for it. What must NOT become tolerant is asserted here just as hard as what must:

  - a declared setting the echo CHANGED, or DROPPED entirely (the silent-discard hazard
    this repo has been bitten by on `model.effort`), is a real difference;
  - an extra/missing config ENTRY is a real difference;
  - a declaration with `configs: []` keeps comparing `tools` verbatim, byte for byte as
    before -- the guarantee the other eleven candidates rely on.

Every HTTP call is mocked (`FakeHttpxClient`); no real agent resource is touched.
"""

from __future__ import annotations

import copy
import json
import shutil

import pytest

from candidate_sync.loader import load_candidate
from candidate_sync.sync import sync_candidate

from conftest import CANDIDATES_DIR, FIXTURES_DIR
from fake_httpx_client import FakeHttpxClient, FakeResponse

CAPPED_SLUG = "haiku-5-5-source-urls-capped"

# The declaration's own config entry, and the extra fields a live echo is expected to
# fill in around it (`type` inferred from `name`, plus the toolset's default_config
# values applied per entry). Mirrors the documented settings shape at
# platform.claude.com/docs/en/managed-agents/tools-web-restrictions.
DECLARED_WEB_FETCH_CONFIG = {"name": "web_fetch", "max_content_tokens": 8000}
LIVE_ECHO_FILLED_IN_FIELDS = {
    "type": "web_fetch",
    "enabled": True,
    "permission_policy": {"type": "always_allow"},
}


def _agent_response(agent_id: str, *, version: int = 1, **overrides) -> FakeResponse:
    return FakeResponse(200, {"id": agent_id, "version": version, **overrides})


def _live_echo_of(declaration, *, live_tools) -> dict:
    """A live `GET /v1/agents/{id}` body that agrees with `declaration` on every field
    except `tools`, which the caller supplies. `model` is echoed in the real,
    live-confirmed object shape (`{"id", "effort", "speed"}`) so these tests exercise the
    same normalization path a real sync does."""
    body = declaration.to_agent_body()
    live = {
        "name": body["name"],
        "description": body["description"],
        "model": {"id": declaration.model, "speed": "standard"},
        "system": body["system"],
        "tools": live_tools,
        "mcp_servers": body["mcp_servers"],
        "skills": body.get("skills", []),
    }
    if declaration.effort is not None:
        live["model"]["effort"] = {"type": declaration.effort}
    return live


def _tools_with_live_configs(declaration, config_entries: list[dict]) -> list[dict]:
    """The declaration's own `tools` list with its single toolset entry's `configs`
    replaced by `config_entries` -- i.e. what the live agent is pretending to carry."""
    tools = copy.deepcopy(declaration.tools)
    tools[0]["configs"] = config_entries
    return tools


def _run_update(candidate_dir, live_tools):
    """Mark the candidate synced, answer one GET with a live echo carrying `live_tools`,
    and run a full `sync_candidate()` update. Returns `(result, agents_client)`."""
    candidate_json = json.loads((candidate_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate_json["agent_id"] = "agent_ALREADY_SYNCED"
    (candidate_dir / "candidate.json").write_text(json.dumps(candidate_json, indent=2) + "\n", encoding="utf-8")

    declaration = load_candidate(candidate_dir).agent
    agents_client = FakeHttpxClient()
    agents_client.when(
        "GET",
        "/v1/agents/agent_ALREADY_SYNCED",
        _agent_response("agent_ALREADY_SYNCED", version=4, **_live_echo_of(declaration, live_tools=live_tools)),
    )
    agents_client.when("POST", "/v1/agents/agent_ALREADY_SYNCED", _agent_response("agent_ALREADY_SYNCED", version=5))

    result = sync_candidate(candidate_dir, agents_client=agents_client, skills_client=FakeHttpxClient())
    return result, agents_client


@pytest.fixture
def capped_candidate_dir(tmp_path):
    """A writable copy of the REAL capped candidate -- so these tests exercise the
    declaration that will actually be synced, not a synthetic stand-in. Copied because
    the sync script rewrites `candidate.json`, and the tracked candidate must stay
    untouched (its `agent_id` is minted by the orchestrator's own sync run)."""
    candidate_dir = tmp_path / CAPPED_SLUG
    shutil.copytree(CANDIDATES_DIR / CAPPED_SLUG, candidate_dir)
    return candidate_dir


@pytest.fixture
def no_configs_candidate_dir(tmp_path):
    """The synthetic single-agent fixture, whose `agent.json` declares a toolset with no
    `configs` key at all -- the shape every pre-2026-10-17 candidate has."""
    candidate_dir = tmp_path / "example-single-agent"
    shutil.copytree(FIXTURES_DIR / "example-single-agent", candidate_dir)
    return candidate_dir


# --- the tolerant case: filled-in fields on a declared config entry -------------------


def test_a_live_echo_that_fills_in_the_declared_config_entry_is_a_full_no_op(capped_candidate_dir):
    """The reason this normalization exists: `type`/`enabled`/`permission_policy` arriving
    on the echoed `web_fetch` entry must not read as a change."""
    declaration = load_candidate(capped_candidate_dir).agent
    live_tools = _tools_with_live_configs(
        declaration, [{**LIVE_ECHO_FILLED_IN_FIELDS, **DECLARED_WEB_FETCH_CONFIG}]
    )

    result, agents_client = _run_update(capped_candidate_dir, live_tools)

    assert result.no_op is True
    assert not result.updated
    assert agents_client.call_signature() == [("GET", "/v1/agents/agent_ALREADY_SYNCED")]


def test_a_byte_identical_live_echo_of_the_declared_configs_is_also_a_no_op(capped_candidate_dir):
    """If the API echoes the entry back exactly as sent, nothing about the projection may
    turn that into a difference either."""
    declaration = load_candidate(capped_candidate_dir).agent

    result, _ = _run_update(capped_candidate_dir, copy.deepcopy(declaration.tools))

    assert result.no_op is True


def test_a_reordered_live_configs_echo_is_still_a_no_op(capped_candidate_dir):
    """Entries are paired by `name`, not by position, so an echo that lists them in
    another order is not a difference. (Two declared entries, since one cannot be
    reordered.)"""
    agent_json = json.loads((capped_candidate_dir / "agent.json").read_text(encoding="utf-8"))
    agent_json["tools"][0]["configs"] = [
        DECLARED_WEB_FETCH_CONFIG,
        {"name": "web_search", "enabled": False},
    ]
    (capped_candidate_dir / "agent.json").write_text(json.dumps(agent_json, indent=2) + "\n", encoding="utf-8")

    declaration = load_candidate(capped_candidate_dir).agent
    live_tools = _tools_with_live_configs(
        declaration,
        [
            {"type": "web_search", "name": "web_search", "enabled": False, "permission_policy": {"type": "always_allow"}},
            {**LIVE_ECHO_FILLED_IN_FIELDS, **DECLARED_WEB_FETCH_CONFIG},
        ],
    )

    result, _ = _run_update(capped_candidate_dir, live_tools)

    assert result.no_op is True


def test_an_echo_that_carries_no_name_is_not_aligned_and_so_still_differs(capped_candidate_dir):
    """Entries are paired by `name` and there is no positional fallback: an echo that
    identifies the entry by `type` alone cannot be aligned, so it is compared verbatim
    and reads as a difference (never a crash, and never a guessed pairing). Pairing it
    positionally would reach the same answer anyway, since the declared `name` would be
    missing from the projection either way."""
    declaration = load_candidate(capped_candidate_dir).agent
    live_tools = _tools_with_live_configs(
        declaration, [{"type": "web_fetch", "enabled": True, "max_content_tokens": 8000}]
    )

    result, _ = _run_update(capped_candidate_dir, live_tools)

    assert result.updated == [f"agent {declaration.name}"]


# --- what must stay intolerant -------------------------------------------------------


def test_a_changed_declared_setting_is_a_real_difference(capped_candidate_dir):
    """The cap is the whole point of this candidate: a live value other than the declared
    one must drive a real update, carrying the declared value."""
    declaration = load_candidate(capped_candidate_dir).agent
    live_tools = _tools_with_live_configs(
        declaration,
        [{**LIVE_ECHO_FILLED_IN_FIELDS, "name": "web_fetch", "max_content_tokens": 16000}],
    )

    result, agents_client = _run_update(capped_candidate_dir, live_tools)

    assert result.updated == [f"agent {declaration.name}"]
    sent_body = agents_client.calls[-1].kwargs["json"]
    assert sent_body["tools"][0]["configs"] == [DECLARED_WEB_FETCH_CONFIG]


def test_a_live_echo_that_dropped_the_declared_setting_is_a_real_difference(capped_candidate_dir):
    """The silent-discard hazard, by name: an accepted-then-dropped
    `max_content_tokens` means the cost cap is NOT in force. The sync must say `updated`,
    not `no-op` -- the operator's only signal that the knob did not take."""
    declaration = load_candidate(capped_candidate_dir).agent
    live_tools = _tools_with_live_configs(declaration, [{**LIVE_ECHO_FILLED_IN_FIELDS, "name": "web_fetch"}])

    result, _ = _run_update(capped_candidate_dir, live_tools)

    assert result.updated == [f"agent {declaration.name}"]


def test_an_extra_live_config_entry_is_a_real_difference(capped_candidate_dir):
    """Per-tool configuration the declaration does not describe is a genuine divergence,
    not noise to be projected away."""
    declaration = load_candidate(capped_candidate_dir).agent
    live_tools = _tools_with_live_configs(
        declaration,
        [
            {**LIVE_ECHO_FILLED_IN_FIELDS, **DECLARED_WEB_FETCH_CONFIG},
            {"type": "web_search", "name": "web_search", "enabled": False},
        ],
    )

    result, _ = _run_update(capped_candidate_dir, live_tools)

    assert result.updated == [f"agent {declaration.name}"]


def test_a_live_echo_missing_the_declared_entry_entirely_is_a_real_difference(capped_candidate_dir):
    declaration = load_candidate(capped_candidate_dir).agent

    result, _ = _run_update(capped_candidate_dir, _tools_with_live_configs(declaration, []))

    assert result.updated == [f"agent {declaration.name}"]


def test_a_changed_toolset_field_outside_configs_is_still_a_real_difference(capped_candidate_dir):
    """Only the `configs` entries are projected; the rest of the toolset entry
    (`type`, `default_config`) is compared exactly as before."""
    declaration = load_candidate(capped_candidate_dir).agent
    live_tools = _tools_with_live_configs(declaration, [{**LIVE_ECHO_FILLED_IN_FIELDS, **DECLARED_WEB_FETCH_CONFIG}])
    live_tools[0]["default_config"] = {"enabled": False, "permission_policy": {"type": "always_ask"}}

    result, _ = _run_update(capped_candidate_dir, live_tools)

    assert result.updated == [f"agent {declaration.name}"]


# --- the eleven other candidates: unchanged behaviour --------------------------------


def test_a_declaration_with_no_configs_compares_tools_verbatim(no_configs_candidate_dir):
    """A live `configs` the declaration never asked for is a difference, exactly as it
    was before this normalization existed -- the tolerance must not leak into the
    `configs: []` candidates."""
    declaration = load_candidate(no_configs_candidate_dir).agent
    live_tools = copy.deepcopy(declaration.tools)
    live_tools[0]["configs"] = [{**LIVE_ECHO_FILLED_IN_FIELDS, **DECLARED_WEB_FETCH_CONFIG}]

    result, _ = _run_update(no_configs_candidate_dir, live_tools)

    assert result.updated == [f"agent {declaration.name}"]


def test_an_unchanged_declaration_with_no_configs_is_still_a_no_op(no_configs_candidate_dir):
    declaration = load_candidate(no_configs_candidate_dir).agent

    result, agents_client = _run_update(no_configs_candidate_dir, copy.deepcopy(declaration.tools))

    assert result.no_op is True
    assert agents_client.call_signature() == [("GET", "/v1/agents/agent_ALREADY_SYNCED")]


def test_no_tracked_candidate_but_the_capped_one_declares_a_toolset_config():
    """The scope claim this whole module rests on: exactly one declaration in the repo
    sets a per-tool config today. If a second one appears, its own comparison behaviour
    needs the same thought -- and this test is where that gets noticed."""
    declaring = []
    for candidate_dir in sorted(p for p in CANDIDATES_DIR.iterdir() if (p / "candidate.json").is_file()):
        candidate = load_candidate(candidate_dir)
        for agent in [candidate.agent, *candidate.sub_agents]:
            if any(entry.get("configs") for entry in agent.tools):
                declaring.append(candidate.slug)

    assert declaring == [CAPPED_SLUG], declaring
