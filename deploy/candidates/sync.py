#!/usr/bin/env python3
"""Sync a candidate declaration to live Claude Platform Agent resources.

Turns a git-tracked `deploy/candidates/<slug>/` directory into (or updates) real
Claude Platform Agent resource(s), per Decision 2c
(`docs/adr/0014-agent-system-redesign-topology.md`) and PRD FR-11/FR-12. This is a
PLAIN LOCAL SCRIPT -- no AWS, no CDK, no Lambda. It calls the Anthropic API directly.

Usage:
    export ANTHROPIC_API_KEY=$(cat ~/.anthropic-managed-agents/ant-api-key.txt)
    python3 sync.py <path-to-candidate-directory>

    # e.g., from this directory:
    python3 sync.py tests/fixtures/example-single-agent
    python3 sync.py tests/fixtures/example-multi-agent

The script rewrites the candidate's OWN tracked files (writing a newly-minted
`agent_id` at first sync, or a freshly-pushed skill version into `skills.json`) but
never runs `git add`/`git commit` itself -- review and commit the resulting diff
yourself. See README.md for the full schema/runbook documentation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `candidate_sync` importable regardless of the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from candidate_sync import api_client  # noqa: E402
from candidate_sync.loader import CandidateLoadError  # noqa: E402
from candidate_sync.sync import sync_candidate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("candidate_dir", help="Path to a candidate directory (e.g. deploy/candidates/production-baseline)")
    args = parser.parse_args(argv)

    candidate_dir = Path(args.candidate_dir).resolve()

    try:
        api_key = api_client.get_anthropic_api_key()
    except api_client.AnthropicApiKeyMissingError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        with api_client.build_agents_client(api_key) as agents_client, api_client.build_skills_client(
            api_key
        ) as skills_client:
            result = sync_candidate(candidate_dir, agents_client=agents_client, skills_client=skills_client)
    except CandidateLoadError as e:
        print(f"error loading candidate: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - surface a clean failure, never leak the API key
        print(f"error: sync failed: {e!r}", file=sys.stderr)
        return 1

    print(str(result))
    if result.created or result.updated or result.skill_versions_pushed:
        print(
            "\nReview the diff in the candidate directory (usually just a new "
            "agent_id/skill-version field) and commit it yourself -- this script "
            "does not run `git commit`."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
