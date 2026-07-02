"""Pytest fixtures for testing deploy/audio_email.py's fan-out loop logic.

`audio_email.py` is intentionally a plain top-level script (mirroring the inline copy in
the scheduled task's SKILL.md, which is always run as `python3 audio_email.py`, never
imported). To unit-test its fan-out/failure-isolation logic without hitting real AWS, we
import it once under a mocked AWS session (moto) with temp input files and env vars, then
exercise the exported `send_all()` / helper functions directly with fake boto3 clients per
test. moto does not implement Polly's async synthesis task API, so the module-level Polly
step naturally falls back to its existing `audio_ok = False` text-only fail-safe during
import — which is itself useful coverage of that fail-safe path (AC-7).
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from moto import mock_aws

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIO_EMAIL_PATH = REPO_ROOT / "deploy" / "audio_email.py"


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
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    old_env = {k: os.environ.get(k) for k in env_overrides}
    os.environ.update(env_overrides)

    module_name = "audio_email_under_test"
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

    return module
