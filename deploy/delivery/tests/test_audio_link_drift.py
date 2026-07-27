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
