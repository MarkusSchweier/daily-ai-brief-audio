"""Unit tests for `feedback_token.py` (docs/adr/0011): valid round-trip plus every
rejection path the ADR's Verification note enumerates — each must degrade to
`valid=False` with no other data (never a partial or forged result).

Tested here against the `deploy/feedback/functions/submit/` copy; the cross-copy
compatibility test (test_feedback_token_cross_copy.py) proves the other two copies
agree on the same wire format.
"""

from __future__ import annotations

import base64
import json

import feedback_token

SECRET = "test-signing-secret"
OTHER_SECRET = "a-different-secret"


def test_valid_round_trip():
    token = feedback_token.generate(SECRET, "reader@example.com", "2026-07-03")

    result = feedback_token.validate(SECRET, token)

    assert result.valid is True
    assert result.identity == "reader@example.com"
    assert result.brief_date == "2026-07-03"


def test_tampered_signature_is_rejected():
    token = feedback_token.generate(SECRET, "reader@example.com", "2026-07-03")
    payload_b64url, sig_b64url = token.rsplit(".", 1)
    tampered_sig = ("A" if sig_b64url[0] != "A" else "B") + sig_b64url[1:]
    tampered_token = f"{payload_b64url}.{tampered_sig}"

    result = feedback_token.validate(SECRET, tampered_token)

    assert result.valid is False
    assert result.identity is None
    assert result.brief_date is None


def test_altered_identity_is_rejected():
    """Changing the payload without re-signing must invalidate the token — proves an
    attacker cannot swap the attributed identity (ADR-0011 tamper-resistance)."""
    token = feedback_token.generate(SECRET, "victim@example.com", "2026-07-03")
    payload_b64url, sig_b64url = token.rsplit(".", 1)

    forged_payload = json.dumps(
        {"v": 1, "id": "attacker@example.com", "d": "2026-07-03"}, separators=(",", ":"), sort_keys=True
    )
    forged_payload_b64url = base64.urlsafe_b64encode(forged_payload.encode("utf-8")).rstrip(b"=").decode("ascii")
    forged_token = f"{forged_payload_b64url}.{sig_b64url}"

    result = feedback_token.validate(SECRET, forged_token)

    assert result.valid is False


def test_altered_date_is_rejected():
    token = feedback_token.generate(SECRET, "reader@example.com", "2026-07-03")
    payload_b64url, sig_b64url = token.rsplit(".", 1)

    forged_payload = json.dumps(
        {"v": 1, "id": "reader@example.com", "d": "2026-07-04"}, separators=(",", ":"), sort_keys=True
    )
    forged_payload_b64url = base64.urlsafe_b64encode(forged_payload.encode("utf-8")).rstrip(b"=").decode("ascii")
    forged_token = f"{forged_payload_b64url}.{sig_b64url}"

    result = feedback_token.validate(SECRET, forged_token)

    assert result.valid is False


def test_wrong_signing_secret_is_rejected():
    token = feedback_token.generate(SECRET, "reader@example.com", "2026-07-03")

    result = feedback_token.validate(OTHER_SECRET, token)

    assert result.valid is False


def test_wrong_version_is_rejected():
    payload = json.dumps({"v": 2, "id": "reader@example.com", "d": "2026-07-03"}, separators=(",", ":"), sort_keys=True)
    payload_b64url = base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")
    sig_b64url = feedback_token._sign(SECRET, payload_b64url)
    token = f"{payload_b64url}.{sig_b64url}"

    result = feedback_token.validate(SECRET, token)

    assert result.valid is False


def test_malformed_date_shape_is_rejected():
    payload = json.dumps({"v": 1, "id": "reader@example.com", "d": "not-a-date"}, separators=(",", ":"), sort_keys=True)
    payload_b64url = base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")
    sig_b64url = feedback_token._sign(SECRET, payload_b64url)
    token = f"{payload_b64url}.{sig_b64url}"

    result = feedback_token.validate(SECRET, token)

    assert result.valid is False


def test_malformed_base64_payload_is_rejected():
    sig_b64url = feedback_token._sign(SECRET, "not-valid-base64!!!")
    token = f"not-valid-base64!!!.{sig_b64url}"

    result = feedback_token.validate(SECRET, token)

    assert result.valid is False


def test_missing_segment_no_dot_is_rejected():
    result = feedback_token.validate(SECRET, "just-one-segment-no-dot")

    assert result.valid is False


def test_too_many_segments_is_rejected():
    result = feedback_token.validate(SECRET, "a.b.c")

    assert result.valid is False


def test_empty_payload_segment_is_rejected():
    token = feedback_token.generate(SECRET, "reader@example.com", "2026-07-03")
    _, sig_b64url = token.rsplit(".", 1)

    result = feedback_token.validate(SECRET, f".{sig_b64url}")

    assert result.valid is False


def test_empty_signature_segment_is_rejected():
    token = feedback_token.generate(SECRET, "reader@example.com", "2026-07-03")
    payload_b64url, _ = token.rsplit(".", 1)

    result = feedback_token.validate(SECRET, f"{payload_b64url}.")

    assert result.valid is False


def test_absent_token_is_rejected():
    result = feedback_token.validate(SECRET, None)

    assert result.valid is False
    assert result.identity is None
    assert result.brief_date is None


def test_empty_string_token_is_rejected():
    result = feedback_token.validate(SECRET, "")

    assert result.valid is False


def test_missing_id_field_is_rejected():
    payload = json.dumps({"v": 1, "d": "2026-07-03"}, separators=(",", ":"), sort_keys=True)
    payload_b64url = base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")
    sig_b64url = feedback_token._sign(SECRET, payload_b64url)
    token = f"{payload_b64url}.{sig_b64url}"

    result = feedback_token.validate(SECRET, token)

    assert result.valid is False


def test_missing_d_field_is_rejected():
    payload = json.dumps({"v": 1, "id": "reader@example.com"}, separators=(",", ":"), sort_keys=True)
    payload_b64url = base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")
    sig_b64url = feedback_token._sign(SECRET, payload_b64url)
    token = f"{payload_b64url}.{sig_b64url}"

    result = feedback_token.validate(SECRET, token)

    assert result.valid is False


def test_non_json_payload_is_rejected():
    payload_b64url = base64.urlsafe_b64encode(b"not json at all").rstrip(b"=").decode("ascii")
    sig_b64url = feedback_token._sign(SECRET, payload_b64url)
    token = f"{payload_b64url}.{sig_b64url}"

    result = feedback_token.validate(SECRET, token)

    assert result.valid is False


def test_json_array_instead_of_object_is_rejected():
    payload = json.dumps(["v", 1])
    payload_b64url = base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")
    sig_b64url = feedback_token._sign(SECRET, payload_b64url)
    token = f"{payload_b64url}.{sig_b64url}"

    result = feedback_token.validate(SECRET, token)

    assert result.valid is False


def test_token_is_url_safe():
    """No characters outside the base64url alphabet + '.' — safe as a single query
    parameter value with no further percent-encoding required (ADR-0011)."""
    token = feedback_token.generate(SECRET, "reader+test@example.com", "2026-07-03")

    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
    assert set(token) <= allowed
