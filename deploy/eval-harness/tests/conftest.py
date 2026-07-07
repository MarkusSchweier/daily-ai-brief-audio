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
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent.parent
CANDIDATES_DIR = HARNESS_DIR.parent / "candidates"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(HARNESS_DIR))
sys.path.insert(0, str(CANDIDATES_DIR))


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeUsage:
    """Mirrors the real Anthropic SDK's flat `Usage` shape (NOT the nested
    `cache_creation: {...}` shape the Sessions/Threads API uses -- see
    `eval_core.judges.base._extract_usage()`'s docstring for why both are
    tolerated). Defaults to small, non-zero, deterministic values so a test that
    doesn't care about usage still gets a realistic, priceable `JudgeResult.usage`
    for free."""

    def __init__(
        self,
        input_tokens: int = 100,
        output_tokens: int = 50,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class FakeMessage:
    def __init__(self, text: str, usage: "FakeUsage | None" = None):
        self.content = [FakeTextBlock(text)]
        self.usage = usage if usage is not None else FakeUsage()


class FakeMessagesResource:
    """Records every call's kwargs and returns queued canned responses in order,
    mirroring the Anthropic SDK's `client.messages.create(...)` shape closely enough
    for the judges under test (which only read `.content[].type`/`.text`/`.usage`).

    Each queued response is either a plain text string (gets a default
    `FakeUsage()`) or a `(text, FakeUsage(...))` tuple (for a test that asserts on
    a specific usage value)."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessagesResource ran out of queued responses")
        item = self._responses.pop(0)
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
