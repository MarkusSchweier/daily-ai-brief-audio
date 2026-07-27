"""Cross-unit drift guard for the hand-duplicated `audio_link.py` module (ADR-0017 D10:
"a test reads the *other* unit's copy from the repo and asserts byte-equality" --
following the existing cross-unit precedent of
`deploy/subscribers/tests/test_send_time_consistency.py` and
`deploy/feedback/tests/test_feedback_token_cross_copy.py`'s
`test_all_three_copies_are_byte_identical`).

`deploy/delivery/` and `deploy/subscribers/` are independent Lambda deployment units
with no shared package (ADR-0017 D10's "Alternatives considered"), so this file is
copied, not imported -- this test is what turns "remember to mirror the change" (PRD
R4) into a failing test.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

DELIVERY_COPY = REPO_ROOT / "deploy" / "delivery" / "functions" / "deliver" / "audio_link.py"
SUBSCRIBERS_COPY = REPO_ROOT / "deploy" / "subscribers" / "layers" / "common" / "python" / "audio_link.py"


def test_both_copies_exist():
    assert DELIVERY_COPY.is_file(), f"expected {DELIVERY_COPY} to exist"
    assert SUBSCRIBERS_COPY.is_file(), f"expected {SUBSCRIBERS_COPY} to exist"


def test_audio_link_copies_are_byte_identical_across_deploy_units():
    delivery_src = DELIVERY_COPY.read_text(encoding="utf-8")
    subscribers_src = SUBSCRIBERS_COPY.read_text(encoding="utf-8")

    assert delivery_src == subscribers_src


# ---------------------------------------------------------------------------
# AUDIO_BASE_URL (ADR-0017 D6): a hand-duplicated module CONSTANT, not part of
# audio_link.py itself -- delivery_core.py and the welcome-send handler each
# define their own `AUDIO_BASE_URL = "https://briefing.mschweier.com"`. Nothing
# guarded this pair before (review gap, matching the pre-existing precedent that
# SUBSCRIBE_SITE_URL also has no such cross-check) -- if the two ever diverge,
# the daily brief and the welcome email would link to two different hosts with
# no test catching it. Parses the source directly (same technique
# test_send_time_consistency.py uses) since these are independent deploy units
# that cannot import each other.
# ---------------------------------------------------------------------------

DELIVERY_CORE_COPY = REPO_ROOT / "deploy" / "delivery" / "functions" / "deliver" / "delivery_core.py"
WELCOME_SEND_COPY = REPO_ROOT / "deploy" / "subscribers" / "functions" / "welcome-send" / "handler.py"

_AUDIO_BASE_URL_RE = re.compile(r'^AUDIO_BASE_URL\s*=\s*"([^"]+)"', re.MULTILINE)


def _find_audio_base_url(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = _AUDIO_BASE_URL_RE.search(text)
    assert match, f"expected an AUDIO_BASE_URL = \"...\" module constant in {path}"
    return match.group(1)


def test_audio_base_url_constant_is_identical_across_both_deploy_units():
    delivery_url = _find_audio_base_url(DELIVERY_CORE_COPY)
    welcome_url = _find_audio_base_url(WELCOME_SEND_COPY)

    assert delivery_url == welcome_url
    # Pin the actual expected value too, so a coordinated-but-wrong edit of both
    # copies at once (which the equality check alone would not catch) also fails.
    assert delivery_url == "https://briefing.mschweier.com"
