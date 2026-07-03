"""Unit tests for the feedback submit Lambda handler (docs/prd/reader-feedback.md
FR-6..FR-15, docs/adr/0012 §B "Submit handler behavior").

Covers: honeypot (AC-2), partial graded answers (AC-3), free-text length cap (AC-4),
attributed-when-not-anonymous (AC-8), anonymous-suppresses-identity (AC-9), walk-up
anonymous (AC-10), tamper-rejected-no-forgery (AC-11), durable record + no email
(AC-13), and that identity is never written to logs on the persisted path.
"""

from __future__ import annotations

import json
from decimal import Decimal

import feedback_token
from conftest import import_submit_handler

submit_handler = import_submit_handler()

SECRET = "test-signing-secret"


def _event(body: dict) -> dict:
    return {"body": json.dumps(body)}


def _get_item(table, submission_id: str) -> dict:
    resp = table.get_item(Key={"submissionId": submission_id})
    return resp["Item"]


def _all_items(table) -> list[dict]:
    return table.scan()["Items"]


def test_honeypot_filled_returns_normal_looking_success_with_no_record(feedback_table):
    resp = submit_handler._handle(
        _event({"overallRating": 5, "website": "http://spam.example"}), feedback_table, secret=SECRET
    )

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["ok"] is True
    assert _all_items(feedback_table) == []


def test_partial_graded_answers_are_accepted_and_persisted(feedback_table):
    resp = submit_handler._handle(
        _event({"overallRating": 4, "length": 2}), feedback_table, secret=SECRET
    )

    assert resp["statusCode"] == 200
    (item,) = _all_items(feedback_table)
    assert item["overallRating"] == 4
    assert item["length"] == 2
    assert "contentSelection" not in item


def test_all_graded_questions_optional_empty_submission_accepted(feedback_table):
    resp = submit_handler._handle(_event({}), feedback_table, secret=SECRET)

    assert resp["statusCode"] == 200
    (item,) = _all_items(feedback_table)
    for key in submit_handler.GRADED_QUESTION_KEYS:
        assert key not in item


def test_graded_answer_out_of_range_is_rejected_no_partial_record(feedback_table):
    resp = submit_handler._handle(_event({"overallRating": 6}), feedback_table, secret=SECRET)

    assert resp["statusCode"] == 400
    assert _all_items(feedback_table) == []


def test_graded_answer_zero_is_rejected(feedback_table):
    resp = submit_handler._handle(_event({"overallRating": 0}), feedback_table, secret=SECRET)

    assert resp["statusCode"] == 400
    assert _all_items(feedback_table) == []


def test_graded_answer_non_integer_is_rejected(feedback_table):
    resp = submit_handler._handle(_event({"overallRating": "five"}), feedback_table, secret=SECRET)

    assert resp["statusCode"] == 400
    assert _all_items(feedback_table) == []


def test_graded_answer_boolean_is_rejected(feedback_table):
    # Python's bool is an int subclass -- must be explicitly rejected, not accepted as 1/0.
    resp = submit_handler._handle(_event({"overallRating": True}), feedback_table, secret=SECRET)

    assert resp["statusCode"] == 400
    assert _all_items(feedback_table) == []


def test_free_text_within_cap_is_persisted(feedback_table):
    resp = submit_handler._handle(
        _event({"additionalSources": "arXiv, Hacker News"}), feedback_table, secret=SECRET
    )

    assert resp["statusCode"] == 200
    (item,) = _all_items(feedback_table)
    assert item["additionalSources"] == "arXiv, Hacker News"


def test_free_text_over_cap_is_rejected_no_partial_record(feedback_table):
    over_cap = "x" * (submit_handler.FREE_TEXT_MAX_LENGTH + 1)
    resp = submit_handler._handle(_event({"otherFeedback": over_cap}), feedback_table, secret=SECRET)

    assert resp["statusCode"] == 400
    assert _all_items(feedback_table) == []


def test_free_text_at_exactly_cap_is_accepted(feedback_table):
    at_cap = "x" * submit_handler.FREE_TEXT_MAX_LENGTH
    resp = submit_handler._handle(_event({"otherFeedback": at_cap}), feedback_table, secret=SECRET)

    assert resp["statusCode"] == 200


def test_attributed_when_not_anonymous_and_token_valid(feedback_table):
    token = feedback_token.generate(SECRET, "reader@example.com", "2026-07-03")

    resp = submit_handler._handle(
        _event({"overallRating": 5, "t": token, "anonymous": False}), feedback_table, secret=SECRET
    )

    assert resp["statusCode"] == 200
    (item,) = _all_items(feedback_table)
    assert item["identity"] == "reader@example.com"
    assert item["briefDate"] == "2026-07-03"
    assert item["anonymous"] is False


def test_anonymous_checkbox_suppresses_identity_but_may_keep_brief_date(feedback_table):
    """AC-9: checkbox checked + valid token -> no identity, no raw token persisted, but
    the brief date MAY still be stored per FR-11 (not personally identifying)."""
    token = feedback_token.generate(SECRET, "reader@example.com", "2026-07-03")

    resp = submit_handler._handle(
        _event({"overallRating": 5, "t": token, "anonymous": True}), feedback_table, secret=SECRET
    )

    assert resp["statusCode"] == 200
    (item,) = _all_items(feedback_table)
    assert "identity" not in item
    assert "t" not in item
    assert "token" not in item
    assert item["anonymous"] is True
    # Date attribution may remain even on an anonymous-checkbox record (FR-11).
    assert item["briefDate"] == "2026-07-03"


def test_walk_up_no_token_is_anonymous_with_no_identity_and_no_date(feedback_table):
    resp = submit_handler._handle(_event({"overallRating": 3}), feedback_table, secret=SECRET)

    assert resp["statusCode"] == 200
    (item,) = _all_items(feedback_table)
    assert item["anonymous"] is True
    assert "identity" not in item
    assert "briefDate" not in item


def test_tampered_token_degrades_to_anonymous_never_forges_identity(feedback_table):
    token = feedback_token.generate(SECRET, "victim@example.com", "2026-07-03")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    resp = submit_handler._handle(
        _event({"overallRating": 3, "t": tampered, "anonymous": False}), feedback_table, secret=SECRET
    )

    assert resp["statusCode"] == 200
    (item,) = _all_items(feedback_table)
    assert item["anonymous"] is True
    assert "identity" not in item
    assert "briefDate" not in item
    # Critically: never the attacker-supplied/original identity, under any field name.
    # boto3's DynamoDB resource returns numeric attributes as Decimal, which the
    # stdlib json encoder can't serialize -- default=str keeps this a pure
    # "does the identity string appear anywhere in the record" check.
    assert "victim@example.com" not in json.dumps(item, default=str)


def test_missing_signing_secret_degrades_to_anonymous_never_fails_submission(feedback_table):
    """A missing/unfetchable secret must never block a submission -- it just can't
    validate any token, so every submission becomes walk-up anonymous."""
    token = feedback_token.generate(SECRET, "reader@example.com", "2026-07-03")

    resp = submit_handler._handle(
        _event({"overallRating": 3, "t": token}), feedback_table, secret=None
    )

    assert resp["statusCode"] == 200
    (item,) = _all_items(feedback_table)
    assert item["anonymous"] is True
    assert "identity" not in item


def test_successful_submit_writes_exactly_one_record_with_expected_shape(feedback_table):
    resp = submit_handler._handle(
        _event(
            {
                "overallRating": 5,
                "contentSelection": 4,
                "additionalSources": "Ars Technica",
                "otherFeedback": "Great work!",
            }
        ),
        feedback_table,
        secret=SECRET,
    )

    assert resp["statusCode"] == 200
    items = _all_items(feedback_table)
    assert len(items) == 1
    item = items[0]
    assert "submissionId" in item and item["submissionId"]
    # boto3's DynamoDB resource returns numeric attributes as Decimal.
    assert "createdAt" in item and isinstance(item["createdAt"], Decimal)
    assert item["overallRating"] == 5
    assert item["contentSelection"] == 4
    assert item["additionalSources"] == "Ars Technica"
    assert item["otherFeedback"] == "Great work!"


def test_success_response_is_generic_thank_you_shape(feedback_table):
    resp = submit_handler._handle(_event({"overallRating": 5}), feedback_table, secret=SECRET)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body == {"ok": True}


def test_malformed_json_body_is_handled_gracefully(feedback_table):
    resp = submit_handler._handle({"body": "not json"}, feedback_table, secret=SECRET)

    # Empty payload after a JSON-parse failure is treated as an all-optional empty
    # submission (still a valid, all-fields-blank feedback record) rather than a 500 --
    # no server error leaked (PRD FR-15).
    assert resp["statusCode"] == 200
    assert resp.get("body")
    assert "Traceback" not in json.dumps(resp)


def test_identity_never_logged_for_anonymous_submission(feedback_table, caplog):
    token = feedback_token.generate(SECRET, "should-not-appear@example.com", "2026-07-03")

    with caplog.at_level("INFO"):
        submit_handler._handle(
            _event({"overallRating": 3, "t": token, "anonymous": True}), feedback_table, secret=SECRET
        )

    assert "should-not-appear@example.com" not in caplog.text


def test_no_stack_trace_leaked_on_put_item_failure(feedback_table, monkeypatch):
    class RaisingTable:
        def put_item(self, **kwargs):
            raise RuntimeError("simulated DynamoDB outage")

    resp = submit_handler._handle(_event({"overallRating": 3}), RaisingTable(), secret=SECRET)

    assert resp["statusCode"] == 500
    body = json.loads(resp["body"])
    assert body["ok"] is False
    assert "Traceback" not in resp["body"]
    assert "RuntimeError" not in resp["body"]
