"""Trigger a real run against ANY candidate's `agent_id` + the ONE shared `cloud`
environment, and retrieve its produced artifacts -- agent-system-redesign epic
Phase 3 (`docs/adr/0014-agent-system-redesign-topology.md` Decision 1, PRD
FR-6/FR-7/FR-8, AC-6/AC-7/AC-8).

This generalizes `deploy/eval/functions/trigger/handler.py` +
`deploy/eval/functions/poll/handler.py`'s ALREADY-PROVEN, live-confirmed mechanics
(create a temporary, non-cron Deployment; `/run` it; poll the Sessions API for
terminal status; archive the Deployment when done) to work against ANY candidate
`agent_id` and the shared `cloud` environment, rather than one hardcoded production
agent/environment pair -- the key generalization this epic exists to prove is
possible (FR-6: "no AWS infrastructure required to trigger a candidate").

The one genuinely NEW piece (not present anywhere else in this repo, because the
eval harness reads its output from S3/AWS, which a `cloud` candidate run has none
of): retrieving produced content via the SESSIONS EVENTS API
(`GET /v1/sessions/{id}/events`), per Decision 1's live-confirmed finding that the
Files-API auto-`file_id` assumption is REFUTED -- an agent-written file does not
become a downloadable Files-API object, but a `cat <path>` `bash` tool_result
DOES echo the exact file body in the session's event stream, and a `write` tool_use
event separately echoes the content it wrote in `input.content`. So a candidate's
task prompt should explicitly ask the agent to `cat` any file it writes, and
`fetch_catted_file_contents()` below parses the event stream for those tool_result
bodies.

No Anthropic API key is ever hardcoded, logged, or committed -- read from
`$ANTHROPIC_API_KEY` at call time via `api_client.get_anthropic_api_key()`, this
repo's established convention.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import api_client

# The same beta header the rest of this repo's Managed Agents API usage already uses
# for Deployments/Agents/Sessions calls (see deploy/eval/functions/trigger/handler.py,
# deploy/eval/eval_core/cost_miner.py) -- confirmed, NOT the Skills API's distinct
# `skills-2025-10-02` header (this module never touches the Skills API).
DEPLOYMENTS_BETA_HEADER = api_client.AGENTS_BETA_HEADER

# Mirrors deploy/eval/functions/poll/handler.py's `_session_is_terminal()` /
# `_session_failed()` tolerant vocabulary exactly (that module's own comment flags
# this as "not independently re-verified against a live session at build time" --
# this module inherits the same hedge, and Phase 3's own live run is one more
# real data point confirming `idle` in practice, per the ADR's "What I verified
# live" section).
_TERMINAL_STATUSES = ("idle", "complete", "completed", "finished")
_FAILED_STATUSES = ("failed", "error", "errored")

# The event-stream-level terminal marker used ONLY by the "settle" retry below
# (see `_wait_for_settled_events()`'s docstring for why this is needed at all --
# a real race confirmed live in Phase 3, NOT assumed).
_SESSION_STATUS_EVENT_TYPES = ("session.status_idle", "session.status_complete", "session.status_completed", "session.status_finished")

DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_POLL_TIMEOUT_SECONDS = 600.0  # 10 minutes -- generous for a trivial smoke-test run;
DEFAULT_EVENTS_SETTLE_RETRIES = 6
DEFAULT_EVENTS_SETTLE_INTERVAL_SECONDS = 3.0


class CandidateRunTimeoutError(RuntimeError):
    """Raised when a triggered session does not reach a terminal status within the
    poll timeout -- a clear, actionable error rather than silently returning a
    still-pending result."""


class CandidateRunFailedError(RuntimeError):
    """Raised when a triggered session reaches a FAILED terminal status (as opposed
    to succeeding) -- distinct from a timeout, so callers/tests can tell the two
    apart."""


class CandidateRunEventsNotSettledError(RuntimeError):
    """Raised when the SESSION's status (`GET /v1/sessions/{id}`) has reached a
    terminal value but the EVENT STREAM (`GET /v1/sessions/{id}/events`) still has
    not caught up after the settle-retry budget is exhausted -- see
    `_wait_for_settled_events()`'s docstring for the real race this guards
    against."""


@dataclass
class CandidateRunResult:
    """What a completed (or failed) candidate run produced -- returned so callers
    (a manual/scripted check, or the CLI) get a clean, structured result rather than
    raw HTTP responses."""

    deployment_id: str
    session_id: str
    final_status: str
    events: list[dict[str, Any]] = field(default_factory=list)


def build_deployments_client(api_key: str | None = None) -> httpx.Client:
    """An httpx.Client configured for Deployments/Sessions API calls -- the SAME
    beta header and base URL the Agents API client uses (all three are part of the
    one `managed-agents-2026-04-01` beta surface); a separate builder purely so a
    caller can name its intent clearly at the call site, mirroring
    `api_client.build_agents_client()` / `build_skills_client()`'s pattern."""
    return httpx.Client(
        base_url=api_client.ANTHROPIC_API_BASE_URL,
        headers={
            "x-api-key": api_key or api_client.get_anthropic_api_key(),
            "anthropic-version": api_client.ANTHROPIC_VERSION,
            "anthropic-beta": DEPLOYMENTS_BETA_HEADER,
        },
        timeout=60.0,
    )


def create_temporary_deployment(
    client: httpx.Client, *, agent_id: str, environment_id: str, task_prompt: str, name: str
) -> str:
    """POST /v1/deployments -- create a new, one-off (non-cron -- no `schedule`
    field) temporary deployment targeting the given candidate `agent_id` and the
    shared `environment_id`. Confirmed shape:
    `deploy/eval/functions/trigger/handler.py:151-173`'s
    `_create_temporary_deployment()` (a top-level `name` is REQUIRED; omitting
    `schedule` produces `"schedule": null` with `"status": "active"`, no error).
    Returns the new deployment's id."""
    response = client.post(
        "/v1/deployments",
        json={
            "name": name,
            "agent": agent_id,
            "environment_id": environment_id,
            "initial_events": [{"type": "user.message", "content": [{"type": "text", "text": task_prompt}]}],
        },
    )
    response.raise_for_status()
    return response.json()["id"]


def start_session(client: httpx.Client, deployment_id: str) -> str:
    """POST /v1/deployments/{id}/run -- trigger one session against the just-created
    temporary deployment. Confirmed shape:
    `deploy/eval/functions/trigger/handler.py:176-189`'s `_start_session()` (the
    endpoint is `/run`, NOT `/sessions`; the session id is under `session_id` in the
    response, NOT `id` -- `id` there is the run's own `drun_...` resource, a
    different thing). Returns the new session id."""
    response = client.post(f"/v1/deployments/{deployment_id}/run")
    response.raise_for_status()
    return response.json()["session_id"]


def get_session_status(client: httpx.Client, session_id: str) -> str:
    """GET /v1/sessions/{id} -- the session's current lifecycle status."""
    response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return response.json().get("status", "")


def fetch_session_events(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """GET /v1/sessions/{id}/events -- the session's full, paginated event stream.

    Confirmed shape (`deploy/eval/eval_core/cost_miner.py`'s `fetch_session_cost()`,
    live-confirmed 2026-07-04): paginated via a `next_page` cursor echoed back as a
    `page` query param on the next request (`limit`/`page`, NOT offset-based). This
    is the ONLY confirmed way to retrieve a `cloud` candidate run's produced content
    (Decision 1: the Files-API auto-`file_id` assumption is REFUTED -- see this
    module's own docstring)."""
    all_events: list[dict[str, Any]] = []
    page: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 1000, **({"page": page} if page else {})}
        response = client.get(f"/v1/sessions/{session_id}/events", params=params)
        response.raise_for_status()
        body = response.json()
        all_events.extend(body.get("data", []))
        page = body.get("next_page")
        if not page:
            break
    return all_events


def _events_are_settled(events: list[dict[str, Any]]) -> bool:
    """True if `events` itself contains a terminal `session.status_*` event -- the
    event-stream-level signal that the full transcript (including any tool_use/
    tool_result pairs from the agent's final turn) has actually been recorded, not
    just that the SEPARATE session-status field has flipped terminal."""
    return any(event.get("type") in _SESSION_STATUS_EVENT_TYPES for event in events)


def _wait_for_settled_events(
    client: httpx.Client,
    session_id: str,
    *,
    retries: int = DEFAULT_EVENTS_SETTLE_RETRIES,
    interval_seconds: float = DEFAULT_EVENTS_SETTLE_INTERVAL_SECONDS,
    sleep_fn=time.sleep,
) -> list[dict[str, Any]]:
    """Fetch the session's events, retrying if the SESSION-level status
    (`GET /v1/sessions/{id}`) has already gone terminal but the EVENT STREAM
    (`GET /v1/sessions/{id}/events`) has not yet caught up.

    CONFIRMED LIVE (2026-07-06, agent-system-redesign epic Phase 3, a real observed
    race -- NOT assumed): on a real smoke-test run, `GET /v1/sessions/{id}` reported
    `status: "idle"` on the VERY FIRST poll, while `GET /v1/sessions/{id}/events` at
    that exact moment returned only 4 events -- none of the agent's actual
    tool_use/tool_result pairs (the write/cat calls) were present yet. Re-fetching
    moments later returned the full 24-event transcript, ending in a
    `session.status_idle` event. So the session-status endpoint can report terminal
    BEFORE the events endpoint has caught up -- this function closes that gap by
    retrying until the events stream ITSELF contains a terminal
    `session.status_*` event (see `_events_are_settled()`), not just trusting the
    separate status field. Raises `CandidateRunEventsNotSettledError` if the retry
    budget is exhausted without the events ever settling (a real, if rare, failure
    mode worth surfacing clearly rather than silently returning an incomplete
    transcript)."""
    events: list[dict[str, Any]] = []
    for attempt in range(retries):
        events = fetch_session_events(client, session_id)
        if _events_are_settled(events):
            return events
        if attempt < retries - 1:
            sleep_fn(interval_seconds)
    raise CandidateRunEventsNotSettledError(
        f"session {session_id}'s event stream did not settle (no terminal session.status_* "
        f"event) after {retries} attempts, {retries * interval_seconds}s total -- got "
        f"{len(events)} events; the session's own status endpoint reported terminal, but its "
        "events endpoint has not caught up"
    )


def archive_deployment(client: httpx.Client, deployment_id: str) -> None:
    """POST /v1/deployments/{id}/archive -- confirmed shape,
    `deploy/eval/functions/poll/handler.py:215-221`'s `_archive_deployment()`. Called
    once a triggered candidate run is done (success OR failure) so no temporary
    deployment is left callable indefinitely -- the same discipline the eval
    harness's own poller already applies to ITS temporary deployments."""
    response = client.post(f"/v1/deployments/{deployment_id}/archive")
    response.raise_for_status()


def run_candidate(
    deployments_client: httpx.Client,
    *,
    agent_id: str,
    environment_id: str,
    task_prompt: str,
    deployment_name: str,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    sleep_fn=time.sleep,
    now_fn=time.monotonic,
) -> CandidateRunResult:
    """Trigger one real run against `agent_id` + `environment_id`, poll until the
    session reaches a terminal status (or the poll timeout elapses), fetch the full
    event stream, and archive the temporary deployment -- in that order, always
    archiving even on a failed/timed-out run (mirroring the eval poller's own
    always-archive-on-every-terminal-branch discipline,
    `deploy/eval/functions/poll/handler.py`'s `_archive_deployment_best_effort()`).

    `sleep_fn`/`now_fn` are injected (defaulting to the real `time.sleep`/
    `time.monotonic`) purely so tests can drive the poll loop deterministically
    without a real wall-clock wait -- the same seam this repo's tests elsewhere use
    for time-dependent logic.

    Raises `CandidateRunFailedError` if the session reaches a FAILED status, or
    `CandidateRunTimeoutError` if it never reaches a terminal status within
    `poll_timeout_seconds` -- in ALL THREE post-creation failure modes (a FAILED
    status, a poll timeout, OR `start_session()` itself raising, e.g. a transient
    5xx on `POST /v1/deployments/{id}/run`), the temporary deployment is still
    archived before the exception propagates (never leaked). A failure in
    `create_temporary_deployment()` itself needs no archive call -- nothing was
    created yet, so `deployment_id` is never assigned and the `try`/`finally` below
    never runs.

    CORRECTED (reviewer + security-engineer, independently converged on the same
    bug): `start_session()` used to run BEFORE the `try:` that owns the `finally:`
    archive call, so a `start_session()` failure (distinct from a FAILED session
    status or a poll timeout, both raised INSIDE the try block) propagated with
    ZERO archive call -- a genuinely leaked, permanently-callable temporary
    deployment, contradicting this function's own docstring and the README's
    explicit "always archives... no callable temporary deployment is ever left
    behind" claim. Fixed by moving `start_session()` inside the `try` -- the
    `finally` archives unconditionally, which is correct because `deployment_id`
    is always bound before the `try`/`finally` is ever entered (see the comment
    at the `finally` clause below).

    CORRECTED AGAIN (agent-system-redesign epic, Phase 5, a SECOND real race found
    on the very first genuine, non-trivial candidate run -- `production-baseline`'s
    real research/writing task): the VERY FIRST `get_session_status()` call, made
    with ZERO delay immediately after `start_session()` returns, can report `idle`
    -- a STALE PLACEHOLDER left over from before the session has even transitioned
    to `running` -- NOT a genuine terminal state. CONFIRMED LIVE via a dedicated
    diagnostic probe (a fresh trivial session, polled every ~0.3s starting the
    instant `/run` returned): poll #1 read `idle`; polls #2-10 (all still within
    ~1 second) correctly read `running`; the session's OWN event stream later
    confirmed `session.status_running` fired ~11 seconds before the GENUINE
    `session.status_idle` terminal event. This is the OPPOSITE-direction sibling of
    `_wait_for_settled_events()`'s already-documented race (that one is "status
    says terminal before events catch up"; THIS one is "status says terminal
    (idle) before the session has even started running at all," which the
    single-status-field poll loop had no way to distinguish from a genuine,
    instant completion). On `production-baseline`'s real run, this caused the poll
    loop to immediately (and wrongly) accept the SPURIOUS first-poll `idle` as
    final, breaking out of the loop after mere milliseconds while the session
    then went on to run for real, for many real minutes -- and the subsequent
    `_wait_for_settled_events()` call correctly found no terminal event yet (there
    genuinely wasn't one), exhausted its retry budget, and raised
    `CandidateRunEventsNotSettledError` -- a confusing, MISLEADING symptom of this
    DIFFERENT underlying bug, not a failure of the events-settle mechanism itself.

    THE FIX: a status reported as terminal is no longer trusted on its own -- it
    must be CONFIRMED by the event stream itself actually containing a genuine
    terminal `session.status_*` event (the same, already-reliable signal
    `_wait_for_settled_events()` uses) before the poll loop accepts it and breaks.
    If the status looks terminal but the events stream does NOT yet contain a
    terminal marker, this is treated as "not actually done yet" -- the loop
    continues polling (respecting the same timeout/interval as any other
    not-yet-terminal iteration) rather than exiting and handing off to
    `_wait_for_settled_events()`'s SEPARATE, narrower retry budget (which exists
    for the other race -- a genuinely-just-finished session whose events haven't
    caught up yet -- not for "the session hasn't even started")."""
    deployment_id = create_temporary_deployment(
        deployments_client,
        agent_id=agent_id,
        environment_id=environment_id,
        task_prompt=task_prompt,
        name=deployment_name,
    )

    deadline = now_fn() + poll_timeout_seconds
    final_status = ""
    try:
        session_id = start_session(deployments_client, deployment_id)

        while True:
            final_status = get_session_status(deployments_client, session_id)
            if final_status.lower() in _FAILED_STATUSES:
                raise CandidateRunFailedError(
                    f"candidate run session {session_id} (deployment {deployment_id}) failed with status {final_status!r}"
                )
            if final_status.lower() in _TERMINAL_STATUSES:
                # Don't trust the status field alone (see the "CORRECTED AGAIN"
                # note above -- a bare "idle" can be a stale pre-`running`
                # placeholder). Confirm against the event stream's OWN terminal
                # marker before actually accepting this as done.
                if _events_are_settled(fetch_session_events(deployments_client, session_id)):
                    break
            if now_fn() >= deadline:
                raise CandidateRunTimeoutError(
                    f"candidate run session {session_id} (deployment {deployment_id}) did not reach a "
                    f"terminal status within {poll_timeout_seconds}s (last status: {final_status!r})"
                )
            sleep_fn(poll_interval_seconds)

        events = _wait_for_settled_events(deployments_client, session_id, sleep_fn=sleep_fn)
        return CandidateRunResult(
            deployment_id=deployment_id, session_id=session_id, final_status=final_status, events=events
        )
    finally:
        # deployment_id is ALWAYS assigned by this point (create_temporary_deployment()
        # already returned successfully, or we wouldn't have reached this try/finally
        # at all) -- so archiving unconditionally here is correct, not merely
        # best-effort-if-present.
        archive_deployment(deployments_client, deployment_id)


def fetch_catted_file_contents(events: list[dict[str, Any]]) -> dict[str, str]:
    """Parse a session's event stream for `cat <path>` bash tool_result bodies,
    returning `{path: content}` for every file successfully catted.

    Per Decision 1's live-confirmed finding: a `bash` tool_result from a
    `cat /some/path` command returns the exact file body as its output. This
    function recovers that mapping WITHOUT any AWS/Files-API involvement -- the
    confirmed substitute for the refuted Files-API auto-`file_id` assumption.

    Matching strategy: walk the events looking for a `bash` tool_use event whose
    `input.command` is (or starts with, allowing trailing whitespace/redirects) a
    plain `cat <path>` invocation, then find the LATER tool_result event carrying
    that SAME tool_use's id and read its output text. Deliberately conservative --
    only recognizes a simple, unambiguous `cat <path>` form (no pipes, no multiple
    files, no shell expansion) since that is exactly the form this repo's candidate
    task prompts are written to use (see e.g.
    `deploy/candidates/smoke-test-example/task-prompt.md`)."""
    tool_use_id_to_path: dict[str, str] = {}
    for event in events:
        event_type = event.get("type", "")
        if "tool_use" not in event_type and "tool_call" not in event_type:
            continue
        tool_name = event.get("name") or event.get("tool_name") or ""
        if tool_name != "bash":
            continue
        command = ((event.get("input") or {}).get("command") or "").strip()
        path = _parse_plain_cat_command(command)
        if path is None:
            continue
        tool_use_id = event.get("id") or event.get("tool_use_id")
        if tool_use_id:
            tool_use_id_to_path[tool_use_id] = path

    if not tool_use_id_to_path:
        return {}

    results: dict[str, str] = {}
    for event in events:
        event_type = event.get("type", "")
        if "tool_result" not in event_type:
            continue
        tool_use_id = event.get("tool_use_id") or event.get("id")
        path = tool_use_id_to_path.get(tool_use_id) if tool_use_id else None
        if path is None:
            continue
        content = _extract_tool_result_text(event)
        if content is not None:
            results[path] = content
    return results


def _parse_plain_cat_command(command: str) -> str | None:
    """Return the path argument of a plain `cat <path>`, `cat "<path>"`, or
    `cat '<path>'` command, or None if `command` is not (a whitespace-trimmed)
    exactly one of those three simple forms. Deliberately strict -- see
    `fetch_catted_file_contents()`'s docstring for why.

    CORRECTED (agent-system-redesign epic, Phase 5, a real gap found on the real
    `production-baseline` trigger -- not a synthetic edge case): the brief
    Markdown file this repo's own skill output contract names,
    `AI Brief - YYYY-MM-DD.md`, contains LITERAL SPACES in its own filename (not
    just shell quoting) -- `SKILL.md`'s own documented output path. A real agent
    run correctly double-quoted its `cat` invocation for this and every other
    path this candidate wrote (`cat "/workspace/AI Brief - 2026-07-06.md"`,
    `cat "/workspace/listening-script.txt"`, etc.) -- entirely reasonable,
    portable shell practice -- but the ORIGINAL version of this function rejected
    ANY remainder containing a bare space character outright, with no
    quote-aware exception. This silently dropped the brief file ENTIRELY (its
    filename's spaces are real content, not just quoting, so no unquoting could
    ever produce a space-free path) and mis-parsed three other quoted paths into
    dict keys that STILL carried their literal surrounding quote characters
    (`'"/workspace/listening-script.txt"'` instead of
    `'/workspace/listening-script.txt'`) -- a confusing, partial failure mode,
    not a clean rejection. Fixed by recognizing ONE additional, still-simple,
    still-unambiguous form (at the time): a remainder that is ENTIRELY wrapped in
    a single pair of double quotes with no unescaped quote or shell
    metacharacter inside -- the exact form the real agent run happened to
    produce -- while continuing to reject anything genuinely ambiguous
    (multiple quoted/unquoted arguments, pipes, redirects, unquoted spaces,
    escaped quotes) exactly as before.

    CORRECTED AGAIN (reviewer follow-up, Phase 5): the double-quote-only fix
    above reproduced the IDENTICAL silent-drop bug for the equally idiomatic
    `cat 'path with spaces'` (single-quoted) form -- nothing in
    `task-prompt.md`'s example constrains the agent to double quotes
    specifically, and single-quoting a path is just as common, portable bash
    practice. The double-quote-only version's fall-through structure meant a
    single-quoted remainder (starting with `'`, not `"`) skipped the
    quoted-path branch entirely and landed in the unquoted-form check below,
    which rejects any bare space -- silently dropping the file again, via a
    different quote character, with zero diagnostic. Fixed by mirroring the
    exact same quoted-path recognition for single quotes: a remainder entirely
    wrapped in one pair of single quotes, with no unescaped quote or shell
    metacharacter inside, resolves to its unquoted inner path -- identical
    rejection rules (unterminated quote, embedded quote, metacharacter inside)
    apply to both quote characters, kept as parallel, independently-readable
    branches rather than a single generalized-but-harder-to-verify helper."""
    if not command.startswith("cat "):
        return None
    remainder = command[len("cat ") :].strip()
    if not remainder:
        return None
    if remainder.startswith('"'):
        # ANY remainder starting with a quote must satisfy the quoted-path form
        # below, or be rejected outright -- it must NOT silently fall through to
        # the unquoted-form check (a real gap an earlier version of this branch
        # had: an unterminated leading quote with no space/metacharacter inside
        # would otherwise fall through and be returned verbatim, quote
        # character and all).
        if not (remainder.endswith('"') and len(remainder) >= 2):
            return None
        inner = remainder[1:-1]
        # Reject anything that still isn't a single, simple, unescaped-quote-free
        # path inside the quotes (an embedded `"` or shell metacharacter means
        # this isn't the plain single-quoted-path form this parser recognizes).
        if not inner or any(ch in inner for ch in ('"', "|", ">", "<", "&", ";")):
            return None
        return inner
    if remainder.startswith("'"):
        # Mirrors the double-quote branch above exactly, for the equally
        # idiomatic single-quoted form (`cat 'path with spaces'`) -- see the
        # "CORRECTED AGAIN" docstring note for why this branch exists at all.
        if not (remainder.endswith("'") and len(remainder) >= 2):
            return None
        inner = remainder[1:-1]
        if not inner or any(ch in inner for ch in ("'", "|", ">", "<", "&", ";")):
            return None
        return inner
    # Unquoted form: reject anything with shell metacharacters/pipes/redirects/
    # multiple args/unquoted spaces -- those aren't the simple form this module
    # is built to recognize.
    if any(ch in remainder for ch in ("|", ">", "<", "&", ";", " ")):
        return None
    return remainder


def _extract_tool_result_text(event: dict[str, Any]) -> str | None:
    """Read a tool_result event's textual output.

    CONFIRMED LIVE (2026-07-06, agent-system-redesign epic Phase 3, real observed
    events -- not assumed): an `agent.tool_result` event's `content` field is a LIST
    of content blocks, each `{"type": "text", "text": "..."}` -- e.g. a real `cat`
    tool_result observed on the smoke-test-example candidate:
    `{"content": [{"text": "The smoke test skill says hello from version one.",
    "type": "text"}], "tool_use_id": "sevt_...", "type": "agent.tool_result", ...}`.
    This function reads that confirmed shape as the primary path, and additionally
    tolerates a couple of other plausible field names (`output`/`result`, or a bare
    string) as a defensive fallback in case a different event family or a future
    API revision uses a different shape -- but `content` as a list of text blocks is
    the one actually observed and relied upon."""
    for field_name in ("output", "content", "result", "text"):
        value = event.get(field_name)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            texts = [block.get("text") for block in value if isinstance(block, dict) and isinstance(block.get("text"), str)]
            if texts:
                return "".join(texts)
    return None
