"""Contract tests for `GET /recent-briefs` (ADR-0014 Decision 2d):
`functions/deliver/handler.py`'s `_handle_recent_briefs()` / `_parse_recent_briefs_count()`.

Covers the endpoint contract precisely as specified: `count` defaulting to
`brief_history.DEFAULT_RECENT_BRIEFS_COUNT` (3, matching production's own
default), clamping an oversized/malformed `count` rather than erroring,
most-recent-first ordering, an empty list still returning 200 (never 404), and
the `contractVersion` field. Uses the `briefs_bucket` fixture (moto S3) from
conftest.py, same pattern `test_brief_history.py` already establishes.

Auth is NOT exercised here (see test_recent_briefs_auth.py /
test_recent_briefs_auth_separation.py for that) -- these tests call
`_handle_recent_briefs()` directly, past the bearer-auth gate, mirroring how
`test_trigger_and_poll_handler.py` calls `_handle_trigger()` / `_handle_poll()`
directly to test contract behavior independent of auth."""

from __future__ import annotations

import json

import handler as handler_module
import brief_history


def _write_prior_brief(briefs_bucket, date: str, markdown: str) -> None:
    briefs_bucket.put_object(
        Bucket=brief_history.BUCKET,
        Key=f"briefs/{date}/brief.md",
        Body=markdown.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )


def _get_event(query_params: dict | None = None) -> dict:
    return {
        "requestContext": {"http": {"method": "GET"}, "routeKey": "GET /recent-briefs"},
        "headers": {},
        "rawPath": "/recent-briefs",
        "queryStringParameters": query_params,
    }


# ---------------------------------------------------------------------------
# _parse_recent_briefs_count -- default / clamp / malformed-input behavior.
# ---------------------------------------------------------------------------


def test_parse_count_defaults_when_missing():
    assert handler_module._parse_recent_briefs_count(None) == brief_history.DEFAULT_RECENT_BRIEFS_COUNT


def test_parse_count_defaults_when_missing_matches_production_default():
    """Pin the actual numeric value too, not just equality with the constant --
    catches an accidental drift in DEFAULT_RECENT_BRIEFS_COUNT itself going
    unnoticed."""
    assert handler_module._parse_recent_briefs_count(None) == 3


def test_parse_count_accepts_a_valid_in_range_value():
    assert handler_module._parse_recent_briefs_count("2") == 2


def test_parse_count_clamps_an_oversized_value_to_the_max():
    assert handler_module._parse_recent_briefs_count("999") == handler_module.MAX_RECENT_BRIEFS_COUNT


def test_parse_count_clamps_a_value_exactly_at_the_max_unchanged():
    assert handler_module._parse_recent_briefs_count(str(handler_module.MAX_RECENT_BRIEFS_COUNT)) == (
        handler_module.MAX_RECENT_BRIEFS_COUNT
    )


def test_parse_count_defaults_on_non_integer_string():
    assert handler_module._parse_recent_briefs_count("not-a-number") == brief_history.DEFAULT_RECENT_BRIEFS_COUNT


def test_parse_count_defaults_on_negative_value():
    assert handler_module._parse_recent_briefs_count("-5") == brief_history.DEFAULT_RECENT_BRIEFS_COUNT


def test_parse_count_defaults_on_zero():
    assert handler_module._parse_recent_briefs_count("0") == brief_history.DEFAULT_RECENT_BRIEFS_COUNT


def test_parse_count_never_raises_on_malformed_input():
    """A malformed count must degrade gracefully (clamp/default), never crash the
    whole request with a 500 -- CLAUDE.md's fail-safe philosophy, applied to this
    read endpoint."""
    for bad_value in ["", "abc", "3.5", "1e10", "-1", "None", "[]"]:
        # Must not raise for any of these.
        handler_module._parse_recent_briefs_count(bad_value)


# ---------------------------------------------------------------------------
# _handle_recent_briefs -- the full contract: 200 always, empty-list-still-200,
# most-recent-first, contractVersion present.
# ---------------------------------------------------------------------------


def test_empty_store_returns_200_with_empty_briefs_list_not_404(briefs_bucket):
    """The single most important graceful-degradation behavior (ADR-0014
    Decision 2d): a cold-start store (or the very first-ever run) is the NORMAL
    case, never an error -- mirrors read_recent_prior_briefs()'s own contract."""
    result = handler_module._handle_recent_briefs(_get_event(), briefs_bucket)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["briefs"] == []


def test_contract_version_field_is_present_and_correct(briefs_bucket):
    result = handler_module._handle_recent_briefs(_get_event(), briefs_bucket)

    body = json.loads(result["body"])
    assert body["contractVersion"] == handler_module.RECENT_BRIEFS_CONTRACT_VERSION
    assert body["contractVersion"] == 1


def test_default_count_returns_up_to_three_most_recent_first(briefs_bucket, monkeypatch):
    monkeypatch.setattr(handler_module, "_today_local_date", lambda: "2026-07-10")
    _write_prior_brief(briefs_bucket, "2026-07-06", "# Brief for July 6")
    _write_prior_brief(briefs_bucket, "2026-07-07", "# Brief for July 7")
    _write_prior_brief(briefs_bucket, "2026-07-08", "# Brief for July 8")
    _write_prior_brief(briefs_bucket, "2026-07-09", "# Brief for July 9")

    result = handler_module._handle_recent_briefs(_get_event(), briefs_bucket)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    dates = [b["date"] for b in body["briefs"]]
    # Only the 3 most recent (default count), most-recent-first -- July 6 (the
    # 4th-most-recent) is correctly excluded.
    assert dates == ["2026-07-09", "2026-07-08", "2026-07-07"]


def test_explicit_count_returns_fewer_than_default_when_requested(briefs_bucket, monkeypatch):
    monkeypatch.setattr(handler_module, "_today_local_date", lambda: "2026-07-10")
    _write_prior_brief(briefs_bucket, "2026-07-08", "# Brief for July 8")
    _write_prior_brief(briefs_bucket, "2026-07-09", "# Brief for July 9")

    result = handler_module._handle_recent_briefs(_get_event({"count": "1"}), briefs_bucket)

    body = json.loads(result["body"])
    assert len(body["briefs"]) == 1
    assert body["briefs"][0]["date"] == "2026-07-09"


def test_fewer_priors_than_count_returns_whatever_exists_still_200(briefs_bucket, monkeypatch):
    """A young store (fewer priors than requested) degrades to "however many
    exist", never an error -- same as read_recent_prior_briefs()'s own contract."""
    monkeypatch.setattr(handler_module, "_today_local_date", lambda: "2026-07-10")
    _write_prior_brief(briefs_bucket, "2026-07-09", "# Only one prior brief")

    result = handler_module._handle_recent_briefs(_get_event({"count": "5"}), briefs_bucket)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert len(body["briefs"]) == 1
    assert body["briefs"][0]["date"] == "2026-07-09"


def test_each_brief_entry_has_date_and_markdown_fields(briefs_bucket, monkeypatch):
    monkeypatch.setattr(handler_module, "_today_local_date", lambda: "2026-07-10")
    _write_prior_brief(briefs_bucket, "2026-07-09", "# The Actual Brief Content\n\nBody text.")

    result = handler_module._handle_recent_briefs(_get_event(), briefs_bucket)

    body = json.loads(result["body"])
    (entry,) = body["briefs"]
    assert entry["date"] == "2026-07-09"
    assert entry["markdown"] == "# The Actual Brief Content\n\nBody text."


def test_oversized_count_query_param_clamps_rather_than_erroring(briefs_bucket, monkeypatch):
    monkeypatch.setattr(handler_module, "_today_local_date", lambda: "2026-07-20")
    for day in range(10, 20):
        _write_prior_brief(briefs_bucket, f"2026-07-{day:02d}", f"# Brief for {day}")

    result = handler_module._handle_recent_briefs(_get_event({"count": "999"}), briefs_bucket)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert len(body["briefs"]) == handler_module.MAX_RECENT_BRIEFS_COUNT


def test_missing_query_string_parameters_key_entirely_defaults_gracefully(briefs_bucket):
    """API Gateway HTTP API omits `queryStringParameters` entirely (rather than
    an empty dict) when the request has no query string at all -- must not
    raise, must default to DEFAULT_RECENT_BRIEFS_COUNT."""
    event = {
        "requestContext": {"http": {"method": "GET"}, "routeKey": "GET /recent-briefs"},
        "headers": {},
        "rawPath": "/recent-briefs",
    }

    result = handler_module._handle_recent_briefs(event, briefs_bucket)

    assert result["statusCode"] == 200


# ---------------------------------------------------------------------------
# Routing: handler()/`_is_recent_briefs_request()` correctly distinguishes
# GET /recent-briefs from GET /deliver/{deliveryId} (both share the GET method,
# ADR-0014 Decision 2d's disambiguation-by-path requirement).
# ---------------------------------------------------------------------------


def test_is_recent_briefs_request_true_for_matching_raw_path():
    event = {"rawPath": "/recent-briefs", "requestContext": {"http": {"method": "GET"}}}
    assert handler_module._is_recent_briefs_request(event) is True


def test_is_recent_briefs_request_false_for_deliver_poll_path():
    event = {"rawPath": "/deliver/abc123", "requestContext": {"http": {"method": "GET"}}}
    assert handler_module._is_recent_briefs_request(event) is False


def test_is_recent_briefs_request_true_via_route_key_fallback():
    """Some API Gateway payload variants may carry routeKey without rawPath (or
    vice versa) -- checking both is defense-in-depth, mirroring the same
    belt-and-suspenders reasoning `_is_worker_invocation()` already applies."""
    event = {"requestContext": {"routeKey": "GET /recent-briefs", "http": {"method": "GET"}}}
    assert handler_module._is_recent_briefs_request(event) is True


def test_is_recent_briefs_request_false_for_worker_invocation_shaped_event():
    event = {"_delivery_worker": True, "deliveryId": "x", "body": {}}
    assert handler_module._is_recent_briefs_request(event) is False
