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
