"""Shared pytest fixtures for deploy/eval/'s test suite.

Mirrors deploy/feedback/tests/conftest.py / deploy/subscribers/tests/conftest.py's
pattern: put this app's own packages on sys.path so tests can import
`from eval_core... import ...` and `from brief_eval.stack import ...` regardless of
the pytest invocation's cwd.
"""

import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

EVAL_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(EVAL_DIR))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


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


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def mocked_aws(aws_credentials):
    with mock_aws():
        yield


@pytest.fixture
def dynamodb_resource(mocked_aws):
    return boto3.resource("dynamodb", region_name="us-east-1")
