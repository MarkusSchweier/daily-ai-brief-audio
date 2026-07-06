"""Thin wrapper over the Claude Platform Agents API + Skills API calls the sync
script needs.

Mirrors the exact working pattern already proven live in this repo at
`deploy/eval/functions/trigger/handler.py` (`handler()`'s `httpx.Client` construction,
and the beta-header choices documented in `deploy/managed-agent/README.md` around its
Skills-API version-push section) -- same base URL, same `x-api-key`/`anthropic-version`
headers, and the SAME two distinct `anthropic-beta` values for the two APIs:

  * Agents API (`POST /v1/agents`, `POST /v1/agents/{id}`, `GET /v1/agents/{id}`,
    `GET /v1/agents/{id}/versions`): `anthropic-beta: managed-agents-2026-04-01`.
  * Skills API (`POST /v1/skills/{id}/versions`, `GET /v1/skills/{id}/versions`): the
    SAME `x-api-key`/`anthropic-version`, but `anthropic-beta: skills-2025-10-02`.

No Anthropic API key is ever hardcoded, logged, or committed -- it is read from
`$ANTHROPIC_API_KEY` at call time (this repo's established local-CLI convention; see
`deploy/managed-agent/README.md`'s Skills-API version-push section), and this module
never includes it in any log line or exception message.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
AGENTS_BETA_HEADER = "managed-agents-2026-04-01"
SKILLS_BETA_HEADER = "skills-2025-10-02"

_REQUEST_TIMEOUT_SECONDS = 60.0


class AnthropicApiKeyMissingError(RuntimeError):
    """Raised when $ANTHROPIC_API_KEY is unset. Deliberately a distinct exception
    type (not a bare RuntimeError) so callers/tests can assert on it precisely."""


class MalformedApiResponseError(RuntimeError):
    """Raised when a Claude Platform API response is missing a field this module
    requires (e.g. `version` on an agent resource) -- a clear, actionable error
    instead of a raw KeyError deep in sync logic. Low-likelihood (the field is
    confirmed live-always-present on every real agent resource), but this keeps the
    failure mode consistent with the rest of the codebase's fail-loud discipline
    (`CandidateLoadError`, `AnthropicApiKeyMissingError`)."""


def require_field(resource: dict[str, Any], field_name: str, *, context: str) -> Any:
    """Read `resource[field_name]`, raising `MalformedApiResponseError` with a clear
    message (naming the field and the calling context, never the resource's full
    content -- avoids echoing anything sensitive back into an error message) if it's
    missing, instead of letting a raw KeyError propagate."""
    if field_name not in resource:
        raise MalformedApiResponseError(
            f"malformed API response while {context}: missing required field {field_name!r}"
        )
    return resource[field_name]


def get_anthropic_api_key() -> str:
    """Read the Anthropic API key from the environment. Never hardcode, log, or
    commit this value -- see this repo's CLAUDE.md credential conventions and
    `deploy/managed-agent/README.md`'s "$ANTHROPIC_API_KEY is read from the
    environment" convention."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AnthropicApiKeyMissingError(
            "ANTHROPIC_API_KEY is not set in the environment. Set it before running "
            "the sync script, e.g.: export ANTHROPIC_API_KEY=$(cat "
            "~/.anthropic-managed-agents/ant-api-key.txt)"
        )
    return api_key


def build_agents_client(api_key: str | None = None) -> httpx.Client:
    """An httpx.Client configured for Agents API calls (create/update/read agents,
    list agent versions)."""
    return httpx.Client(
        base_url=ANTHROPIC_API_BASE_URL,
        headers={
            "x-api-key": api_key or get_anthropic_api_key(),
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": AGENTS_BETA_HEADER,
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )


def build_skills_client(api_key: str | None = None) -> httpx.Client:
    """An httpx.Client configured for Skills API calls (push a new skill version,
    list skill versions) -- a DIFFERENT anthropic-beta header than the Agents API."""
    return httpx.Client(
        base_url=ANTHROPIC_API_BASE_URL,
        headers={
            "x-api-key": api_key or get_anthropic_api_key(),
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": SKILLS_BETA_HEADER,
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )


def create_agent(client: httpx.Client, agent_definition: dict[str, Any]) -> dict[str, Any]:
    """POST /v1/agents -- create a new agent. `agent_definition` is the full agent
    body (name/description/model/system/tools/mcp_servers/skills, and, for a
    coordinator, `multiagent`). Returns the created agent resource (which carries the
    new `id` and `version`)."""
    response = client.post("/v1/agents", json=agent_definition)
    response.raise_for_status()
    return response.json()


def get_agent(client: httpx.Client, agent_id: str) -> dict[str, Any]:
    """GET /v1/agents/{id} -- fetch the agent's CURRENT state, including its current
    `version`. Callers must call this immediately before an update (never cache/assume
    a version) per Decision 2c's update-in-place discipline."""
    response = client.get(f"/v1/agents/{agent_id}")
    response.raise_for_status()
    return response.json()


def update_agent(client: httpx.Client, agent_id: str, *, version: int, agent_definition: dict[str, Any]) -> dict[str, Any]:
    """POST /v1/agents/{id} -- update the agent IN PLACE under the same agent_id,
    incrementing its version (confirmed live: this ADR's "What I verified live"
    section). The body must include the current `version` as a required optimistic-
    concurrency precondition; a stale version returns 409 (raised as
    `httpx.HTTPStatusError` by `raise_for_status()`, which callers detect via
    `response.status_code == 409` -- see `sync.py`'s retry-on-409 handling)."""
    body = dict(agent_definition)
    body["version"] = version
    response = client.post(f"/v1/agents/{agent_id}", json=body)
    response.raise_for_status()
    return response.json()


def list_agent_versions(client: httpx.Client, agent_id: str) -> list[dict[str, Any]]:
    """GET /v1/agents/{id}/versions -- the full version history Claude Platform
    tracks natively for one candidate's `agent_id` (Decision 2c's "historical *live*
    state" source, complementary to git's historical *declaration* state)."""
    response = client.get(f"/v1/agents/{agent_id}/versions")
    response.raise_for_status()
    payload = response.json()
    # Confirmed live shape mirrors the Skills API's own versions list: a `data` array.
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def create_skill(client: httpx.Client, zip_path: str) -> dict[str, Any]:
    """POST /v1/skills -- create a BRAND-NEW skill resource (its id AND its first
    version) from a zip file at `zip_path`. Used the FIRST time a candidate-owned
    skill (one with no `skill_id` recorded yet at all) is synced -- distinct from
    `create_skill_version()`, which pushes a new version to an ALREADY-EXISTING skill
    id.

    CONFIRMED LIVE (2026-07-06, agent-system-redesign epic Phase 3, a careful minimal
    probe -- not assumed from docs): this is the SAME multipart shape as
    `create_skill_version()` (`-F "files[]=@<zip>"`) AND, corrected by a second live
    probe (see `create_skill_version()`'s own docstring for the full story), enforces
    the SAME two constraints that endpoint also turned out to require:
      1. The zip's entries MUST sit inside a single top-level folder (not a bare
         `SKILL.md` at the archive root) -- confirmed via a real 400:
         `"Zip must contain a top-level folder with all files inside it, including
         SKILL.md"`.
      2. That top-level folder's name MUST match the `name:` field in the zipped
         `SKILL.md`'s YAML front matter exactly -- confirmed via a real 400:
         `"The folder name '<x>' must match the skill name '<y>' in SKILL.md."`
    `sync.py`'s `_zip_skill_source()` builds the zip with this shape for BOTH this
    function and `create_skill_version()` -- one zip-building function serves both,
    since the two endpoints' requirements turned out to be identical (an earlier,
    now-corrected assumption in this module's history held they differed; see
    `_zip_skill_source()`'s own docstring in `sync.py` for the full correction). The
    response carries both the new `id` and the version of this first upload -- both
    get recorded into `skills.json`."""
    with open(zip_path, "rb") as fh:
        response = client.post(
            "/v1/skills",
            files={"files[]": (os.path.basename(zip_path), fh, "application/zip")},
        )
    response.raise_for_status()
    return response.json()


def create_skill_version(client: httpx.Client, skill_id: str, zip_path: str) -> dict[str, Any]:
    """POST /v1/skills/{id}/versions -- push a new skill version from a zip file at
    `zip_path`, exactly the multipart shape documented in
    `deploy/managed-agent/README.md` (`-F "files[]=@<zip>"`). Skill versions are
    auto-assigned an epoch-timestamp version id -- the caller cannot choose one; the
    returned resource's version field is what gets recorded into `skills.json`.

    CORRECTED (2026-07-06, agent-system-redesign epic Phase 3, live-confirmed): this
    module previously assumed (never tested against the real API -- see
    `deploy/candidates/README.md`'s Phase 2 "Judgment calls" note) that this endpoint
    accepted a FLATTENED zip (no wrapping folder), unlike `create_skill()`'s
    creation endpoint. A real Phase 3 skill-version push using that flattened shape
    failed with a genuine 400: `"Zip must contain a top-level folder with all files
    inside it, including SKILL.md"` -- and a follow-up probe with a deliberately
    mismatched folder name confirmed the SAME `"folder name must match the skill
    name in SKILL.md"` check ALSO applies here. So this endpoint's zip-shape
    requirement is IDENTICAL to `create_skill()`'s -- `deploy/managed-agent/README.md`'s
    own documented version-push command (`cd deploy/managed-agent/skills; zip -r -q
    ... daily-ai-brief -x "*.DS_Store"`, run from one directory ABOVE the
    `daily-ai-brief/` folder) already produced this exact wrapping-folder shape by
    construction, which is why that real, earlier push never hit this bug -- this
    module's own `_zip_skill_source()` (in `sync.py`) originally flattened instead,
    and simply was never exercised against the real API until now."""
    with open(zip_path, "rb") as fh:
        response = client.post(
            f"/v1/skills/{skill_id}/versions",
            files={"files[]": (os.path.basename(zip_path), fh, "application/zip")},
        )
    response.raise_for_status()
    return response.json()
