#!/usr/bin/env python3
"""Trigger a real run against a candidate's `agent_id` + the shared `cloud`
environment, and print its produced content.

This is the reusable, infra-free, delivery-free candidate trigger-and-retrieve tool
(agent-system-redesign epic Phase 3, PRD FR-6/FR-7/FR-8, AC-6/AC-7/AC-8). It is a
PLAIN LOCAL SCRIPT -- no AWS, no CDK, no Lambda. It calls the Anthropic API directly,
exactly like sync.py.

Usage:
    export ANTHROPIC_API_KEY=$(cat ~/.anthropic-managed-agents/ant-api-key.txt)
    python3 trigger.py <path-to-candidate-directory> ["<task prompt override>"] [--timeout SECONDS]

    # e.g., against the real smoke-test-example candidate (already synced -- has a
    # real agent_id -- see README.md's "Phase 3 live validation" section):
    python3 trigger.py smoke-test-example

    # A real content-generation candidate's research/writing run takes meaningfully
    # longer than the trivial smoke test -- override the default 600s poll timeout
    # (see Phase 5 live validation in README.md for why 20 minutes was chosen there):
    python3 trigger.py production-baseline --timeout 1200

If no task-prompt override is given, the candidate's own `task-prompt.md` is used
(the same file `sync.py`/`candidate_sync.loader` reads for the agent's declared
per-run task). `--timeout` overrides `candidate_sync.trigger.DEFAULT_POLL_TIMEOUT_SECONDS`
(600s) -- raise it for any candidate whose task is a genuine research/writing job,
not a trivial smoke test.

Prints: the deployment id, the session id, the final status, and every file
successfully recovered via `cat` from the session's event stream (per
`candidate_sync.trigger.fetch_catted_file_contents()` -- the confirmed working
substitute for the refuted Files-API auto-`file_id` assumption, see Decision 1).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make `candidate_sync` importable regardless of the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from candidate_sync import api_client, trigger  # noqa: E402
from candidate_sync.loader import CandidateLoadError, load_candidate  # noqa: E402

_ENVIRONMENT_JSON_PATH = Path(__file__).resolve().parent / "environment.json"


def _load_shared_environment_id() -> str:
    """Read the ONE shared `cloud` environment's id from `environment.json` (see
    that file / README.md's 'The shared cloud environment' section for how it was
    created -- once, deliberately, via a real, confirmed POST /v1/environments
    call)."""
    if not _ENVIRONMENT_JSON_PATH.is_file():
        raise SystemExit(
            f"error: {_ENVIRONMENT_JSON_PATH} is missing -- the shared cloud environment "
            "must be created once (see README.md) before any candidate can be triggered."
        )
    data = json.loads(_ENVIRONMENT_JSON_PATH.read_text(encoding="utf-8"))
    environment_id = data.get("environment_id")
    if not environment_id:
        raise SystemExit(f"error: {_ENVIRONMENT_JSON_PATH} has no 'environment_id' field")
    return environment_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("candidate_dir", help="Path to a candidate directory (e.g. deploy/candidates/smoke-test-example)")
    parser.add_argument(
        "task_prompt_override",
        nargs="?",
        default=None,
        help="Optional task prompt to use instead of the candidate's own task-prompt.md",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=trigger.DEFAULT_POLL_TIMEOUT_SECONDS,
        help=(
            "Poll timeout in seconds (default: "
            f"{trigger.DEFAULT_POLL_TIMEOUT_SECONDS:.0f}s / "
            f"{trigger.DEFAULT_POLL_TIMEOUT_SECONDS / 60:.0f}min). Raise this for a "
            "real research/writing candidate task, which takes meaningfully longer "
            "than a trivial smoke test."
        ),
    )
    args = parser.parse_args(argv)

    candidate_dir = Path(args.candidate_dir).resolve()
    environment_id = _load_shared_environment_id()

    try:
        api_key = api_client.get_anthropic_api_key()
    except api_client.AnthropicApiKeyMissingError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        candidate = load_candidate(candidate_dir)
    except CandidateLoadError as e:
        print(f"error loading candidate: {e}", file=sys.stderr)
        return 1

    if candidate.agent.agent_id is None:
        print(
            f"error: candidate '{candidate.slug}' has no agent_id yet -- run sync.py against it first",
            file=sys.stderr,
        )
        return 1

    task_prompt = args.task_prompt_override or candidate.agent.task_prompt
    if not task_prompt:
        print(f"error: candidate '{candidate.slug}' has no task prompt (task-prompt.md is empty) and none was given", file=sys.stderr)
        return 1

    # Substitute __RECENT_BRIEFS_TOKEN__/__DELIVERY_BASE_URL__ if this candidate's
    # task prompt uses them (ADR-0014 Decision 2d's correction) -- a no-op for any
    # candidate whose prompt doesn't reference them. Reads $RECENT_BRIEFS_SIGNING_KEY/
    # $DELIVERY_BASE_URL from the environment; never an AWS call.
    try:
        task_prompt = trigger.substitute_recent_briefs_placeholders(task_prompt)
    except trigger.RecentBriefsPlaceholderConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    deployment_name = f"candidate-trigger-{candidate.slug}"

    try:
        with trigger.build_deployments_client(api_key) as deployments_client:
            result = trigger.run_candidate(
                deployments_client,
                agent_id=candidate.agent.agent_id,
                environment_id=environment_id,
                task_prompt=task_prompt,
                deployment_name=deployment_name,
                poll_timeout_seconds=args.timeout,
            )
    except (trigger.CandidateRunFailedError, trigger.CandidateRunTimeoutError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - surface a clean failure, never leak the API key
        print(f"error: trigger failed: {e!r}", file=sys.stderr)
        return 1

    print(f"candidate '{candidate.slug}': deployment={result.deployment_id} session={result.session_id} status={result.final_status}")

    catted_files = trigger.fetch_catted_file_contents(result.events)
    if not catted_files:
        print("(no cat'd file contents found in the session's event stream)")
    for path, content in catted_files.items():
        print(f"\n--- {path} ---")
        print(content)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
