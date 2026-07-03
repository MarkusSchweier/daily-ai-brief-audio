"""Pytest fixtures for deploy/managed-agent/pipeline/'s test suite.

Mirrors the repo-root tests/conftest.py pattern (tests/conftest.py,
deploy/subscribers/tests/conftest.py): `audio_email.py` is a plain top-level script
(mirroring the local SKILL.md inline copy, always run as `python3 audio_email.py`,
never imported by production code), so we load it once under a mocked AWS session
(moto) with temp input files and env vars, then exercise its exported `send_all()` /
helper functions directly with fake boto3 clients per test. `brief_history.py`, by
contrast, is a real importable module (no module-level AWS calls at import time), so
its tests import it directly.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from moto import mock_aws

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
AUDIO_EMAIL_PATH = PIPELINE_DIR / "audio_email.py"

sys.path.insert(0, str(PIPELINE_DIR))


@pytest.fixture(scope="session")
def audio_email_module(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("audio_email_inputs")
    script_path = tmp_dir / "listening-script.txt"
    html_path = tmp_dir / "brief.html"
    mp3_path = tmp_dir / "brief.mp3"
    script_path.write_text("This is the listening script.", encoding="utf-8")
    html_path.write_text("<html><body><h1>Brief</h1></body></html>", encoding="utf-8")

    env_overrides = {
        "LISTENING_SCRIPT_PATH": str(script_path),
        "BRIEF_HTML_PATH": str(html_path),
        "MP3_OUT_PATH": str(mp3_path),
        "EMAIL_SUBJECT": "Test AI Brief",
        # Deliberately NOT setting AWS_SHARED_CREDENTIALS_FILE anywhere in this fixture —
        # the whole point of the port is that no credential-file loading exists any
        # more (docs/adr/0004). moto's mock_aws() only needs *some* boto3 credentials
        # to be resolvable, which these dummy env vars (the standard moto pattern)
        # provide, exactly as the repo-root fixture already does for the live
        # deploy/audio_email.py.
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    old_env = {k: os.environ.get(k) for k in env_overrides}
    old_shared_cred_file = os.environ.pop("AWS_SHARED_CREDENTIALS_FILE", None)
    os.environ.update(env_overrides)

    module_name = "managed_agent_audio_email_under_test"
    try:
        with mock_aws():
            spec = importlib.util.spec_from_file_location(module_name, AUDIO_EMAIL_PATH)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if old_shared_cred_file is not None:
            os.environ["AWS_SHARED_CREDENTIALS_FILE"] = old_shared_cred_file

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
    with mock_aws():
        yield


@pytest.fixture
def briefs_bucket(mocked_aws):
    import boto3

    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="cowork-polly-tts-740353583786")
    yield client
