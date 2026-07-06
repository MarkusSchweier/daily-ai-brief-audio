"""Shared pytest fixtures for deploy/delivery/'s test suite.

Mirrors deploy/eval/tests/conftest.py's pattern: put this app's own packages on
sys.path so tests can import `from brief_delivery.stack import ...` regardless of
the pytest invocation's cwd, and provide the standard moto AWS-credential fixtures
this repo's other test suites already use (deploy/managed-agent/tests/conftest.py,
deploy/eval/tests/conftest.py).
"""

import os
import sys
from pathlib import Path

import pytest
from moto import mock_aws

DELIVERY_DIR = Path(__file__).resolve().parent.parent
FUNCTIONS_DIR = DELIVERY_DIR / "functions"
DELIVER_DIR = FUNCTIONS_DIR / "deliver"

sys.path.insert(0, str(DELIVERY_DIR))
sys.path.insert(0, str(DELIVER_DIR))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


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
def deliveries_table(mocked_aws):
    import boto3

    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="brief-deliveries-test",
        KeySchema=[{"AttributeName": "deliveryId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "deliveryId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    yield table


@pytest.fixture
def briefs_bucket(mocked_aws):
    import boto3

    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="cowork-polly-tts-740353583786")
    yield client
