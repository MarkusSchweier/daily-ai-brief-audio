"""POST /trigger — start a new evaluation run of the current production candidate
configuration (PRD docs/prd/eval-harness.md FR-1/FR-2, AC-1/AC-2).

Mirrors the exact create-new/POST-.../run/archive-after mechanism documented in
`deploy/managed-agent/README.md` §6 ("Create/update the scheduled deployment" --
deployments are immutable, confirmed live 2026-07-04) and §7 ("Verify end-to-end" --
manual-trigger flow): this Lambda creates a TEMPORARY Deployments-API deployment
targeting the current production agent/environment (never the live scheduled
deployment itself), with `SKIP_SUBSCRIBER_FANOUT=1` baked into its initial prompt's
env exports so an evaluation run can NEVER reach a real subscriber (PRD FR-1/FR-22),
triggers one session against it, records a pending evaluation row, and returns the new
session id. The "poll and process" Lambda (functions/poll/) later archives this
temporary deployment once the session completes (mirroring README §6 step 3's
create-then-archive discipline -- this Lambda does not archive it inline, since the
session is still running when this handler returns).

Defense in depth (security fix, 2026-07-04): `audio_email.py`'s subscriber fan-out
gate is now an opt-IN `ENABLE_SUBSCRIBER_FANOUT` (defaults OFF unless explicitly
asserted -- see `deploy/managed-agent/pipeline/audio_email.py`'s
`_resolve_skip_subscriber_fanout()`), so this Lambda's prompt not mentioning it is
already safe by construction, with or without the `SKIP_SUBSCRIBER_FANOUT=1` export
above. `_handle()` additionally rejects the trigger request with a 400 if the FINAL
assembled prompt (including any caller-supplied `basePrompt`, which is prepended
ahead of this module's own safety instruction) contains the literal string
`ENABLE_SUBSCRIBER_FANOUT` anywhere, closing off a caller injecting/contradicting
the safety instruction via `basePrompt`.

Gated by the shared reviewer bearer secret (ADR-0013 §E) -- only a reviewer can
trigger an evaluation run, since each run costs real Claude API usage.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3

import review_auth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EVAL_TABLE_NAME = os.environ.get("EVAL_TABLE_NAME", "brief-eval-records")
ANTHROPIC_API_KEY_SECRET_ARN = os.environ.get("ANTHROPIC_API_KEY_SECRET_ARN", "")

# The current production agent + self_hosted environment this stack targets for
# evaluation runs -- the SAME agent/environment `deploy/managed-agent/deployment.json`
# uses for the live scheduled send, per PRD FR-1's "same replay / temporary-deployment
# mechanism already established" requirement (this is deliberately NOT a second,
# parallel way to run the pipeline -- it reuses the identical agent/environment/skill
# the live deployment does, only the deployment (schedule wrapper) is temporary and
# scoped to one on-demand run).
PRODUCTION_AGENT_ID = os.environ.get("PRODUCTION_AGENT_ID", "")
PRODUCTION_ENVIRONMENT_ID = os.environ.get("PRODUCTION_ENVIRONMENT_ID", "")

DEFAULT_CANDIDATE_CONFIG_ID = "production"

_ANTHROPIC_BETA_HEADER = "managed-agents-2026-04-01"
_secret_cache: str | None = None


def _get_anthropic_api_key() -> str:
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=ANTHROPIC_API_KEY_SECRET_ARN)
    _secret_cache = response["SecretString"]
    return _secret_cache


def _build_initial_prompt(base_prompt: str) -> str:
    """The eval trigger's initial prompt is the SAME task the live deployment runs
    (research -> write -> narrate -> deliver), with one addition: export
    SKIP_SUBSCRIBER_FANOUT=1 before the delivery step, so the run never reaches a real
    subscriber (PRD FR-1/FR-22) -- see audio_email.py's existing, already-shipped
    manual-validation-only escape hatch (send_all()'s skip_subscriber_fanout
    parameter), which this reuses rather than inventing a second mechanism."""
    return (
        base_prompt
        + " IMPORTANT (evaluation run only): before running the delivery step "
        "(`python3.13 /opt/pipeline/audio_email.py`), also export "
        "SKIP_SUBSCRIBER_FANOUT=1 -- this is an evaluation run and must NEVER reach a "
        "real subscriber; only the owner's own copy (if any) is sent, and this env "
        "var causes the subscriber fan-out to be skipped entirely (see "
        "audio_email.py's existing SKIP_SUBSCRIBER_FANOUT support)."
    )


def _create_temporary_deployment(client, *, agent_id: str, environment_id: str, initial_prompt: str) -> str:
    """POST /v1/deployments -- create a new, one-off (non-cron -- no `schedule` field,
    per the Deployments API's support for on-demand deployments used by README §7's
    manual-trigger flow) temporary deployment. Returns the new deployment's id.

    NOTE: this repo's confirmed Deployments-API knowledge (README §6) covers the
    create-new-then-archive mechanism for a SCHEDULED (cron) deployment; an on-demand
    deployment omitting `schedule` was not independently re-verified against a live
    API call while building this trigger Lambda (no live session existed to test
    against at build time) -- flagged explicitly in this task's final report as
    something the orchestrating session should confirm against the real API before
    relying on this in production.
    """
    response = client.post(
        "/v1/deployments",
        json={
            "agent": agent_id,
            "environment_id": environment_id,
            "initial_events": [{"type": "user.message", "content": [{"type": "text", "text": initial_prompt}]}],
        },
    )
    response.raise_for_status()
    return response.json()["id"]


def _start_session(client, deployment_id: str) -> str:
    """POST /v1/deployments/{id}/sessions -- trigger one session against the
    just-created temporary deployment (mirrors README §7's manual "run now" trigger).
    Returns the new session id."""
    response = client.post(f"/v1/deployments/{deployment_id}/sessions")
    response.raise_for_status()
    return response.json()["id"]


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if not review_auth.is_authorized(event):
        return review_auth.unauthorized_response()

    import httpx

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(EVAL_TABLE_NAME)

    api_key = _get_anthropic_api_key()
    with httpx.Client(
        base_url="https://api.anthropic.com",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": _ANTHROPIC_BETA_HEADER,
        },
        timeout=30.0,
    ) as client:
        return _handle(event, table, client)


def _handle(event: dict[str, Any], table, client) -> dict[str, Any]:
    payload = {}
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        pass
    candidate_config_id = payload.get("candidateConfigId") or DEFAULT_CANDIDATE_CONFIG_ID
    base_prompt = payload.get("basePrompt") or ""

    initial_prompt = _build_initial_prompt(base_prompt)

    # Defense in depth (security fix, FIX 7): audio_email.py's subscriber fan-out
    # gate is now an opt-IN ENABLE_SUBSCRIBER_FANOUT (default OFF unless explicitly
    # asserted -- see deploy/managed-agent/pipeline/audio_email.py's
    # _resolve_skip_subscriber_fanout()), so an eval run's prompt not mentioning it
    # at all is already safe by construction. This check additionally rejects the
    # trigger outright if the string ever appears anywhere in the FINAL assembled
    # prompt -- including a caller-supplied `base_prompt`, which is prepended AHEAD
    # of this module's own safety instruction and could otherwise inject/contradict
    # it (e.g. a malicious or buggy caller asserting ENABLE_SUBSCRIBER_FANOUT=1
    # earlier in the prompt, hoping the agent honors the first instruction it sees).
    if "ENABLE_SUBSCRIBER_FANOUT" in initial_prompt:
        logger.error("EVAL_TRIGGER_REJECTED_UNSAFE_PROMPT run_id=none reason=ENABLE_SUBSCRIBER_FANOUT_present")
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": "prompt must not reference ENABLE_SUBSCRIBER_FANOUT"}),
        }

    try:
        deployment_id = _create_temporary_deployment(
            client,
            agent_id=PRODUCTION_AGENT_ID,
            environment_id=PRODUCTION_ENVIRONMENT_ID,
            initial_prompt=initial_prompt,
        )
        session_id = _start_session(client, deployment_id)
    except Exception as e:  # noqa: BLE001 - surface a clean 502, never leak internals
        logger.error("EVAL_TRIGGER_FAILED error=%r", e)
        return {
            "statusCode": 502,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": "trigger_failed"}),
        }

    run_id = uuid.uuid4().hex
    table.put_item(
        Item={
            "runId": run_id,
            "status": "pending",
            "candidateConfigId": candidate_config_id,
            "sessionId": session_id,
            "deploymentId": deployment_id,
            "createdAt": int(time.time()),
        }
    )
    logger.info("EVAL_TRIGGERED run_id=%s session_id=%s deployment_id=%s", run_id, session_id, deployment_id)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True, "runId": run_id, "sessionId": session_id}),
    }
