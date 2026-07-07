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

import httpx
import pytest

from candidate_sync import recent_briefs_token, trigger
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


def test_run_candidate_does_not_accept_a_spurious_first_poll_idle_status():
    """Regression test for a REAL race this phase's production-baseline trigger
    found live (agent-system-redesign epic, Phase 5) -- the SECOND, distinct race
    this function has now had fixed (see the "CORRECTED AGAIN" note on
    run_candidate()'s own docstring for the full story and the live diagnostic
    that confirmed it).

    CONFIRMED LIVE (2026-07-06): the VERY FIRST `GET /v1/sessions/{id}` call,
    issued with essentially zero delay right after `POST /v1/deployments/{id}/run`
    returns, can report `status: "idle"` -- a STALE PLACEHOLDER from before the
    session has even transitioned to `running` -- while the session's OWN event
    stream at that exact same instant contains no terminal marker at all (the
    session hasn't genuinely started, let alone finished). A live diagnostic probe
    (a fresh trivial session, polled repeatedly starting the instant `/run`
    returned) reproduced this exactly: poll #1 read `idle`; polls #2 onward
    correctly read `running`; the session's real event stream later confirmed
    `session.status_running` fired several seconds before the GENUINE
    `session.status_idle` event. Before this fix, the poll loop would have
    accepted that spurious FIRST "idle" as final and broken out of the loop after
    milliseconds, on a session that then went on to run for real for many
    minutes -- exactly what happened on the real `production-baseline` trigger
    (masked by a confusing, misleading `CandidateRunEventsNotSettledError`, since
    the events-settle retry correctly found no terminal marker yet -- there
    genuinely wasn't one -- but that retry's own narrower budget was never
    designed for "the session hasn't started," only for "it just finished and the
    events endpoint is catching up").

    This test scripts EXACTLY that sequence: poll #1 -> `idle` status but events
    with NO terminal marker (must NOT be accepted); the loop must keep polling
    (sleep_fn called); poll #2 -> `running`; poll #3 -> a GENUINE `idle`, this
    time confirmed by events that DO contain the terminal marker -- only THIS
    poll may be accepted.

    Confirmed, directly: this test FAILS against the pre-fix code (it would
    accept poll #1's spurious idle immediately, making zero sleep_fn calls and
    returns before ever reaching poll #2/#3's scripted responses, so
    `clock.sleep_calls` would be empty and the events-endpoint queue below would
    be left with unconsumed entries) and PASSES against the fix."""
    client = FakeHttpxClient()
    client.when("POST", "/v1/deployments", FakeResponse(200, {"id": "depl_EXAMPLE"}))
    client.when("POST", "/v1/deployments/depl_EXAMPLE/run", FakeResponse(200, {"session_id": "sesn_EXAMPLE"}))
    client.when(
        "GET",
        "/v1/sessions/sesn_EXAMPLE",
        FakeResponse(200, {"status": "idle"}),  # poll #1: the SPURIOUS placeholder
        FakeResponse(200, {"status": "running"}),  # poll #2: genuinely running now
        FakeResponse(200, {"status": "idle"}),  # poll #3: the GENUINE terminal status
    )
    client.when(
        "GET",
        "/v1/sessions/sesn_EXAMPLE/events",
        # Confirming-fetch after poll #1's spurious "idle": NO terminal marker yet
        # -- must NOT be accepted as done.
        FakeResponse(200, {"data": [REAL_SESSION_STATUS_RUNNING_EVENT], "next_page": None}),
        # Confirming-fetch after poll #3's GENUINE "idle": the terminal marker IS
        # present now -- this one may be accepted.
        FakeResponse(200, {"data": [REAL_SESSION_STATUS_RUNNING_EVENT, REAL_SESSION_STATUS_IDLE_EVENT], "next_page": None}),
        # _wait_for_settled_events()'s own subsequent fetch, once the loop has
        # broken out -- same, now-genuinely-settled transcript.
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
        poll_interval_seconds=5.0,
        sleep_fn=clock.sleep_fn,
        now_fn=clock.now_fn,
    )

    assert result.final_status == "idle"
    assert result.events == [REAL_SESSION_STATUS_RUNNING_EVENT, REAL_SESSION_STATUS_IDLE_EVENT]
    # The loop had to sleep TWICE (after poll #1's rejected spurious idle, and
    # after poll #2's genuine "running") before poll #3's genuinely-confirmed idle
    # was accepted -- proving the spurious first "idle" was NOT accepted outright.
    assert clock.sleep_calls == [5.0, 5.0]


def test_run_candidate_archives_even_when_start_session_itself_raises():
    """Regression test for the leaked-deployment bug the reviewer + security-
    engineer independently found: create_temporary_deployment() and start_session()
    used to run BEFORE the try/finally that owns the archive call, so a
    start_session() failure (e.g. a transient 5xx on POST
    /v1/deployments/{id}/run) -- DISTINCT from a FAILED session status or a poll
    timeout, both raised INSIDE the try block -- propagated with ZERO archive call.
    This is the THIRD failure mode, the one the docstring's narrower
    "FAILED status / timeout" claim did not cover. Asserts archive_deployment was
    called exactly once despite start_session raising."""
    client = FakeHttpxClient()
    client.when("POST", "/v1/deployments", FakeResponse(200, {"id": "depl_EXAMPLE"}))
    # A transient 5xx on POST /v1/deployments/{id}/run -- start_session() raises
    # httpx.HTTPStatusError via raise_for_status(), never reaching the poll loop.
    client.when("POST", "/v1/deployments/depl_EXAMPLE/run", FakeResponse(500, {"error": "simulated transient failure"}))
    client.when("POST", "/v1/deployments/depl_EXAMPLE/archive", FakeResponse(200, {}))

    with pytest.raises(httpx.HTTPStatusError):
        trigger.run_candidate(
            client,
            agent_id="agent_EXAMPLE",
            environment_id="env_EXAMPLE",
            task_prompt="do the thing",
            deployment_name="candidate-trigger-example",
        )

    # THE assertion that matters: archive_deployment was called exactly once, even
    # though start_session() itself raised before any session/poll logic ran at all.
    archive_calls = [c for c in client.calls if c.method == "POST" and c.path == "/v1/deployments/depl_EXAMPLE/archive"]
    assert len(archive_calls) == 1


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
        'cat "/path with space" /other',  # a quoted path PLUS a second arg -- still rejected
        'cat "unterminated',  # opening quote with no matching close -- rejected
        'cat "embedded " quote"',  # an embedded quote inside the quotes -- rejected
        'cat ""',  # empty quoted path -- rejected
        "cat '/path with space' /other",  # single-quoted path PLUS a second arg -- still rejected
        "cat 'unterminated",  # opening single quote with no matching close -- rejected
        "cat 'embedded ' quote'",  # an embedded single quote inside the quotes -- rejected
        "cat ''",  # empty single-quoted path -- rejected
    ],
)
def test_parse_plain_cat_command_rejects_non_simple_forms(command):
    assert trigger._parse_plain_cat_command(command) is None


def test_parse_plain_cat_command_accepts_the_simple_form():
    assert trigger._parse_plain_cat_command("cat /workspace/smoke-test-output.txt") == "/workspace/smoke-test-output.txt"


def test_parse_plain_cat_command_accepts_a_double_quoted_path_with_spaces():
    """Regression test for a REAL gap this phase's production-baseline trigger
    found (agent-system-redesign epic, Phase 5) -- see
    `_parse_plain_cat_command()`'s own "CORRECTED" docstring note for the full
    story. The brief file this repo's own skill output contract names,
    `AI Brief - YYYY-MM-DD.md`, has LITERAL SPACES in its filename -- a real
    agent run correctly double-quoted its `cat` invocation for it
    (`cat "/workspace/AI Brief - 2026-07-06.md"`), which the pre-fix parser
    rejected outright (any bare space in the remainder was disqualifying, with
    no quote-aware exception), silently dropping the brief entirely -- the most
    important single artifact a candidate produces. Confirmed, directly: this
    exact real command (verbatim, from the real captured transcript) FAILS
    against the pre-fix code (returns None) and PASSES against the fix."""
    assert (
        trigger._parse_plain_cat_command('cat "/workspace/AI Brief - 2026-07-06.md"')
        == "/workspace/AI Brief - 2026-07-06.md"
    )


def test_parse_plain_cat_command_accepts_a_double_quoted_path_without_spaces():
    """A quoted path with NO spaces inside (e.g. a real agent quoting every path
    uniformly, as `production-baseline`'s run did for `listening-script.txt`/
    `candidates.json`/`source-usage.json` too) must ALSO resolve to the bare,
    unquoted path -- not a dict key still carrying literal quote characters."""
    assert trigger._parse_plain_cat_command('cat "/workspace/listening-script.txt"') == "/workspace/listening-script.txt"


def test_parse_plain_cat_command_accepts_a_single_quoted_path_with_spaces():
    """Regression test for a REAL gap the reviewer found in the double-quote-only
    fix above (agent-system-redesign epic, Phase 5 follow-up) -- see
    `_parse_plain_cat_command()`'s own "CORRECTED AGAIN" docstring note. Nothing
    constrains a real agent to double-quoting its `cat` invocations --
    `cat 'path with spaces'` (single-quoted) is equally idiomatic bash, and the
    double-quote-only version of this parser reproduced the IDENTICAL
    silent-drop bug for it (a single-quoted remainder fell through to the
    unquoted-form check, which rejects any bare space). Confirmed, directly:
    this command FAILS against the double-quote-only fix (returns None) and
    PASSES against this follow-up fix."""
    assert (
        trigger._parse_plain_cat_command("cat '/workspace/AI Brief - 2026-07-06.md'")
        == "/workspace/AI Brief - 2026-07-06.md"
    )


def test_parse_plain_cat_command_accepts_a_single_quoted_path_without_spaces():
    """A single-quoted path with NO spaces inside must ALSO resolve to the bare,
    unquoted path -- not a dict key still carrying literal quote characters --
    mirroring the equivalent double-quote test above."""
    assert trigger._parse_plain_cat_command("cat '/workspace/listening-script.txt'") == "/workspace/listening-script.txt"


def test_extract_tool_result_text_reads_the_confirmed_content_list_shape():
    """The CONFIRMED real shape: content is a list of {"type": "text", "text": ...}
    blocks."""
    assert trigger._extract_tool_result_text(REAL_BASH_CAT_TOOL_RESULT_EVENT) == "The smoke test skill says hello from version one."


def test_extract_tool_result_text_tolerates_a_bare_string_fallback():
    assert trigger._extract_tool_result_text({"output": "plain string body"}) == "plain string body"


def test_extract_tool_result_text_returns_none_when_nothing_recognized():
    assert trigger._extract_tool_result_text({"type": "agent.tool_result", "is_error": False}) is None


# ---------------------------------------------------------------------------
# substitute_recent_briefs_placeholders() -- ADR-0014 Decision 2d's correction
# (status ACCEPTED): mints a fresh signed recent-briefs read token per triggered
# run and substitutes it (plus the delivery base URL) into a candidate's task
# prompt, IF that prompt references the two placeholders at all.
# ---------------------------------------------------------------------------


def test_no_placeholders_present_returns_prompt_unchanged():
    """A candidate whose task prompt predates this mechanism (e.g.
    smoke-test-example) must be entirely unaffected -- no substitution
    attempted, no env var read, no token minted."""
    prompt = "Just research and write today's brief. Nothing about recent briefs here."
    result = trigger.substitute_recent_briefs_placeholders(
        prompt, signing_key="unused-key", delivery_base_url="unused-url"
    )
    assert result == prompt


def test_no_placeholders_present_does_not_require_any_config_at_all():
    """Confirms the "no env var read at all" half of the above: passing NEITHER
    signing_key NOR delivery_base_url (so the function would fall back to
    os.environ) must still succeed and return the prompt unchanged, since
    neither placeholder is present -- proving the config-missing error path is
    never reached when it isn't needed."""
    prompt = "No placeholders anywhere in this prompt."
    result = trigger.substitute_recent_briefs_placeholders(prompt)
    assert result == prompt


def test_both_placeholders_present_are_both_substituted_with_signing_key_and_url_given():
    prompt = (
        'curl -s -H "Authorization: Bearer __RECENT_BRIEFS_TOKEN__" '
        '"__DELIVERY_BASE_URL__/recent-briefs?count=3"'
    )
    result = trigger.substitute_recent_briefs_placeholders(
        prompt,
        signing_key="a-real-signing-key",
        delivery_base_url="https://example-delivery.test",
        now=1_000_000,
    )
    assert "__RECENT_BRIEFS_TOKEN__" not in result
    assert "__DELIVERY_BASE_URL__" not in result
    assert "https://example-delivery.test/recent-briefs?count=3" in result


def test_substituted_token_is_a_genuine_valid_signed_token():
    """Not just "the placeholder is gone" -- the substituted value must be a
    REAL token that verifies under the same signing key with the recent-briefs
    scheme (candidate_sync.recent_briefs_token.verify())."""
    prompt = 'Bearer __RECENT_BRIEFS_TOKEN__ against __DELIVERY_BASE_URL__'
    result = trigger.substitute_recent_briefs_placeholders(
        prompt, signing_key="a-real-signing-key", delivery_base_url="https://example.test", now=1_000_000
    )
    token = result.split("Bearer ", 1)[1].split(" against ", 1)[0]
    assert recent_briefs_token.verify("a-real-signing-key", token, now=1_000_000) is True


def test_substituted_token_respects_the_given_ttl():
    prompt = "__RECENT_BRIEFS_TOKEN__"
    result = trigger.substitute_recent_briefs_placeholders(
        prompt, signing_key="key", delivery_base_url="https://example.test", ttl_seconds=300, now=1_000_000
    )
    # Valid just before the 300s TTL elapses...
    assert recent_briefs_token.verify("key", result, now=1_000_000 + 299) is True
    # ...but not after.
    assert recent_briefs_token.verify("key", result, now=1_000_000 + 301) is False


def test_only_token_placeholder_present_substitutes_only_that_one():
    prompt = "token=__RECENT_BRIEFS_TOKEN__, no url placeholder here"
    result = trigger.substitute_recent_briefs_placeholders(
        prompt, signing_key="key", delivery_base_url="unused-since-no-url-placeholder", now=1_000_000
    )
    assert "__RECENT_BRIEFS_TOKEN__" not in result
    assert "token=" in result


def test_only_url_placeholder_present_substitutes_only_that_one_and_no_token_minted(monkeypatch):
    """If only __DELIVERY_BASE_URL__ is present (no token placeholder at all),
    the signing key is never even consulted for minting -- confirms the two
    placeholders are substituted independently, not as an all-or-nothing pair."""
    prompt = "url=__DELIVERY_BASE_URL__, no token placeholder here"
    result = trigger.substitute_recent_briefs_placeholders(
        prompt, signing_key=None, delivery_base_url="https://example.test"
    )
    assert result == "url=https://example.test, no token placeholder here"


def test_token_placeholder_present_but_signing_key_env_var_unset_raises_clear_error(monkeypatch):
    """A candidate's prompt referencing __RECENT_BRIEFS_TOKEN__ with NO signing
    key configured anywhere (neither an explicit param nor the env var) must
    raise a CLEAR, actionable error naming the env var to set -- never silently
    trigger a run with a literal, useless placeholder string."""
    monkeypatch.delenv(trigger.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, raising=False)
    prompt = "Bearer __RECENT_BRIEFS_TOKEN__"

    with pytest.raises(trigger.RecentBriefsPlaceholderConfigError) as exc_info:
        trigger.substitute_recent_briefs_placeholders(prompt, delivery_base_url="https://example.test")

    assert trigger.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR in str(exc_info.value)


def test_url_placeholder_present_but_env_var_unset_raises_clear_error(monkeypatch):
    monkeypatch.delenv(trigger.DELIVERY_BASE_URL_ENV_VAR, raising=False)
    prompt = "__DELIVERY_BASE_URL__"

    with pytest.raises(trigger.RecentBriefsPlaceholderConfigError) as exc_info:
        trigger.substitute_recent_briefs_placeholders(prompt, signing_key="a-key")

    assert trigger.DELIVERY_BASE_URL_ENV_VAR in str(exc_info.value)


def test_both_placeholders_present_but_both_env_vars_unset_raises_on_the_token_check_first(monkeypatch):
    """When both are missing, the function must still raise (not silently
    succeed on one check and skip the other) -- pins that the token check is
    evaluated (and raises) before ever reaching the url check."""
    monkeypatch.delenv(trigger.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(trigger.DELIVERY_BASE_URL_ENV_VAR, raising=False)
    prompt = "__RECENT_BRIEFS_TOKEN__ and __DELIVERY_BASE_URL__"

    with pytest.raises(trigger.RecentBriefsPlaceholderConfigError):
        trigger.substitute_recent_briefs_placeholders(prompt)


def test_reads_signing_key_and_url_from_environment_when_not_explicitly_given(monkeypatch):
    """The default (no explicit signing_key/delivery_base_url arguments) path
    must read $RECENT_BRIEFS_SIGNING_KEY / $DELIVERY_BASE_URL from the process
    environment -- proving the CLI's real call site (which passes neither
    explicitly) actually works end-to-end, not just the test-only explicit-
    argument path every other test above uses."""
    monkeypatch.setenv(trigger.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, "env-sourced-key")
    monkeypatch.setenv(trigger.DELIVERY_BASE_URL_ENV_VAR, "https://from-env.test")

    prompt = 'Bearer __RECENT_BRIEFS_TOKEN__ at __DELIVERY_BASE_URL__'
    result = trigger.substitute_recent_briefs_placeholders(prompt, now=1_000_000)

    assert "https://from-env.test" in result
    token = result.split("Bearer ", 1)[1].split(" at ", 1)[0]
    assert recent_briefs_token.verify("env-sourced-key", token, now=1_000_000) is True


def test_substitution_never_writes_the_real_token_to_any_file():
    """Sanity/documentation check: substitute_recent_briefs_placeholders() is a
    pure in-memory string transform -- it must not touch the filesystem at all
    (confirms the "no literal token ever lands in a committed file" property is
    upheld by construction: this function never opens a file for writing)."""
    import inspect

    source = inspect.getsource(trigger.substitute_recent_briefs_placeholders)
    assert "open(" not in source
    assert ".write(" not in source


def test_parse_plain_cat_command_accepts_backslash_escaped_spaces():
    """Regression test for the THIRD real quoting-idiom gap (2026-07-07, the
    cost-optimization epic's first real haiku-swap eval run) -- see
    `_parse_plain_cat_command()`'s "CORRECTED A THIRD TIME" docstring note. A
    real Haiku 4.5 agent catted the brief with BACKSLASH-ESCAPED spaces
    (verbatim from the captured run transcript below); the pre-fix
    form-enumeration parser rejected it (bare-space check fired on the raw
    remainder), silently dropping the brief for the third distinct shell idiom
    in a row. The shlex-based parser accepts every plain single-path POSIX
    spelling uniformly."""
    assert (
        trigger._parse_plain_cat_command("cat /workspace/AI\\ Brief\\ -\\ 2026-07-07.md")
        == "/workspace/AI Brief - 2026-07-07.md"
    )


def test_parse_plain_cat_command_accepts_mixed_quoting_forms():
    # shlex handles partial quoting too (e.g. quoting only the spaced segment).
    assert (
        trigger._parse_plain_cat_command('cat /workspace/"AI Brief - 2026-07-07".md')
        == "/workspace/AI Brief - 2026-07-07.md"
    )


def test_parse_plain_cat_command_rejects_substitution_forms():
    # $-expansion and backticks are conservatively rejected even where shlex
    # would tokenize them -- a `$var` key can never match a real artifact.
    assert trigger._parse_plain_cat_command("cat $BRIEF_PATH") is None
    assert trigger._parse_plain_cat_command("cat `which brief`") is None
    assert trigger._parse_plain_cat_command('cat "/workspace/$FILE.md"') is None
