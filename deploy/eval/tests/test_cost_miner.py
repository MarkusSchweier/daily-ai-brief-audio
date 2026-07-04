"""Unit tests for eval_core/cost_miner.py (PRD docs/prd/eval-harness.md FR-14/AC-14).

Uses a small, representative fixture built to look like the Sessions API's
`span.model_request_end` / tool-use event shape described in this epic's task and in
the owner's original manual mining procedure -- no real API call, no network access.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_core import cost_miner  # noqa: E402


def _model_request_end(input_tokens=0, output_tokens=0, cache_creation=0, cache_read=0):
    return {
        "type": "span.model_request_end",
        "model_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
    }


def _web_search_tool_use():
    return {"type": "tool_use", "tool_name": "web_search"}


# --- TokenUsage / pricing -----------------------------------------------------------


def test_token_usage_cost_uses_the_documented_introductory_pricing():
    usage = cost_miner.TokenUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    # $2 + $10 + $2.50 + $0.20 = $14.70 per the cited introductory Sonnet 5 pricing.
    assert usage.cost_usd() == 2.00 + 10.00 + 2.50 + 0.20


def test_token_usage_addition_sums_all_four_fields():
    a = cost_miner.TokenUsage(input_tokens=10, output_tokens=20, cache_creation_input_tokens=30, cache_read_input_tokens=40)
    b = cost_miner.TokenUsage(input_tokens=1, output_tokens=2, cache_creation_input_tokens=3, cache_read_input_tokens=4)
    total = a + b
    assert total == cost_miner.TokenUsage(
        input_tokens=11, output_tokens=22, cache_creation_input_tokens=33, cache_read_input_tokens=44
    )


# --- Single-thread phase-boundary heuristic -----------------------------------------


def test_single_thread_splits_research_and_writing_at_last_web_search():
    events = [
        _model_request_end(input_tokens=100, cache_read=1000),  # research
        _web_search_tool_use(),
        _model_request_end(input_tokens=50, cache_read=500),  # still research (before last boundary)
        _web_search_tool_use(),  # LAST web_search -- this is the real boundary
        _model_request_end(input_tokens=10, output_tokens=200, cache_read=4000),  # writing
        _model_request_end(output_tokens=300, cache_read=200),  # writing
    ]

    thread_cost = cost_miner.mine_thread_cost("thread_1", events)

    phases_by_name = {p.phase: p for p in thread_cost.phase_breakdown}
    assert set(phases_by_name) == {cost_miner.PHASE_RESEARCH, cost_miner.PHASE_WRITING}

    research = phases_by_name[cost_miner.PHASE_RESEARCH]
    assert research.usage.input_tokens == 150
    assert research.usage.cache_read_input_tokens == 1500

    writing = phases_by_name[cost_miner.PHASE_WRITING]
    assert writing.usage.input_tokens == 10
    assert writing.usage.output_tokens == 500
    assert writing.usage.cache_read_input_tokens == 4200


def test_matches_the_owners_real_cost_analysis_shape_writing_costs_more_than_research():
    """Regression-shaped test mirroring the PRD's own cited real numbers: writing
    should come out costing MORE than research when cache-read tokens dominate the
    post-research phase, exactly as the owner's manual mine found (~1.2M research vs.
    ~4.2M writing cache-read tokens)."""
    events = [
        _model_request_end(cache_read=1_200_000),  # research phase (before the only web_search)
        _web_search_tool_use(),
        _model_request_end(cache_read=2_100_000),  # writing phase
        _model_request_end(cache_read=2_100_000),  # writing phase
    ]

    thread_cost = cost_miner.mine_thread_cost("thread_1", events)
    phases_by_name = {p.phase: p for p in thread_cost.phase_breakdown}

    assert phases_by_name[cost_miner.PHASE_WRITING].cost_usd > phases_by_name[cost_miner.PHASE_RESEARCH].cost_usd


def test_no_web_search_event_attributes_everything_to_unknown_phase():
    events = [_model_request_end(input_tokens=42)]

    thread_cost = cost_miner.mine_thread_cost("thread_1", events)

    assert len(thread_cost.phase_breakdown) == 1
    assert thread_cost.phase_breakdown[0].phase == cost_miner.PHASE_UNKNOWN
    assert thread_cost.phase_breakdown[0].usage.input_tokens == 42


def test_events_missing_usage_fields_default_to_zero_not_raise():
    events = [{"type": "span.model_request_end"}, _web_search_tool_use()]

    thread_cost = cost_miner.mine_thread_cost("thread_1", events)

    assert thread_cost.usage == cost_miner.TokenUsage()
    assert thread_cost.cost_usd == 0.0


def test_non_model_request_end_events_are_ignored_for_usage():
    events = [
        {"type": "span.tool_use_start"},
        _model_request_end(input_tokens=10),
        {"type": "span.some_other_event"},
    ]

    thread_cost = cost_miner.mine_thread_cost("thread_1", events)

    assert thread_cost.usage.input_tokens == 10


# --- Session-level (multi-thread forward-compat) aggregation ------------------------


def test_single_thread_session_cost_matches_the_one_threads_totals():
    events = [_model_request_end(input_tokens=100, output_tokens=50)]
    breakdown = cost_miner.mine_session_cost("sesn_abc", {"thread_1": events})

    assert breakdown.session_id == "sesn_abc"
    assert len(breakdown.threads) == 1
    assert breakdown.total_usage.input_tokens == 100
    assert breakdown.total_usage.output_tokens == 50
    assert breakdown.total_cost_usd == breakdown.threads[0].cost_usd


def test_multi_thread_session_attributes_cost_per_thread_independently():
    """Forward-compatibility: a session with more than one thread (a future
    coordinator + sub-agents split) must get each thread mined and attributed on its
    own -- not pooled into one heuristic boundary across threads."""
    thread_a_events = [
        _model_request_end(input_tokens=100),
        _web_search_tool_use(),
        _model_request_end(output_tokens=100),
    ]
    thread_b_events = [
        _model_request_end(input_tokens=5),
    ]

    breakdown = cost_miner.mine_session_cost(
        "sesn_multi", {"thread_a": thread_a_events, "thread_b": thread_b_events}
    )

    assert len(breakdown.threads) == 2
    thread_ids = {t.thread_id for t in breakdown.threads}
    assert thread_ids == {"thread_a", "thread_b"}

    total_input = sum(t.usage.input_tokens for t in breakdown.threads)
    assert total_input == 105
    assert breakdown.total_usage.input_tokens == 105

    # Thread A has its own research/writing split; thread B (no web_search at all)
    # is UNKNOWN -- each thread's own heuristic outcome, not blended together.
    thread_a = next(t for t in breakdown.threads if t.thread_id == "thread_a")
    thread_b = next(t for t in breakdown.threads if t.thread_id == "thread_b")
    assert {p.phase for p in thread_a.phase_breakdown} == {cost_miner.PHASE_RESEARCH, cost_miner.PHASE_WRITING}
    assert {p.phase for p in thread_b.phase_breakdown} == {cost_miner.PHASE_UNKNOWN}


def test_session_phase_totals_aggregate_across_threads():
    thread_a_events = [
        _model_request_end(input_tokens=100),
        _web_search_tool_use(),
        _model_request_end(output_tokens=100),
    ]
    thread_b_events = [
        _model_request_end(input_tokens=10),
        _web_search_tool_use(),
        _model_request_end(output_tokens=10),
    ]

    breakdown = cost_miner.mine_session_cost(
        "sesn_multi", {"thread_a": thread_a_events, "thread_b": thread_b_events}
    )

    phase_totals_by_name = {p.phase: p for p in breakdown.phase_totals}
    assert phase_totals_by_name[cost_miner.PHASE_RESEARCH].usage.input_tokens == 110
    assert phase_totals_by_name[cost_miner.PHASE_WRITING].usage.output_tokens == 110


# --- fetch_session_cost (the live-HTTP entry point) ---------------------------------
#
# CONFIRMED LIVE (2026-07-04): GET /v1/sessions/{id}/threads/{tid} returns thread
# METADATA, not an event list -- this fixture matches that shape (no "events" key) to
# lock in the fix (fetch_session_cost must NOT rely on that endpoint for events). The
# real event log is GET /v1/sessions/{id}/events, paginated via next_page/page.


class _FakeResponse:
    def __init__(self, json_body):
        self._json_body = json_body

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_body


class _FakeHttpxClient:
    """Matches the real, confirmed-live shapes: /threads/{tid} has no "events" key
    at all, and /events is paginated (two pages here, to exercise the page cursor)."""

    def __init__(self):
        self.get_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, path, params=None, **kwargs):
        self.get_calls.append((path, params))
        if path == "/v1/sessions/sesn_live/threads":
            return _FakeResponse({"data": [{"id": "sthr_only"}]})
        if path == "/v1/sessions/sesn_live/threads/sthr_only":
            # Real shape: thread metadata, deliberately no "events" key.
            return _FakeResponse({"id": "sthr_only", "status": "idle", "usage": {}})
        if path == "/v1/sessions/sesn_live/events":
            if not params or not params.get("page"):
                return _FakeResponse(
                    {"data": [_model_request_end(input_tokens=100)], "next_page": "page_cursor_2"}
                )
            assert params["page"] == "page_cursor_2"
            return _FakeResponse({"data": [_model_request_end(output_tokens=50)], "next_page": None})
        raise AssertionError(f"unexpected GET {path}")


def test_fetch_session_cost_uses_the_events_endpoint_not_threads_endpoint(monkeypatch):
    import httpx

    fake_client = _FakeHttpxClient()
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: fake_client)

    breakdown = cost_miner.fetch_session_cost("fake-api-key", "sesn_live")

    # The old bug: reading "events" off the /threads/{tid} response (which doesn't
    # have one) silently produced an empty list and a $0.00 total. This asserts the
    # real, paginated /events endpoint was actually used and both pages were mined.
    assert breakdown.total_usage.input_tokens == 100
    assert breakdown.total_usage.output_tokens == 50
    assert breakdown.total_cost_usd > 0

    paths_called = [p for p, _ in fake_client.get_calls]
    assert "/v1/sessions/sesn_live/events" in paths_called
    assert "/v1/sessions/sesn_live/threads/sthr_only" not in paths_called
