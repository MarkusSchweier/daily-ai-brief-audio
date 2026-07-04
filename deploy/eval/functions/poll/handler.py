"""Scheduled poll-and-process Lambda (PRD docs/prd/eval-harness.md FR-1..FR-17), invoked
every 2 minutes by an EventBridge rule (see `brief_eval/stack.py`) -- NOT an HTTP API
route. A real pipeline run takes ~8-10 minutes end to end (README's own confirmed
~16-minute full run including Polly/SES), too long for a single Lambda invocation to
comfortably own synchronously, so this Lambda:

  1. Lists every `status="pending"` row in the eval-records table.
  2. For each, checks the session's status via the Sessions API.
  3. Once a session is idle/complete: fetches the run's archived artifacts from S3
     (brief.html/.md, listening-script.txt, candidates.json if present), runs the
     Phase 2 cost miner and the Phase 3 v1 judges, runs Phase 5 calibration when
     applicable, writes the structured record (Phase 4), archives the temporary
     deployment (mirroring README §6's create-then-archive discipline -- this is the
     "archive after" half; the trigger Lambda already did the "create new" half), and
     marks the row `status="complete"`.
  4. A session that fails (or a run whose artifacts never appear) is marked
     `status="failed"` after a bounded number of poll attempts, rather than polling
     forever -- see `MAX_POLL_ATTEMPTS` below.

Not gated by the reviewer secret (EventBridge invokes it directly, not the public
HTTP API) -- it has no public route at all.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EVAL_TABLE_NAME = os.environ.get("EVAL_TABLE_NAME", "brief-eval-records")
PIPELINE_BUCKET_NAME = os.environ.get("PIPELINE_BUCKET_NAME", "cowork-polly-tts-740353583786")
ANTHROPIC_API_KEY_SECRET_ARN = os.environ.get("ANTHROPIC_API_KEY_SECRET_ARN", "")
FEEDBACK_TABLE_NAME = os.environ.get("FEEDBACK_TABLE_NAME", "")

# A session that's been pending this many poll cycles (~2 min apart -> ~40 min) without
# completing is marked failed rather than polled forever -- a stuck/errored session
# must not silently accumulate as an ever-growing pending list.
MAX_POLL_ATTEMPTS = 20

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


def _session_is_terminal(session_status: str) -> bool:
    """The Sessions API reports a session's lifecycle state; `idle`/`complete`/
    `completed` are all treated as "the run finished, go fetch artifacts" -- exact
    terminal-state vocabulary was not independently re-verified against a live
    session at build time (flagged in this task's final report); this tolerates a
    couple of plausible spellings rather than hard-coding exactly one."""
    return session_status.lower() in ("idle", "complete", "completed", "finished")


def _session_failed(session_status: str) -> bool:
    return session_status.lower() in ("failed", "error", "errored")


def _fetch_text(s3_client, bucket: str, key: str) -> str | None:
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception:  # noqa: BLE001 - a missing artifact degrades to None, never raises
        return None


def _fetch_candidates_json(s3_client, bucket: str, key: str) -> list[dict] | None:
    raw = _fetch_text(s3_client, bucket, key)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None


def _process_completed_run(row: dict[str, Any], *, s3_client, dynamodb_client, judge_client, anthropic_api_key: str) -> dict[str, Any]:
    """Fetch artifacts, run the cost miner + v1 judges + calibration, build and
    return an `EvalRecord.to_dict()`-shaped dict (imported lazily below to keep this
    module importable without eval_core's dependencies during a bare handler test)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from eval_core import calibration, record
    from eval_core.cost_miner import fetch_session_cost
    from eval_core.judges import (
        judge_content_selection,
        judge_dedup,
        judge_factual_accuracy,
        judge_length_format,
    )

    session_id = row["sessionId"]
    run_id = row["runId"]
    candidate_config_id = row.get("candidateConfigId", "production")

    # Resolve which date this run archived to -- the pipeline archives under
    # briefs/<local-date>/ (brief_history.py); a run triggered "now" archives under
    # today's date in the pipeline's own timezone. This poll Lambda doesn't recompute
    # that timezone logic itself; it instead looks for the most recently created
    # briefs/<date>/ prefix, which is the run this poll cycle is processing (an eval
    # run is not concurrent with the live daily send in practice, so "most recent" is
    # an unambiguous match for the run just completed).
    import re

    paginator = s3_client.get_paginator("list_objects_v2")
    dated_prefixes = []
    for page in paginator.paginate(Bucket=PIPELINE_BUCKET_NAME, Prefix="briefs/", Delimiter="/"):
        for common_prefix in page.get("CommonPrefixes", []):
            match = re.match(r"^briefs/(\d{4}-\d{2}-\d{2})/$", common_prefix.get("Prefix", ""))
            if match:
                dated_prefixes.append(match.group(1))
    latest_date = max(dated_prefixes) if dated_prefixes else None

    brief_markdown = None
    listening_script = None
    candidates_json = None
    prior_briefs_markdown: list[str] = []
    if latest_date:
        brief_markdown = _fetch_text(s3_client, PIPELINE_BUCKET_NAME, f"briefs/{latest_date}/brief.md")
        listening_script = _fetch_text(s3_client, PIPELINE_BUCKET_NAME, f"briefs/{latest_date}/listening-script.txt")
        candidates_json = _fetch_candidates_json(s3_client, PIPELINE_BUCKET_NAME, f"briefs/{latest_date}/candidates.json")
        for prior_date in sorted((d for d in dated_prefixes if d < latest_date), reverse=True)[:3]:
            prior_markdown = _fetch_text(s3_client, PIPELINE_BUCKET_NAME, f"briefs/{prior_date}/brief.md")
            if prior_markdown:
                prior_briefs_markdown.append(prior_markdown)

    brief_markdown = brief_markdown or ""

    cost_breakdown = fetch_session_cost(anthropic_api_key, session_id)
    cost_record = record.CostBreakdownRecord(
        total_cost_usd=cost_breakdown.total_cost_usd,
        phase_costs_usd={p.phase: p.cost_usd for p in cost_breakdown.phase_totals},
        thread_costs_usd={t.thread_id: t.cost_usd for t in cost_breakdown.threads},
    )

    judge_results = [
        judge_content_selection(judge_client, candidates_json=candidates_json, brief_markdown=brief_markdown),
        judge_factual_accuracy(judge_client, brief_markdown=brief_markdown),
        judge_length_format(judge_client, brief_markdown=brief_markdown),
        judge_dedup(judge_client, brief_markdown=brief_markdown, prior_briefs_markdown=prior_briefs_markdown),
    ]
    criterion_scores = {
        jr.criterion: record.CriterionScore(criterion=jr.criterion, score=jr.score, rationale=jr.rationale, evidence=jr.evidence, insufficient_data=jr.insufficient_data)
        for jr in judge_results
    }

    eval_record = record.EvalRecord(
        run_id=run_id,
        candidate_config_id=candidate_config_id,
        session_id=session_id,
        created_at=int(time.time()),
        criterion_scores=criterion_scores,
        cost=cost_record,
        # AC-18: the review UI's detail view shows the brief content and its
        # listening script side by side with the judge scores -- inlined directly
        # (see EvalRecord's docstring/comment for the size-vs-S3-pointer reasoning).
        brief_markdown=brief_markdown or None,
        listening_script=listening_script,
    )

    # Calibration (FR-15) is opportunistic -- run it if the feedback table is
    # configured, but it must never fail the run's completion (PRD §7 "insufficient
    # feedback to calibrate" degrade path). Also surfaces reader free-text
    # suggestions (extract_free_text_feedback()) into the review context, per FR-15
    # -- previously implemented and unit-tested but never actually wired in here.
    calibration_correlations = None
    free_text_feedback = None
    if FEEDBACK_TABLE_NAME:
        try:
            feedback_table = dynamodb_client.Table(FEEDBACK_TABLE_NAME)
            feedback_rows = calibration.query_feedback_table(feedback_table)
            judge_scores_by_date = {latest_date: {c: s.score for c, s in criterion_scores.items() if s.score is not None}} if latest_date else {}
            calibration_correlations = calibration.correlate_judge_vs_reader(judge_scores_by_date, feedback_rows)
            free_text_feedback = calibration.extract_free_text_feedback(feedback_rows)
        except Exception as e:  # noqa: BLE001 - calibration must never fail the run
            logger.warning("EVAL_CALIBRATION_FAILED run_id=%s error=%r", run_id, e)

    result = eval_record.to_dict()
    if calibration_correlations or free_text_feedback:
        result["calibration"] = {
            **{k: v.__dict__ for k, v in (calibration_correlations or {}).items()},
            "free_text_feedback": free_text_feedback or [],
        }
    return result


def _archive_deployment(client, deployment_id: str) -> None:
    """POST /v1/deployments/{id}/archive -- the "archive after" half of README §6's
    create-then-archive discipline, applied here to the TEMPORARY eval deployment
    (never the live scheduled one, which this Lambda never touches)."""
    response = client.post(f"/v1/deployments/{deployment_id}/archive")
    response.raise_for_status()


def _archive_deployment_best_effort(client, row: dict[str, Any], run_id: str) -> None:
    """Best-effort wrapper around `_archive_deployment()` for every terminal branch
    (success, session-failed, poll-timeout, processing-exception) -- a leaked
    temporary eval deployment left "active" forever is a real, if low-severity,
    security/cost surface (FIX 6): it's a live, on-demand deployment against the
    SAME production agent/environment the real pipeline uses, so a forgotten
    archive leaves an extra callable surface around indefinitely. Wrapped in its own
    try/except so an archive-endpoint failure never un-fails/un-complete a row's
    already-decided status, and never crashes the rest of this poll cycle."""
    deployment_id = row.get("deploymentId")
    if not deployment_id:
        return
    try:
        _archive_deployment(client, deployment_id)
    except Exception as e:  # noqa: BLE001 - archival failure must never affect row status
        logger.warning("EVAL_DEPLOYMENT_ARCHIVE_FAILED run_id=%s deployment_id=%s error=%r", run_id, deployment_id, e)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    import httpx

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(EVAL_TABLE_NAME)
    s3_client = boto3.client("s3")

    api_key = _get_anthropic_api_key()
    anthropic_client = httpx.Client(
        base_url="https://api.anthropic.com",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": _ANTHROPIC_BETA_HEADER,
        },
        timeout=30.0,
    )

    import anthropic as anthropic_sdk

    judge_client = anthropic_sdk.Anthropic(api_key=api_key)

    return _handle(table, s3_client, dynamodb, anthropic_client, judge_client, api_key)


def _handle(table, s3_client, dynamodb_resource, deployments_client, judge_client, anthropic_api_key: str) -> dict[str, Any]:
    processed = 0
    failed = 0

    pending_rows = [item for item in table.scan().get("Items", []) if item.get("status") == "pending"]

    for row in pending_rows:
        run_id = row["runId"]
        session_id = row["sessionId"]
        poll_count = int(row.get("pollCount", 0))

        try:
            session_response = deployments_client.get(f"/v1/sessions/{session_id}")
            session_response.raise_for_status()
            session_status = session_response.json().get("status", "")
        except Exception as e:  # noqa: BLE001 - a transient poll failure is retried next cycle
            logger.warning("EVAL_POLL_SESSION_STATUS_FAILED run_id=%s error=%r", run_id, e)
            table.update_item(
                Key={"runId": run_id},
                UpdateExpression="SET pollCount = :n",
                ExpressionAttributeValues={":n": poll_count + 1},
            )
            continue

        if _session_failed(session_status):
            logger.info("EVAL_SESSION_FAILED run_id=%s session_id=%s", run_id, session_id)
            table.update_item(Key={"runId": run_id}, UpdateExpression="SET #s = :failed", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":failed": "failed"})
            _archive_deployment_best_effort(deployments_client, row, run_id)
            failed += 1
            continue

        if not _session_is_terminal(session_status):
            if poll_count + 1 >= MAX_POLL_ATTEMPTS:
                logger.warning("EVAL_SESSION_POLL_TIMEOUT run_id=%s session_id=%s", run_id, session_id)
                table.update_item(Key={"runId": run_id}, UpdateExpression="SET #s = :failed", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":failed": "failed"})
                _archive_deployment_best_effort(deployments_client, row, run_id)
                failed += 1
            else:
                table.update_item(Key={"runId": run_id}, UpdateExpression="SET pollCount = :n", ExpressionAttributeValues={":n": poll_count + 1})
            continue

        try:
            eval_record_dict = _process_completed_run(
                row, s3_client=s3_client, dynamodb_client=dynamodb_resource, judge_client=judge_client, anthropic_api_key=anthropic_api_key
            )
            table.update_item(
                Key={"runId": run_id},
                UpdateExpression="SET #s = :complete, #r = :record",
                ExpressionAttributeNames={"#s": "status", "#r": "record"},
                ExpressionAttributeValues={":complete": "complete", ":record": json.dumps(eval_record_dict)},
            )
            _archive_deployment_best_effort(deployments_client, row, run_id)
            processed += 1
            logger.info("EVAL_PROCESSED run_id=%s", run_id)
        except Exception as e:  # noqa: BLE001 - a processing failure marks the run failed, never crashes the poller for the rest
            logger.error("EVAL_PROCESSING_FAILED run_id=%s error=%r", run_id, e)
            table.update_item(Key={"runId": run_id}, UpdateExpression="SET #s = :failed", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":failed": "failed"})
            _archive_deployment_best_effort(deployments_client, row, run_id)
            failed += 1

    return {"processed": processed, "failed": failed, "pending_seen": len(pending_rows)}
