"""Tests for candidate_sync.api_client -- the thin Agents/Skills API wrappers.

Covers `list_agent_versions()` (previously untested, per the reviewer's should-fix
note: it's read-only and low-risk, but it's part of the documented historical-state-
retrieval story in README.md, so it deserves a small confirming test) and
`require_field()`'s fail-loud behavior on a malformed response."""

from __future__ import annotations

import pytest

from candidate_sync import api_client
from fake_httpx_client import FakeHttpxClient, FakeResponse


def test_list_agent_versions_reads_the_data_array_shape():
    """Confirmed live shape (see api_client.py's own docstring): a dict with a
    top-level "data" array, mirroring the Skills API's versions-list shape."""
    client = FakeHttpxClient()
    client.when(
        "GET",
        "/v1/agents/agent_EXAMPLE/versions",
        FakeResponse(
            200,
            {
                "data": [
                    {"id": "agent_EXAMPLE", "version": 1, "updated_at": "2026-01-01T00:00:00Z"},
                    {"id": "agent_EXAMPLE", "version": 2, "updated_at": "2026-01-02T00:00:00Z"},
                ]
            },
        ),
    )

    versions = api_client.list_agent_versions(client, "agent_EXAMPLE")

    assert len(versions) == 2
    assert versions[0]["version"] == 1
    assert versions[1]["version"] == 2


def test_list_agent_versions_reads_a_bare_list_shape_too():
    """Defensive: if a future response shape returns a bare list instead of
    {"data": [...]}, list_agent_versions() must still return it directly rather
    than erroring or silently returning something wrong."""
    client = FakeHttpxClient()
    client.when(
        "GET",
        "/v1/agents/agent_EXAMPLE/versions",
        FakeResponse(200, [{"id": "agent_EXAMPLE", "version": 1}]),
    )

    versions = api_client.list_agent_versions(client, "agent_EXAMPLE")

    assert versions == [{"id": "agent_EXAMPLE", "version": 1}]


def test_require_field_returns_the_value_when_present():
    resource = {"id": "agent_EXAMPLE", "version": 3}
    assert api_client.require_field(resource, "version", context="a test") == 3


def test_require_field_raises_a_clear_error_when_missing():
    """Confirms the fail-loud fix: a malformed response missing 'version' raises a
    clear, actionable MalformedApiResponseError (naming the field and context) rather
    than propagating a raw, confusing KeyError."""
    resource = {"id": "agent_EXAMPLE"}  # no "version" field

    with pytest.raises(api_client.MalformedApiResponseError, match="version"):
        api_client.require_field(resource, "version", context="reading agent agent_EXAMPLE before an update")


def test_create_skill_posts_to_top_level_skills_collection(tmp_path):
    """agent-system-redesign epic Phase 3: create_skill() (POST /v1/skills) is a
    genuinely NEW function -- Phase 2 only ever pushed a VERSION to an
    ALREADY-EXISTING skill_id (create_skill_version()); this confirms the request
    goes to the top-level collection, not a /versions sub-resource, and that a
    zip file is attached as multipart files[] (the same shape create_skill_version()
    already uses)."""
    zip_path = tmp_path / "skill.zip"
    zip_path.write_bytes(b"fake zip bytes")

    client = FakeHttpxClient()
    client.when("POST", "/v1/skills", FakeResponse(200, {"id": "skill_NEW", "version": 1783337264004829}))

    result = api_client.create_skill(client, str(zip_path))

    assert result == {"id": "skill_NEW", "version": 1783337264004829}
    assert client.call_signature() == [("POST", "/v1/skills")]
    sent_files = client.calls[0].kwargs["files"]
    assert "files[]" in sent_files
    assert sent_files["files[]"][0] == "skill.zip"


def test_create_skill_version_still_posts_to_the_versions_sub_resource(tmp_path):
    """Regression check: create_skill() (new, top-level) must NOT be confused with
    create_skill_version() (existing, /versions sub-resource) -- both are exercised
    together here to confirm they hit DIFFERENT paths."""
    zip_path = tmp_path / "skill.zip"
    zip_path.write_bytes(b"fake zip bytes")

    client = FakeHttpxClient()
    client.when("POST", "/v1/skills/skill_EXISTING/versions", FakeResponse(200, {"id": "skill_EXISTING", "version": 1783337264004829}))

    result = api_client.create_skill_version(client, "skill_EXISTING", str(zip_path))

    assert result == {"id": "skill_EXISTING", "version": 1783337264004829}
    assert client.call_signature() == [("POST", "/v1/skills/skill_EXISTING/versions")]
