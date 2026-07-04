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

FIDELITY FIX, confirmed live 2026-07-04 (independent fork-session finding, verified
against a real session transcript): the default (no caller-supplied `basePrompt`)
eval prompt used to be a short, hand-written sentence, NOT the actual production
task -- despite this module's own docstring/comment claiming otherwise. The real
run proceeded anyway because the agent improvised the whole task from its own
system prompt and by exploring the filesystem, but it reconstructed the task
IMPERFECTLY (confirmed missing `PIPELINE_TIMEZONE`, `SUBSCRIBERS_TABLE_NAME`,
`SUBSCRIBERS_API_BASE_URL`, `FEEDBACK_TOKEN_SECRET_ARN`, `FEEDBACK_BASE_URL`, and
using a differently-formatted `EMAIL_SUBJECT` than production's real one). Since the
harness's entire point (PRD FR-1) is measuring "the current production
configuration" as a trustworthy baseline, an improvised task threatens both that
baseline's validity and replicate-variance measurement (FR-3) -- three "replicates"
weren't even guaranteed to run under an identical task. Fixed by deriving the
default prompt from `deployment.json`'s own real `agent.initial_prompt`, fetched
from an SSM Parameter the CDK stack populates at deploy time from that same file
(`brief_eval/stack.py`'s `_build_production_prompt_parameter()`) -- not bundled as a
file (avoids a cross-directory Docker-bundling story) and not a Lambda environment
variable (deployment.json's prompt is ~3KB, and Lambda's *combined* env-var size
limit is 4KB total, too tight alongside this function's other env vars).

Defense in depth (security fix, 2026-07-04): `audio_email.py`'s subscriber fan-out
gate is now an opt-IN `ENABLE_SUBSCRIBER_FANOUT` (defaults OFF unless explicitly
asserted -- see `deploy/managed-agent/pipeline/audio_email.py`'s
`_resolve_skip_subscriber_fanout()`), so this Lambda's prompt not mentioning it is
already safe by construction, with or without the `SKIP_SUBSCRIBER_FANOUT=1` export
above. The production prompt fetched from SSM DOES contain a real
`ENABLE_SUBSCRIBER_FANOUT=1` clause (it's production's own live task text, which
must assert it) -- `_build_initial_prompt()` strips exactly that clause out before
use, and `_handle()` separately rejects the trigger request with a 400 if the FINAL
assembled prompt (including any caller-supplied `basePrompt`, which is used
verbatim, NOT auto-stripped, and is prepended ahead of this module's own safety
instruction) still contains the literal string `ENABLE_SUBSCRIBER_FANOUT` anywhere
-- a fail-loud self-check in case the strip regex ever stops matching (e.g. the
production wording changes), rather than silently sending an unsafe prompt.

Gated by the shared reviewer bearer secret (ADR-0013 §E) -- only a reviewer can
trigger an evaluation run, since each run costs real Claude API usage.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any

import boto3

import review_auth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EVAL_TABLE_NAME = os.environ.get("EVAL_TABLE_NAME", "brief-eval-records")
ANTHROPIC_API_KEY_SECRET_ARN = os.environ.get("ANTHROPIC_API_KEY_SECRET_ARN", "")
PRODUCTION_PROMPT_PARAM_NAME = os.environ.get("PRODUCTION_PROMPT_PARAM_NAME", "")

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
_production_prompt_cache: str | None = None

# Matches production deployment.json's real `ENABLE_SUBSCRIBER_FANOUT=1 (...)` clause
# -- a comma-plus-whitespace-prefixed item in a longer sentence, followed by a single
# (non-nested, confirmed against the real text) parenthetical -- so the fetched
# production prompt can be reused for an eval run without carrying over the one
# instruction that must never appear in one (see module docstring).
_ENABLE_FANOUT_CLAUSE_RE = re.compile(r",?\s*ENABLE_SUBSCRIBER_FANOUT=1\s*\([^)]*\)")


def _get_anthropic_api_key() -> str:
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=ANTHROPIC_API_KEY_SECRET_ARN)
    _secret_cache = response["SecretString"]
    return _secret_cache


def _fetch_production_task_prompt(ssm_client) -> str:
    """Fetch deployment.json's real `agent.initial_prompt`, snapshotted into an SSM
    Parameter by the CDK stack at deploy time (see `brief_eval/stack.py`'s
    `_build_production_prompt_parameter()`). Cached per warm Lambda container,
    mirroring `_get_anthropic_api_key()`'s pattern."""
    global _production_prompt_cache
    if _production_prompt_cache is not None:
        return _production_prompt_cache
    response = ssm_client.get_parameter(Name=PRODUCTION_PROMPT_PARAM_NAME)
    _production_prompt_cache = response["Parameter"]["Value"]
    return _production_prompt_cache


def _build_initial_prompt(base_prompt: str, production_task_prompt: str = "") -> str:
    """The eval trigger's initial prompt is the SAME task the live deployment runs
    (research -> write -> narrate -> deliver), with one addition: export
    SKIP_SUBSCRIBER_FANOUT=1 before the delivery step, so the run never reaches a real
    subscriber (PRD FR-1/FR-22) -- see audio_email.py's existing, already-shipped
    manual-validation-only escape hatch (send_all()'s skip_subscriber_fanout
    parameter), which this reuses rather than inventing a second mechanism.

    If `base_prompt` is supplied (a caller-specified candidate task), it is used
    verbatim -- NOT auto-sanitized -- so an unexpected `ENABLE_SUBSCRIBER_FANOUT`
    inside it still hits the caller-facing reject-if-present check in `_handle()`
    rather than being silently altered. Otherwise, `production_task_prompt` (the
    real `deployment.json` text, fetched from SSM) is used, with its own
    production-only `ENABLE_SUBSCRIBER_FANOUT=1` clause stripped -- see
    `_ENABLE_FANOUT_CLAUSE_RE`."""
    task_prompt = base_prompt if base_prompt else _ENABLE_FANOUT_CLAUSE_RE.sub("", production_task_prompt)
    return (
        task_prompt
        + " IMPORTANT (evaluation run only): before running the delivery step "
        "(`python3.13 /opt/pipeline/audio_email.py`), also export "
        "SKIP_SUBSCRIBER_FANOUT=1 -- this is an evaluation run and must NEVER reach a "
        "real subscriber; only the owner's own copy (if any) is sent, and this env "
        "var causes the subscriber fan-out to be skipped entirely (see "
        "audio_email.py's existing SKIP_SUBSCRIBER_FANOUT support)."
    )


def _create_temporary_deployment(client, *, agent_id: str, environment_id: str, initial_prompt: str, name: str) -> str:
    """POST /v1/deployments -- create a new, one-off (non-cron -- no `schedule` field,
    per the Deployments API's support for on-demand deployments used by README §7's
    manual-trigger flow) temporary deployment. Returns the new deployment's id.

    CONFIRMED LIVE (2026-07-04) against the real Deployments API: a top-level `name`
    field is REQUIRED (`"name: Field required"` otherwise) -- omitting `schedule`
    entirely is correct and produces `"schedule": null` with `"status": "active"`, no
    error. This was the one gap in this repo's prior confirmed knowledge (README §6
    only covered the SCHEDULED/cron create-new-then-archive shape); verified directly
    via a real probe deployment (created, confirmed active, immediately archived).
    """
    response = client.post(
        "/v1/deployments",
        json={
            "name": name,
            "agent": agent_id,
            "environment_id": environment_id,
            "initial_events": [{"type": "user.message", "content": [{"type": "text", "text": initial_prompt}]}],
        },
    )
    response.raise_for_status()
    return response.json()["id"]


def _start_session(client, deployment_id: str) -> str:
    """POST /v1/deployments/{id}/run -- trigger one session against the just-created
    temporary deployment (mirrors README §7's manual "run now" trigger). Returns the
    new session id.

    CONFIRMED LIVE (2026-07-04): the endpoint is `/run`, not `/sessions` (`/sessions`
    404s). The response is a `deployment_run` object; the session id is under
    `session_id`, not `id` (`id` on that object is the `drun_...` run id, a different
    resource). Verified via a real probe deployment + run (session reached `status:
    "idle"` -- already-correctly recognized as terminal by poll/handler.py's
    `_session_is_terminal()` -- then both were archived)."""
    response = client.post(f"/v1/deployments/{deployment_id}/run")
    response.raise_for_status()
    return response.json()["session_id"]


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if not review_auth.is_authorized(event):
        return review_auth.unauthorized_response()

    import httpx

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(EVAL_TABLE_NAME)
    ssm_client = boto3.client("ssm")

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
        return _handle(event, table, client, ssm_client)


def _handle(event: dict[str, Any], table, client, ssm_client) -> dict[str, Any]:
    payload = {}
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        pass
    candidate_config_id = payload.get("candidateConfigId") or DEFAULT_CANDIDATE_CONFIG_ID
    base_prompt = payload.get("basePrompt") or ""

    # Only fetch the production prompt snapshot when it's actually needed (no
    # caller-supplied base_prompt) -- avoids a needless SSM call otherwise.
    production_task_prompt = "" if base_prompt else _fetch_production_task_prompt(ssm_client)
    initial_prompt = _build_initial_prompt(base_prompt, production_task_prompt)

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

    run_id = uuid.uuid4().hex

    try:
        deployment_id = _create_temporary_deployment(
            client,
            agent_id=PRODUCTION_AGENT_ID,
            environment_id=PRODUCTION_ENVIRONMENT_ID,
            initial_prompt=initial_prompt,
            name=f"eval-{candidate_config_id}-{run_id[:8]}",
        )
        session_id = _start_session(client, deployment_id)
    except Exception as e:  # noqa: BLE001 - surface a clean 502, never leak internals
        logger.error("EVAL_TRIGGER_FAILED error=%r", e)
        return {
            "statusCode": 502,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": "trigger_failed"}),
        }

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
