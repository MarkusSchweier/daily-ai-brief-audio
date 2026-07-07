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
"""

from __future__ import annotations

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


class FakeMessage:
    def __init__(self, text: str):
        self.content = [FakeTextBlock(text)]


class FakeMessagesResource:
    """Records every call's kwargs and returns queued canned responses in order,
    mirroring the Anthropic SDK's `client.messages.create(...)` shape closely enough
    for the judges under test (which only read `.content[].type`/`.text`)."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessagesResource ran out of queued responses")
        return FakeMessage(self._responses.pop(0))


class FakeAnthropicClient:
    def __init__(self, responses: list[str]):
        self.messages = FakeMessagesResource(responses)


def make_fake_client(*responses: str) -> FakeAnthropicClient:
    """Build a fake Anthropic client that returns `responses` in order, one per
    `messages.create(...)` call -- for judge tests that don't want a real API key."""
    return FakeAnthropicClient(list(responses))
