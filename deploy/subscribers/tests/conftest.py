"""Shared pytest fixtures for the subscriber Lambda tests.

Each Lambda's deployed package is its own directory containing a top-level `handler.py`
(matching how Lambda mounts function code at the root and the layer at `/opt/python`), so
all three functions have a module literally named `handler`. To let a single pytest
session import all three without one shadowing another on `sys.path`, each test module
loads its target `handler.py` under a unique module name via `import_handler()` below
instead of a plain `import`/`from ... import`.
"""

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

SUBSCRIBERS_DIR = Path(__file__).resolve().parent.parent
LAYER_PYTHON_DIR = SUBSCRIBERS_DIR / "layers" / "common" / "python"
FUNCTIONS_DIR = SUBSCRIBERS_DIR / "functions"

sys.path.insert(0, str(LAYER_PYTHON_DIR))
# Makes `from brief_subscribers.stack import ...` (test_stack_iam.py) importable
# regardless of the pytest invocation's cwd -- `python3 app.py` gets this for free
# (interpreter auto-adds its own script directory), but pytest only auto-adds
# directories containing conftest.py/test files, not this package's parent.
sys.path.insert(0, str(SUBSCRIBERS_DIR))

os.environ.setdefault("SUBSCRIBERS_TABLE_NAME", "brief-subscribers-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def import_handler(function_name: str):
    """Load `functions/<function_name>/handler.py` as a uniquely-named module.

    Avoids `sys.path` collisions between the three same-named `handler.py` files
    (subscribe/confirm/unsubscribe), mirroring how each is an isolated Lambda package.
    """
    module_name = f"{function_name}_handler_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    handler_path = FUNCTIONS_DIR / function_name / "handler.py"
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
    """One shared moto session so DynamoDB + SES mocks are visible to the same handler call."""
    with mock_aws():
        yield


@pytest.fixture
def subscribers_table(mocked_aws):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="brief-subscribers-test",
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "email", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "status-index",
                "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    yield table


@pytest.fixture
def ses_client(mocked_aws):
    client = boto3.client("ses", region_name="us-east-1")
    client.verify_domain_identity(Domain="mschweier.com")
    yield client


@pytest.fixture
def briefs_bucket(mocked_aws):
    """A mocked `cowork-polly-tts-740353583786` bucket for latest_brief.py /
    welcome-send handler tests -- mirrors deploy/managed-agent/tests/conftest.py's
    fixture of the same name (same bucket, same mocked-AWS pattern), duplicated here
    rather than imported across the two separate CDK apps' test suites."""
    import latest_brief

    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=latest_brief.BUCKET)
    yield client
