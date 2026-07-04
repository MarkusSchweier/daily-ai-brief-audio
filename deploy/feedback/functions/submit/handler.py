"""POST /submit — accept one reader-feedback submission and persist it durably.

See docs/prd/reader-feedback.md FR-6..FR-15 and
docs/adr/0012-feedback-standalone-stack-and-token-helper-packaging.md §B "Submit
handler behavior" for the exact behavior this implements. Runs with the
least-privilege role in `brief_feedback/stack.py`'s `SubmitFunctionRole`
(dynamodb:PutItem on the one table, secretsmanager:GetSecretValue on the one signing
secret — no SES, no other table/bucket access).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3

import feedback_token

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FEEDBACK_TABLE_NAME = os.environ.get("FEEDBACK_TABLE_NAME", "brief-feedback")
FEEDBACK_TOKEN_SECRET_ARN = os.environ.get("FEEDBACK_TOKEN_SECRET_ARN", "")

# The seven graded questions (PRD FR-6), all optional, each 1-5 when present.
GRADED_QUESTION_KEYS = (
    "overallRating",
    "contentSelection",
    "contentRepresentation",
    "contentCorrectness",
    "contentComprehensiveness",
    "length",
    "technicalDepth",
)

# The two free-text questions (PRD FR-7), both optional, length-capped server-side.
FREE_TEXT_QUESTION_KEYS = ("additionalSources", "otherFeedback")
FREE_TEXT_MAX_LENGTH = 2000

_CORS_HEADERS = {"Content-Type": "application/json"}

# A generic, honeypot-indistinguishable success body (PRD FR-4/AC-2): the caller
# cannot tell "real submission accepted" apart from "honeypot tripped, nothing
# persisted" from the HTTP response.
_SUCCESS_BODY = json.dumps({"ok": True})


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status_code, "headers": _CORS_HEADERS, "body": json.dumps(body)}


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _validate_graded_answers(payload: dict[str, Any]) -> tuple[dict[str, int], bool]:
    """Return (validated_answers, ok). Each of the seven keys, when present in the
    payload, must be an int 1-5 (bool is deliberately rejected -- Python's bool is an
    int subclass, and a bare true/false is not a valid 1-5 rating). Absent keys are
    simply omitted from the result (PRD FR-6: a partial set is valid, AC-3)."""
    answers: dict[str, int] = {}
    for key in GRADED_QUESTION_KEYS:
        if key not in payload or payload[key] is None or payload[key] == "":
            continue
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return {}, False
        if value < 1 or value > 5:
            return {}, False
        answers[key] = value
    return answers, True


def _validate_free_text_answers(payload: dict[str, Any]) -> tuple[dict[str, str], bool]:
    """Return (validated_answers, ok). Each of the two keys, when present, must be a
    string no longer than FREE_TEXT_MAX_LENGTH -- an over-length answer is rejected
    inline with no partial record written (PRD FR-7/FR-15, AC-4), never silently
    truncated. Free text is treated as data only (never reflected unescaped) -- it is
    a DynamoDB attribute value, not interpolated anywhere (PRD §7 injection concern)."""
    answers: dict[str, str] = {}
    for key in FREE_TEXT_QUESTION_KEYS:
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if not isinstance(value, str):
            return {}, False
        if len(value) > FREE_TEXT_MAX_LENGTH:
            return {}, False
        if value:
            answers[key] = value
    return answers, True


_secret_cache: str | None = None


def _get_signing_secret() -> str | None:
    """Fetch the feedback-token signing secret once per cold start (module-level
    cache), mirroring the launcher's `_get_secret` shape (ADR-0011 "Where the signing
    secret lives"). Returns None (never raises) when the ARN is unset or the fetch
    fails -- a submission must still be accepted (as walk-up anonymous) even if the
    secret is unavailable."""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    if not FEEDBACK_TOKEN_SECRET_ARN:
        return None
    try:
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=FEEDBACK_TOKEN_SECRET_ARN)
        _secret_cache = response["SecretString"]
        return _secret_cache
    except Exception as e:  # noqa: BLE001 - never block a submission over a secret-fetch glitch
        logger.warning("FEEDBACK_SECRET_FETCH_FAILED error=%r", e)
        return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(FEEDBACK_TABLE_NAME)
    return _handle(event, table)


def _handle(event: dict[str, Any], table, secret: str | None = "__UNSET__") -> dict[str, Any]:
    payload = _parse_body(event)

    # Honeypot: a bot-filled hidden field. Return a normal-looking success response
    # with no record written (PRD FR-4, AC-2).
    if (payload.get("website") or "").strip():
        logger.info("FEEDBACK_HONEYPOT_TRIPPED")
        return _response(200, {"ok": True})

    graded_answers, graded_ok = _validate_graded_answers(payload)
    if not graded_ok:
        return _response(400, {"ok": False, "error": "invalid graded answer"})

    free_text_answers, free_text_ok = _validate_free_text_answers(payload)
    if not free_text_ok:
        return _response(400, {"ok": False, "error": "free-text answer too long"})

    # Resolve the signing secret. `secret="__UNSET__"` (the default) means "fetch it
    # normally"; tests may pass an explicit secret (including None) to exercise the
    # missing-secret path without touching Secrets Manager.
    if secret == "__UNSET__":
        secret = _get_signing_secret()

    token_result = feedback_token.validate(secret, payload.get("t")) if secret else feedback_token.FeedbackTokenResult(valid=False)

    anonymous_checkbox = bool(payload.get("anonymous"))

    # Anonymity resolution (PRD FR-8/FR-9/FR-10/FR-11, ADR-0011 "Identity, attribution,
    # and anonymity interaction"):
    #   - anonymous checkbox set OR token invalid/absent -> no identity, no raw token
    #     persisted; but the brief date MAY still be persisted when the token was
    #     valid, even if the checkbox was checked (FR-11: date is not identifying).
    #   - checkbox unchecked AND token valid -> persist identity + briefDate.
    anonymous = anonymous_checkbox or not token_result.valid
    identity = None if anonymous else token_result.identity
    brief_date = token_result.brief_date if token_result.valid else None

    submission_id = uuid.uuid4().hex
    item: dict[str, Any] = {
        "submissionId": submission_id,
        "createdAt": int(time.time()),
        "anonymous": anonymous,
    }
    item.update(graded_answers)
    item.update(free_text_answers)
    if not anonymous and identity:
        item["identity"] = identity
    if brief_date:
        item["briefDate"] = brief_date

    try:
        table.put_item(Item=item)
    except Exception as e:  # noqa: BLE001 - never leak a stack trace to the caller
        # Never log identity on a failure path either -- log only the submission id and
        # anonymity flag (PRD §6 "no identity in logs on the persisted path").
        logger.error("FEEDBACK_PUT_FAILED submission_id=%s error=%r", submission_id, e)
        return _response(500, {"ok": False, "error": "could not save feedback"})

    # Never log identity for anonymous submissions; for attributed ones, log only that
    # attribution happened, not the identity value itself (keeps this log line uniform
    # regardless of anonymity, so a log-scanning habit can't accidentally leak PII by
    # branching).
    logger.info(
        "FEEDBACK_SUBMITTED submission_id=%s anonymous=%s attributed=%s",
        submission_id,
        anonymous,
        bool(not anonymous and identity),
    )
    return _response(200, {"ok": True})
