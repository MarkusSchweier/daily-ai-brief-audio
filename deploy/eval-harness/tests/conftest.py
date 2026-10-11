"""Shared pytest fixtures for `deploy/eval-harness/`'s test suite.

Mirrors `deploy/eval/tests/conftest.py` / `deploy/candidates/tests/conftest.py`'s
pattern: put this app's own packages on `sys.path` so tests can `from eval_core...`,
`from harness...`, and `from candidate_sync...` import regardless of the pytest
invocation's cwd -- this harness reuses `deploy/candidates/candidate_sync` for
candidate loading + trigger/retrieve (ADR-0016 D1: "reusing candidate_sync, don't
duplicate"), so its directory goes on `sys.path` too.

`make_fake_client()` is ported verbatim from `deploy/eval/tests/conftest.py` (the
judges' fake-Anthropic-client test double) -- this harness's ported judge tests
(`test_judges.py`) import it unchanged. Unlike the original `deploy/eval/` suite,
this harness's tests need NO AWS/moto fixtures at all -- the harness makes zero AWS
calls in its core loop (ADR-0016 D1), so there is nothing here to mock.

AMENDED (review-fix pass, 2026-07-07): `FakeMessagesResource`/`FakeMessage` now
model a `.usage` object too (matching the real Anthropic SDK's flat
`{input_tokens, output_tokens, cache_creation_input_tokens,
cache_read_input_tokens}` shape) -- `eval_core/judges/base.py`'s `run_judge()` was
changed to capture `response.usage` into every `JudgeResult` (a reviewer-confirmed
gap vs ADR-0016: judge cost was previously discarded entirely). Every existing
ported judge test keeps working unchanged (`make_fake_client()` still takes plain
response-text strings and gets a default, non-zero `FakeUsage()` for free); tests
that need to assert on a SPECIFIC usage value pass `(text, FakeUsage(...))` tuples
instead of bare strings.

AMENDED (judge methodology v2, 2026-07-07): two more additions, both additive to
the shapes above --

1. `FakeUsage` gains an optional `server_tool_use` attribute (a `FakeServerToolUse`
   with `web_search_requests`/`web_fetch_requests` counts) so tests can exercise
   `base._extract_search_count()`'s capture path. Defaults to `None` (no server
   tools used), matching a response that never called web_search/web_fetch.
2. `FakeMessagesResource.create()` now also accepts a queued `FakeMixedMessage`
   item -- a response whose `.content` is a CALLER-SUPPLIED list of blocks
   (`FakeContentBlock`), for tests that need to prove `run_judge()` finds the
   LAST text block among interleaved server_tool_use/tool_result/text blocks
   (mixed-content responses from server-side tools), rather than joining every
   text block or grabbing the first one.

AMENDED (judge model -> Claude Opus 5.5, 2026-10-11): `FakeMessage`/
`FakeMixedMessage` carry optional `stop_reason` + `stop_details` (a
`FakeStopDetails`, mirroring the SDK's `RefusalStopDetails`), both defaulting to
None so every pre-existing fake behaves exactly as before. `run_judge()` now
branches on `stop_reason` BEFORE reading content -- refusal/truncation raise typed
errors, `pause_turn` is resumed -- and these fields are how a test drives those
paths without a live call.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HARNESS_DIR = Path(__file__).resolve().parent.parent
CANDIDATES_DIR = HARNESS_DIR.parent / "candidates"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(HARNESS_DIR))
sys.path.insert(0, str(CANDIDATES_DIR))


class _NoNetworkHttpClient:
    """Stands in for the real `httpx.Client` `harness.dedup_priors` would build
    when no client is injected -- every request method raises, so no test can
    reach the LIVE delivery endpoint (see the autouse fixture below)."""

    def get(self, *args, **kwargs):
        raise RuntimeError("the test suite makes no live HTTP calls (harness.dedup_priors)")

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_live_recent_briefs_fetch(monkeypatch):
    """HERMETICITY GUARD (2026-10-11). `run.py`'s captured-brief guard
    (`_verify_candidate_brief_was_captured()`) reads `GET /recent-briefs` for
    EVERY repetition, and `harness.local_config` resolves a REAL signing key +
    the committed real delivery base URL from the operator's machine -- so a test
    that drives `run.main()` would otherwise make a genuine, live API call to
    production's delivery boundary (observed: the suite's runtime jumped from 2s
    to 19s).

    Patching `dedup_priors._build_http_client` (the one factory the module uses
    when NO client is injected) keeps every such path offline: the fetch raises,
    `RecentBriefsUnavailableError` is reported, and the guard degrades with its
    documented `BRIEF_PRIOR_CHECK_SKIPPED` warning. Tests that want a feed inject
    their own fake `client=` (unaffected by this fixture) or monkeypatch
    `fetch_recent_briefs_unfiltered` directly."""
    from harness import dedup_priors

    monkeypatch.setattr(dedup_priors, "_build_http_client", lambda: _NoNetworkHttpClient())


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeServerToolUse:
    """Mirrors the real Anthropic SDK's `usage.server_tool_use` shape (confirmed
    live 2026-07-07 against the web-search-tool docs page's own example response:
    `"usage": {..., "server_tool_use": {"web_search_requests": 1}}`)."""

    def __init__(self, web_search_requests: int = 0, web_fetch_requests: int = 0):
        self.web_search_requests = web_search_requests
        self.web_fetch_requests = web_fetch_requests


class FakeUsage:
    """Mirrors the real Anthropic SDK's flat `Usage` shape (NOT the nested
    `cache_creation: {...}` shape the Sessions/Threads API uses -- see
    `eval_core.judges.base._extract_usage()`'s docstring for why both are
    tolerated). Defaults to small, non-zero, deterministic values so a test that
    doesn't care about usage still gets a realistic, priceable `JudgeResult.usage`
    for free.

    `server_tool_use` (judge methodology v2) defaults to `None` -- a response
    that never used web_search/web_fetch -- matching a real response's own
    absence of the field when no server-side tool was invoked."""

    def __init__(
        self,
        input_tokens: int = 100,
        output_tokens: int = 50,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        server_tool_use: "FakeServerToolUse | None" = None,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.server_tool_use = server_tool_use


class FakeStopDetails:
    """Mirrors the real SDK's `RefusalStopDetails` (`{type, category, explanation}`,
    both of the latter optional/nullable) -- for tests of `run_judge()`'s
    `stop_reason: "refusal"` branch (2026-10-11, Opus 5.5)."""

    def __init__(self, category: str | None = None, explanation: str | None = None):
        self.type = "refusal"
        self.category = category
        self.explanation = explanation


class FakeMessage:
    """`stop_reason`/`stop_details` (2026-10-11) default to None -- a fake that
    doesn't care about stop reasons behaves exactly as it did before `run_judge()`
    learned to branch on them (`getattr(response, "stop_reason", None)`), so every
    pre-existing test keeps working unchanged."""

    def __init__(
        self,
        text: str,
        usage: "FakeUsage | None" = None,
        stop_reason: str | None = None,
        stop_details: "FakeStopDetails | None" = None,
    ):
        self.content = [FakeTextBlock(text)]
        self.usage = usage if usage is not None else FakeUsage()
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class FakeContentBlock:
    """A single content block for a `FakeMixedMessage` -- mirrors whichever real
    SDK block type `type` names (`text`, `thinking`, `server_tool_use`,
    `web_search_tool_result`, `web_fetch_tool_result`, ...). Only `text` blocks
    carry a `.text` attribute (matching the real SDK: non-text blocks expose
    other fields `run_judge()` never reads -- a real `thinking` block carries
    `.thinking`, empty under the default `display: "omitted"`, never `.text`)."""

    def __init__(self, block_type: str, text: str | None = None):
        self.type = block_type
        if text is not None:
            self.text = text


class FakeMixedMessage:
    """A canned response whose `.content` is a CALLER-SUPPLIED list of blocks
    (judge methodology v2) -- for tests that need interleaved
    server_tool_use/tool_result/thinking/text blocks, proving `run_judge()`'s
    `_extract_final_text_block()` finds the LAST text block specifically, not the
    first, and not a join of every text block. Queue one via
    `make_fake_client(FakeMixedMessage([...]))` exactly like any other response.

    `stop_reason`/`stop_details` (2026-10-11) default to None, exactly as on
    `FakeMessage` -- set them to exercise `run_judge()`'s refusal/truncation/
    pause_turn branches."""

    def __init__(
        self,
        content_blocks: list,
        usage: "FakeUsage | None" = None,
        stop_reason: str | None = None,
        stop_details: "FakeStopDetails | None" = None,
    ):
        self.content = content_blocks
        self.usage = usage if usage is not None else FakeUsage()
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class FakeMessagesResource:
    """Records every call's kwargs and returns queued canned responses in order,
    mirroring the Anthropic SDK's `client.messages.create(...)` shape closely enough
    for the judges under test (which only read `.content[].type`/`.text`/`.usage`).

    Each queued response is a plain text string (gets a default `FakeUsage()`), a
    `(text, FakeUsage(...))` tuple (for a test that asserts on a specific usage
    value), or a `FakeMixedMessage(...)` (for a test that needs a caller-supplied,
    interleaved content-block list -- judge methodology v2)."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessagesResource ran out of queued responses")
        item = self._responses.pop(0)
        if isinstance(item, FakeMixedMessage):
            return item
        if isinstance(item, tuple):
            text, usage = item
        else:
            text, usage = item, None
        return FakeMessage(text, usage=usage)


class FakeAnthropicClient:
    def __init__(self, responses: list):
        self.messages = FakeMessagesResource(responses)


def make_fake_client(*responses) -> FakeAnthropicClient:
    """Build a fake Anthropic client that returns `responses` in order, one per
    `messages.create(...)` call -- for judge tests that don't want a real API key.
    Each element is either a plain response-text string or a `(text,
    FakeUsage(...))` tuple."""
    return FakeAnthropicClient(list(responses))


def git_init_and_commit(directory: Path) -> None:
    """Initialize `directory` as a real, minimal git repo with one commit of its
    current contents -- LOCAL-ONLY git commands, no network. Used by tests that
    exercise `harness.run_store.candidate_declaration_is_dirty()` (review-fix:
    reviewer Medium, "dirty-working-tree guard") against a synthetic candidate
    fixture: the REAL `deploy/candidates/` tree always lives inside this repo's
    own git history, so a synthetic fixture built in `tmp_path` needs its own tiny
    real repo to make that check meaningful (git status against a path that isn't
    inside any repo at all is a setup error, not a clean/dirty verdict -- see that
    function's own docstring)."""
    subprocess.run(["git", "init", "-q"], cwd=str(directory), check=True)
    env_args = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]
    subprocess.run(["git", *env_args, "add", "-A"], cwd=str(directory), check=True)
    subprocess.run(["git", *env_args, "commit", "-q", "-m", "initial"], cwd=str(directory), check=True)
