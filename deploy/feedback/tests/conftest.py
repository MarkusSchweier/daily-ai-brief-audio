"""Shared pytest fixtures for deploy/feedback/'s test suite.

Mirrors deploy/subscribers/tests/conftest.py's pattern: the submit Lambda's package is
its own directory (functions/submit/) containing `handler.py` + its own copy of
`feedback_token.py` (matching how Lambda mounts function code at the root), so tests
load it via `import_handler()` under a unique module name rather than a plain import.
`feedback_token` itself is put on `sys.path` directly (there is only one consumer of
it within this tree) so `test_feedback_token.py` can `import feedback_token` normally.
"""

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

FEEDBACK_DIR = Path(__file__).resolve().parent.parent
SUBMIT_DIR = FEEDBACK_DIR / "functions" / "submit"

sys.path.insert(0, str(SUBMIT_DIR))
# Makes `from brief_feedback.stack import ...` (test_stack_iam.py) importable
# regardless of the pytest invocation's cwd, mirroring
# deploy/subscribers/tests/conftest.py's identical reasoning.
sys.path.insert(0, str(FEEDBACK_DIR))

os.environ.setdefault("FEEDBACK_TABLE_NAME", "brief-feedback-test")
os.environ.setdefault("FEEDBACK_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:740353583786:secret:test-xxxxx")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def import_submit_handler():
    """Load `functions/submit/handler.py` as a uniquely-named module."""
    module_name = "submit_handler_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    handler_path = SUBMIT_DIR / "handler.py"
    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def mocked_aws(aws_credentials):
    """One shared moto session so DynamoDB + Secrets Manager mocks are visible to the
    same handler call."""
    with mock_aws():
        yield


@pytest.fixture
def feedback_table(mocked_aws):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="brief-feedback-test",
        KeySchema=[{"AttributeName": "submissionId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "submissionId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    yield table


@pytest.fixture
def secrets_client(mocked_aws):
    client = boto3.client("secretsmanager", region_name="us-east-1")
    client.create_secret(
        Name="daily-ai-brief/feedback-token-signing-secret", SecretString="test-signing-secret"
    )
    yield client
