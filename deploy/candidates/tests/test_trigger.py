"""Tests for candidate_sync.trigger -- the reusable candidate trigger-and-retrieve
mechanism (agent-system-redesign epic Phase 3, PRD FR-6/FR-7/FR-8, AC-6/AC-7/AC-8).

Every Anthropic API interaction is mocked via `FakeHttpxClient` (this repo's own
established fake-client pattern from Phase 2, see `fake_httpx_client.py`'s
docstring) -- NO real network calls, NO real Deployment/Session is ever created by
this test suite. The event-stream shapes used below are taken DIRECTLY from real,
observed event data captured during this phase's own live validation runs against
the real `smoke-test-example` candidate (see README.md's "Phase 3 live validation"
section) -- not guessed at.

Covers:
  - create_temporary_deployment() / start_session() / archive_deployment() request
    shapes (mirroring deploy/eval/functions/trigger/handler.py's already-proven
    request bodies, generalized to an arbitrary agent_id/environment_id)
  - run_candidate()'s poll loop: reaching a terminal status, a FAILED status, and a
    poll timeout -- and that archive_deployment() is ALWAYS called (success,
    failure, AND timeout), using injected sleep_fn/now_fn so no real time passes
  - the events-settle race fix (_wait_for_settled_events()): the REAL race observed
    live in Phase 3, where GET /v1/sessions/{id} reports a terminal status before
    GET /v1/sessions/{id}/events has caught up -- confirming the retry-until-settled
    logic actually closes that gap, and that it fails loudly (not silently) if the
    retry budget is exhausted
  - fetch_catted_file_contents()'s event-stream parsing, using the ACTUAL event
    shapes observed on the real smoke-test-example run (agent.tool_use /
    agent.tool_result, tool_use_id correlation, content-as-list-of-text-blocks)
"""

from __future__ import annotations

import pytest

from candidate_sync import trigger
from fake_httpx_client import FakeHttpxClient, FakeResponse

# --- Real, observed event fixtures ---------------------------------------------------
# Captured directly from a real session run against the smoke-test-example candidate
# during this phase's live validation (see README.md) -- not synthesized guesses.

REAL_BASH_CAT_TOOL_USE_EVENT = {
    "evaluated_permission": "allow",
    "id": "sevt_01C9gKqht933w4mbeGXjWDgy",
    "input": {"command": "cat /workspace/smoke-test-output.txt"},
    "name": "bash",
    "processed_at": "2026-07-06T11:17:50.967906Z",
    "type": "agent.tool_use",
}

REAL_BASH_CAT_TOOL_RESULT_EVENT = {
    "content": [{"text": "The smoke test skill says hello from version one.", "type": "text"}],
    "id": "sevt_01McFXTStiE6p5QfSJbDdtBB",
    "is_error": False,
    "processed_at": "2026-07-06T11:17:52.247944Z",
    "tool_use_id": "sevt_01C9gKqht933w4mbeGXjWDgy",
    "type": "agent.tool_result",
}

REAL_WRITE_TOOL_USE_EVENT = {
    "evaluated_permission": "allow",
    "id": "sevt_012GEd6Pg2DDYbFwDM1hQDiJ",
    "input": {"content": "The smoke test skill says hello from version one.", "file_path": "/workspace/smoke-test-output.txt"},
    "name": "write",
    "processed_at": "2026-07-06T11:17:50.967905Z",
    "type": "agent.tool_use",
}

REAL_WRITE_TOOL_RESULT_EVENT = {
    "content": [{"text": "File created: /workspace/smoke-test-output.txt", "type": "text"}],
    "id": "sevt_019n9cfBXxTgFZ8cqTfGkovS",
    "is_error": False,
    "processed_at": "2026-07-06T11:17:52.247943Z",
    "tool_use_id": "sevt_012GEd6Pg2DDYbFwDM1hQDiJ",
    "type": "agent.tool_result",
}

REAL_READ_SKILL_TOOL_USE_EVENT = {
    "evaluated_permission": "allow",
    "id": "sevt_011crWBDwPzXqKcsNGdUSTeE",
    "input": {"file_path": "/workspace/skills/smoke-test-skill/SKILL.md"},
    "name": "read",
    "processed_at": "2026-07-06T11:17:45.033111Z",
    "type": "agent.tool_use",
}

REAL_READ_SKILL_TOOL_RESULT_EVENT = {
    "content": [{"text": "1\t---\n2\tname: smoke-test-skill\n...", "type": "text"}],
    "id": "sevt_01CjoWvPR5PWYzH6B3eJzpDw",
    "is_error": False,
    "processed_at": "2026-07-06T11:17:49.161383Z",
    "tool_use_id": "sevt_011crWBDwPzXqKcsNGdUSTeE",
    "type": "agent.tool_result",
}

REAL_SESSION_STATUS_IDLE_EVENT = {
    "id": "sevt_01Lm52QH44CjBwShm3LvCWX2",
    "processed_at": "2026-07-06T11:17:53.710305Z",
    "stop_reason": {"type": "end_turn"},
    "type": "session.status_idle",
}

REAL_SESSION_STATUS_RUNNING_EVENT = {
    "id": "sevt_EXAMPLE_RUNNING",
    "processed_at": "2026-07-06T11:17:43.547530Z",
    "type": "session.status_running",
}


# --- create_temporary_deployment() / start_session() / archive_deployment() --------


def test_create_temporary_deployment_sends_confirmed_shape():
    """Mirrors deploy/eval/functions/trigger/handler.py:151-173's already-proven
    request body shape, generalized to an arbitrary agent_id/environment_id (not one
    hardcoded production pair) -- the exact generalization FR-6 requires."""
    client = FakeHttpxClient()
    client.when("POST", "/v1/deployments", FakeResponse(200, {"id": "depl_EXAMPLE"}))

    deployment_id = trigger.create_temporary_deployment(
        client,
        agent_id="agent_EXAMPLE",
        environment_id="env_EXAMPLE_SHARED_CLOUD",
        task_prompt="do the thing",
        name="candidate-trigger-example",
    )

    assert deployment_id == "depl_EXAMPLE"
    sent_body = client.calls[0].kwargs["json"]
    assert sent_body["name"] == "candidate-trigger-example"
    assert sent_body["agent"] == "agent_EXAMPLE"
    assert sent_body["environment_id"] == "env_EXAMPLE_SHARED_CLOUD"
    assert sent_body["initial_events"] == [{"type": "user.message", "content": [{"type": "text", "text": "do the thing"}]}]
    # No `schedule` field at all -- a non-cron, one-off deployment (confirmed shape).
    assert "schedule" not in sent_body


def test_start_session_reads_session_id_not_id():
    """CONFIRMED LIVE (deploy/eval/functions/trigger/handler.py's own comment): the
    session id is under `session_id`, NOT `id` -- `id` on the /run response is the
    run's own drun_... resource, a different thing."""
    client = FakeHttpxClient()
    client.when("POST", "/v1/deployments/depl_EXAMPLE/run", FakeResponse(200, {"id": "drun_EXAMPLE", "session_id": "sesn_EXAMPLE"}))

    session_id = trigger.start_session(client, "depl_EXAMPLE")

    assert session_id == "sesn_EXAMPLE"
    assert client.call_signature() == [("POST", "/v1/deployments/depl_EXAMPLE/run")]


def test_get_session_status_reads_status_field():
    client = FakeHttpxClient()
    client.when("GET", "/v1/sessions/sesn_EXAMPLE", FakeResponse(200, {"status": "idle"}))

    assert trigger.get_session_status(client, "sesn_EXAMPLE") == "idle"


def test_archive_deployment_calls_confirmed_endpoint():
    client = FakeHttpxClient()
    client.when("POST", "/v1/deployments/depl_EXAMPLE/archive", FakeResponse(200, {"archived_at": "2026-07-06T00:00:00Z"}))

    trigger.archive_deployment(client, "depl_EXAMPLE")

    assert client.call_signature() == [("POST", "/v1/deployments/depl_EXAMPLE/archive")]


# --- fetch_session_events() pagination -----------------------------------------------


def test_fetch_session_events_paginates_via_next_page_cursor():
    """Mirrors eval_core/cost_miner.py's confirmed pagination shape: limit/page
    query params, a next_page cursor in the response echoed back as the next page
    param, NOT offset-based."""
    client = FakeHttpxClient()
    client.when(
        "GET",
        "/v1/sessions/sesn_EXAMPLE/events",
        FakeResponse(200, {"data": [{"type": "session.status_running"}], "next_page": "cursor-abc"}),
        FakeResponse(200, {"data": [{"type": "session.status_idle"}], "next_page": None}),
    )

    events = trigger.fetch_session_events(client, "sesn_EXAMPLE")

    assert events == [{"type": "session.status_running"}, {"type": "session.status_idle"}]
    assert client.calls[0].kwargs["params"] == {"limit": 1000}
    assert client.calls[1].kwargs["params"] == {"limit": 1000, "page": "cursor-abc"}


# --- run_candidate(): the poll loop ---------------------------------------------------


class _FakeClock:
    """A tiny, deterministic stand-in for time.monotonic()/time.sleep() so
    run_candidate()'s poll loop can be tested with NO real wall-clock wait -- each
    sleep_fn() call just advances the fake clock by the requested amount."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleep_calls: list[float] = []

    def now_fn(self) -> float:
        return self.now

    def sleep_fn(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


def test_run_candidate_success_path_returns_result_and_archives():
    client = FakeHttpxClient()
    client.when("POST", "/v1/deployments", FakeResponse(200, {"id": "depl_EXAMPLE"}))
    client.when("POST", "/v1/deployments/depl_EXAMPLE/run", FakeResponse(200, {"id": "drun_EXAMPLE", "session_id": "sesn_EXAMPLE"}))
    client.when("GET", "/v1/sessions/sesn_EXAMPLE", FakeResponse(200, {"status": "idle"}))
    client.when(
        "GET",
        "/v1/sessions/sesn_EXAMPLE/events",
        FakeResponse(200, {"data": [REAL_SESSION_STATUS_RUNNING_EVENT, REAL_SESSION_STATUS_IDLE_EVENT], "next_page": None}),
    )
    client.when("POST", "/v1/deployments/depl_EXAMPLE/archive", FakeResponse(200, {}))
    clock = _FakeClock()

    result = trigger.run_candidate(
        client,
        agent_id="agent_EXAMPLE",
        environment_id="env_EXAMPLE",
        task_prompt="do the thing",
        deployment_name="candidate-trigger-example",
        sleep_fn=clock.sleep_fn,
        now_fn=clock.now_fn,
    )

    assert result.deployment_id == "depl_EXAMPLE"
    assert result.session_id == "sesn_EXAMPLE"
    assert result.final_status == "idle"
    assert result.events == [REAL_SESSION_STATUS_RUNNING_EVENT, REAL_SESSION_STATUS_IDLE_EVENT]
    # archive_deployment() WAS called on the success path.
    assert ("POST", "/v1/deployments/depl_EXAMPLE/archive") in client.call_signature()


def test_run_candidate_polls_until_terminal_using_injected_sleep():
    """The session reports 'running' twice before going 'idle' -- confirms the poll
    loop actually loops (not just succeeding on the first check) and that
    sleep_fn() is called between polls, using the injected fake clock (NO real
    wall-clock wait)."""
    client = FakeHttpxClient()
    client.when("POST", "/v1/deployments", FakeResponse(200, {"id": "depl_EXAMPLE"}))
    client.when("POST", "/v1/deployments/depl_EXAMPLE/run", FakeResponse(200, {"session_id": "sesn_EXAMPLE"}))
    client.when(
        "GET",
        "/v1/sessions/sesn_EXAMPLE",
        FakeResponse(200, {"status": "running"}),
        FakeResponse(200, {"status": "running"}),
        FakeResponse(200, {"status": "idle"}),
    )
    client.when(
        "GET",
        "/v1/sessions/sesn_EXAMPLE/events",
        FakeResponse(200, {"data": [REAL_SESSION_STATUS_IDLE_EVENT], "next_page": None}),
    )
    client.when("POST", "/v1/deployments/depl_EXAMPLE/archive", FakeResponse(200, {}))
    clock = _FakeClock()

    result = trigger.run_candidate(
        client,
        agent_id="agent_EXAMPLE",
        environment_id="env_EXAMPLE",
        task_prompt="do the thing",
        deployment_name="candidate-trigger-example",
        poll_interval_seconds=5.0,
        sleep_fn=clock.sleep_fn,
        now_fn=clock.now_fn,
    )

    assert result.final_status == "idle"
    # Two polls came back "running" -> exactly two sleeps of 5.0s each between polls.
    assert clock.sleep_calls == [5.0, 5.0]


def test_run_candidate_raises_on_failed_status_and_still_archives():
    client = FakeHttpxClient()
    client.when("POST", "/v1/deployments", FakeResponse(200, {"id": "depl_EXAMPLE"}))
    client.when("POST", "/v1/deployments/depl_EXAMPLE/run", FakeResponse(200, {"session_id": "sesn_EXAMPLE"}))
    client.when("GET", "/v1/sessions/sesn_EXAMPLE", FakeResponse(200, {"status": "failed"}))
    client.when("POST", "/v1/deployments/depl_EXAMPLE/archive", FakeResponse(200, {}))

    with pytest.raises(trigger.CandidateRunFailedError, match="failed"):
        trigger.run_candidate(
            client,
            agent_id="agent_EXAMPLE",
            environment_id="env_EXAMPLE",
            task_prompt="do the thing",
            deployment_name="candidate-trigger-example",
        )

    # archive_deployment() is STILL called even though the run failed -- never leaked.
    assert ("POST", "/v1/deployments/depl_EXAMPLE/archive") in client.call_signature()


def test_run_candidate_raises_on_poll_timeout_and_still_archives():
    """The session stays 'running' forever (never reaches a terminal status) -- the
    poll loop must give up once the deadline (driven by the injected fake clock)
    passes, rather than looping forever, and must STILL archive the deployment."""
    client = FakeHttpxClient()
    client.when("POST", "/v1/deployments", FakeResponse(200, {"id": "depl_EXAMPLE"}))
    client.when("POST", "/v1/deployments/depl_EXAMPLE/run", FakeResponse(200, {"session_id": "sesn_EXAMPLE"}))
    client.when("GET", "/v1/sessions/sesn_EXAMPLE", lambda **_kw: FakeResponse(200, {"status": "running"}))
    client.when("POST", "/v1/deployments/depl_EXAMPLE/archive", FakeResponse(200, {}))
    clock = _FakeClock()

    with pytest.raises(trigger.CandidateRunTimeoutError, match="did not reach a terminal status"):
        trigger.run_candidate(
            client,
            agent_id="agent_EXAMPLE",
            environment_id="env_EXAMPLE",
            task_prompt="do the thing",
            deployment_name="candidate-trigger-example",
            poll_interval_seconds=10.0,
            poll_timeout_seconds=25.0,
            sleep_fn=clock.sleep_fn,
            now_fn=clock.now_fn,
        )

    assert ("POST", "/v1/deployments/depl_EXAMPLE/archive") in client.call_signature()


# --- The events-settle race fix: a REAL race observed live in Phase 3 --------------


def test_events_settle_retries_until_terminal_session_status_event_appears():
    """CONFIRMED LIVE (2026-07-06): GET /v1/sessions/{id} reported status "idle" on
    the VERY FIRST poll, while GET /v1/sessions/{id}/events at that exact moment
    returned only a partial transcript with NO terminal session.status_* event yet.
    This test proves _wait_for_settled_events() retries past that partial response
    and returns only once a genuinely terminal events response arrives."""
    client = FakeHttpxClient()
    client.when(
        "GET",
        "/v1/sessions/sesn_EXAMPLE/events",
        # 1st fetch: status already went terminal, but events haven't caught up --
        # only 2 early events, no session.status_* terminal event present.
        FakeResponse(200, {"data": [REAL_SESSION_STATUS_RUNNING_EVENT, REAL_READ_SKILL_TOOL_USE_EVENT], "next_page": None}),
        # 2nd fetch: NOW the full transcript, ending in the terminal event.
        FakeResponse(
            200,
            {
                "data": [
                    REAL_SESSION_STATUS_RUNNING_EVENT,
                    REAL_READ_SKILL_TOOL_USE_EVENT,
                    REAL_BASH_CAT_TOOL_USE_EVENT,
                    REAL_BASH_CAT_TOOL_RESULT_EVENT,
                    REAL_SESSION_STATUS_IDLE_EVENT,
                ],
                "next_page": None,
            },
        ),
    )
    clock = _FakeClock()

    events = trigger._wait_for_settled_events(client, "sesn_EXAMPLE", retries=6, interval_seconds=3.0, sleep_fn=clock.sleep_fn)

    assert events[-1] == REAL_SESSION_STATUS_IDLE_EVENT
    assert len(events) == 5
    # Exactly one retry sleep happened between the two fetches.
    assert clock.sleep_calls == [3.0]


def test_events_settle_raises_a_clear_error_if_never_settles():
    """If the events stream NEVER settles within the retry budget, this must raise a
    clear, actionable error rather than silently returning an incomplete transcript
    (which fetch_catted_file_contents() could then misread as 'nothing was
    written')."""
    client = FakeHttpxClient()
    client.when(
        "GET",
        "/v1/sessions/sesn_EXAMPLE/events",
        lambda **_kw: FakeResponse(200, {"data": [REAL_SESSION_STATUS_RUNNING_EVENT], "next_page": None}),
    )
    clock = _FakeClock()

    with pytest.raises(trigger.CandidateRunEventsNotSettledError, match="did not settle"):
        trigger._wait_for_settled_events(client, "sesn_EXAMPLE", retries=3, interval_seconds=1.0, sleep_fn=clock.sleep_fn)

    assert len(client.calls) == 3
    assert clock.sleep_calls == [1.0, 1.0]  # sleeps BETWEEN attempts, none after the last


# --- fetch_catted_file_contents(): real, observed event shapes ----------------------


def test_fetch_catted_file_contents_recovers_a_single_real_cat_result():
    """Uses the ACTUAL event pair observed on the real smoke-test-example run (see
    module docstring) -- the write tool_use/tool_result pair is present too (as it
    was in the real transcript) but must NOT be picked up, since it isn't a `cat`
    bash command."""
    events = [
        REAL_WRITE_TOOL_USE_EVENT,
        REAL_BASH_CAT_TOOL_USE_EVENT,
        REAL_WRITE_TOOL_RESULT_EVENT,
        REAL_BASH_CAT_TOOL_RESULT_EVENT,
    ]

    result = trigger.fetch_catted_file_contents(events)

    assert result == {"/workspace/smoke-test-output.txt": "The smoke test skill says hello from version one."}


def test_fetch_catted_file_contents_against_the_full_real_captured_transcript():
    """The FULL real event list captured from Phase 3's actual first live run (17
    events, before the session finished settling) -- confirms the parser correctly
    recovers the one genuine cat'd file from a realistic, busier transcript that
    also contains an unrelated `read` tool call (reading the skill file) and a
    `write` tool call, neither of which should be mistaken for a cat result."""
    events = [
        {"type": "session.status_running", "id": "sevt_1"},
        {"type": "session.thread_status_running", "id": "sevt_2", "agent_name": "smoke-test-example", "session_thread_id": "sthr_1"},
        {"type": "user.message", "id": "sevt_3", "content": [{"type": "text", "text": "Run the smoke test now..."}]},
        {"type": "span.model_request_start", "id": "sevt_4"},
        {"type": "agent.thinking", "id": "sevt_5"},
        {"type": "agent.message", "id": "sevt_6", "content": [{"type": "text", "text": "I'll run the smoke test."}]},
        REAL_READ_SKILL_TOOL_USE_EVENT,
        {"type": "span.model_request_end", "id": "sevt_7", "model_request_start_id": "sevt_4", "is_error": False, "model_usage": {}},
        REAL_READ_SKILL_TOOL_RESULT_EVENT,
        {"type": "span.model_request_start", "id": "sevt_8"},
        {"type": "agent.message", "id": "sevt_9", "content": [{"type": "text", "text": "Writing the output now."}]},
        REAL_WRITE_TOOL_USE_EVENT,
        REAL_BASH_CAT_TOOL_USE_EVENT,
        {"type": "span.model_request_end", "id": "sevt_10", "model_request_start_id": "sevt_8", "is_error": False, "model_usage": {}},
        REAL_WRITE_TOOL_RESULT_EVENT,
        REAL_BASH_CAT_TOOL_RESULT_EVENT,
        {"type": "span.model_request_start", "id": "sevt_11"},
    ]

    result = trigger.fetch_catted_file_contents(events)

    assert result == {"/workspace/smoke-test-output.txt": "The smoke test skill says hello from version one."}


def test_fetch_catted_file_contents_returns_empty_dict_when_no_cat_happened():
    events = [REAL_WRITE_TOOL_USE_EVENT, REAL_WRITE_TOOL_RESULT_EVENT]

    assert trigger.fetch_catted_file_contents(events) == {}


def test_fetch_catted_file_contents_recovers_multiple_distinct_cat_results():
    second_tool_use = {**REAL_BASH_CAT_TOOL_USE_EVENT, "id": "sevt_SECOND_USE", "input": {"command": "cat /workspace/other-file.txt"}}
    second_tool_result = {
        "content": [{"text": "second file content", "type": "text"}],
        "id": "sevt_SECOND_RESULT",
        "is_error": False,
        "tool_use_id": "sevt_SECOND_USE",
        "type": "agent.tool_result",
    }
    events = [REAL_BASH_CAT_TOOL_USE_EVENT, REAL_BASH_CAT_TOOL_RESULT_EVENT, second_tool_use, second_tool_result]

    result = trigger.fetch_catted_file_contents(events)

    assert result == {
        "/workspace/smoke-test-output.txt": "The smoke test skill says hello from version one.",
        "/workspace/other-file.txt": "second file content",
    }


@pytest.mark.parametrize(
    "command",
    [
        "cat /path | grep foo",  # pipe -- rejected, not the simple form
        "cat /path > /other",  # redirect -- rejected
        "cat /path1 /path2",  # multiple files -- rejected (contains a space)
        "cat",  # no path at all
        "tail -f /path",  # not a cat command
    ],
)
def test_parse_plain_cat_command_rejects_non_simple_forms(command):
    assert trigger._parse_plain_cat_command(command) is None


def test_parse_plain_cat_command_accepts_the_simple_form():
    assert trigger._parse_plain_cat_command("cat /workspace/smoke-test-output.txt") == "/workspace/smoke-test-output.txt"


def test_extract_tool_result_text_reads_the_confirmed_content_list_shape():
    """The CONFIRMED real shape: content is a list of {"type": "text", "text": ...}
    blocks."""
    assert trigger._extract_tool_result_text(REAL_BASH_CAT_TOOL_RESULT_EVENT) == "The smoke test skill says hello from version one."


def test_extract_tool_result_text_tolerates_a_bare_string_fallback():
    assert trigger._extract_tool_result_text({"output": "plain string body"}) == "plain string body"


def test_extract_tool_result_text_returns_none_when_nothing_recognized():
    assert trigger._extract_tool_result_text({"type": "agent.tool_result", "is_error": False}) is None
