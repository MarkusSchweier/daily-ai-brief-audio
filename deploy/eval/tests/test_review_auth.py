"""Unit tests for functions/*/review_auth.py (ADR-0013 §E: shared bearer secret,
hmac.compare_digest-checked; no secret => 401).
"""

import importlib.util
import sys
from pathlib import Path

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "functions"


def _import_review_auth(function_dir: str):
    module_name = f"review_auth_under_test_{function_dir}"
    if module_name in sys.modules:
        del sys.modules[module_name]
    path = FUNCTIONS_DIR / function_dir / "review_auth.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_authorized_with_matching_bearer_header():
    review_auth = _import_review_auth("read")
    event = {"headers": {"Authorization": "Bearer secret123"}}
    assert review_auth.is_authorized(event, secret="secret123") is True


def test_unauthorized_with_mismatched_bearer_header():
    review_auth = _import_review_auth("read")
    event = {"headers": {"Authorization": "Bearer wrong"}}
    assert review_auth.is_authorized(event, secret="secret123") is False


def test_authorized_with_query_string_k_param():
    review_auth = _import_review_auth("read")
    event = {"queryStringParameters": {"k": "secret123"}}
    assert review_auth.is_authorized(event, secret="secret123") is True


def test_unauthorized_with_no_secret_configured():
    """No secret configured (e.g. Secrets Manager fetch failed or unset) must fail
    CLOSED, never open."""
    review_auth = _import_review_auth("read")
    event = {"headers": {"Authorization": "Bearer anything"}}
    assert review_auth.is_authorized(event, secret=None) is False


def test_unauthorized_with_no_bearer_supplied():
    review_auth = _import_review_auth("read")
    event = {"headers": {}}
    assert review_auth.is_authorized(event, secret="secret123") is False


def test_unauthorized_response_shape():
    review_auth = _import_review_auth("read")
    response = review_auth.unauthorized_response()
    assert response["statusCode"] == 401


def test_review_auth_is_identical_across_all_function_copies():
    """Hand-duplicated across function directories (this app's own convention,
    mirroring feedback_token.py's multi-copy pattern in the rest of this repo) --
    must stay byte-identical."""
    copies = ["trigger", "poll", "submit-review", "read"]
    contents = {name: (FUNCTIONS_DIR / name / "review_auth.py").read_text() for name in copies}
    first = contents[copies[0]]
    for name in copies[1:]:
        assert contents[name] == first, f"review_auth.py in {name}/ has drifted from {copies[0]}/"
