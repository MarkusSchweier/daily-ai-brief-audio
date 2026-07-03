"""Cross-copy compatibility test for the three hand-duplicated `feedback_token.py`
copies (docs/adr/0011, docs/adr/0012 §A): proves a token `generate`d by the
managed-agent (microVM) copy `validate`s successfully under the feedback-stack
(submit Lambda) copy — i.e. the three independent deploy units agree on the exact
wire format (payload JSON shape, base64url encoding, HMAC construction).

Loads both copies by file path (rather than relying on any shared package/sys.path
convention) so this test works regardless of which directory pytest is invoked from,
mirroring how `deploy/subscribers/tests/conftest.py` and
`deploy/managed-agent/tests/conftest.py` already load same-named modules from
different directories under unique names to avoid `sys.path` collisions.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

MANAGED_AGENT_COPY = REPO_ROOT / "deploy" / "managed-agent" / "pipeline" / "feedback_token.py"
SUBSCRIBERS_COPY = REPO_ROOT / "deploy" / "subscribers" / "layers" / "common" / "python" / "feedback_token.py"
FEEDBACK_COPY = REPO_ROOT / "deploy" / "feedback" / "functions" / "submit" / "feedback_token.py"


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


managed_agent_feedback_token = _load("managed_agent_feedback_token_under_test", MANAGED_AGENT_COPY)
subscribers_feedback_token = _load("subscribers_feedback_token_under_test", SUBSCRIBERS_COPY)
feedback_stack_feedback_token = _load("feedback_stack_feedback_token_under_test", FEEDBACK_COPY)

SECRET = "shared-test-signing-secret"


def test_managed_agent_generated_token_validates_under_feedback_stack_copy():
    token = managed_agent_feedback_token.generate(SECRET, "mail@mschweier.com", "2026-07-03")

    result = feedback_stack_feedback_token.validate(SECRET, token)

    assert result.valid is True
    assert result.identity == "mail@mschweier.com"
    assert result.brief_date == "2026-07-03"


def test_subscribers_generated_token_validates_under_feedback_stack_copy():
    token = subscribers_feedback_token.generate(SECRET, "sub@example.com", "2026-07-02")

    result = feedback_stack_feedback_token.validate(SECRET, token)

    assert result.valid is True
    assert result.identity == "sub@example.com"
    assert result.brief_date == "2026-07-02"


def test_feedback_stack_generated_token_validates_under_managed_agent_copy():
    token = feedback_stack_feedback_token.generate(SECRET, "roundtrip@example.com", "2026-06-30")

    result = managed_agent_feedback_token.validate(SECRET, token)

    assert result.valid is True
    assert result.identity == "roundtrip@example.com"
    assert result.brief_date == "2026-06-30"


def test_all_three_copies_are_byte_identical():
    """Belt-and-braces: the ADR requires byte-identical copies (kept in sync by hand).
    This directly guards against silent drift between deploys, independent of the
    behavioral round-trip tests above."""
    managed_agent_src = MANAGED_AGENT_COPY.read_text(encoding="utf-8")
    subscribers_src = SUBSCRIBERS_COPY.read_text(encoding="utf-8")
    feedback_src = FEEDBACK_COPY.read_text(encoding="utf-8")

    assert managed_agent_src == subscribers_src == feedback_src
