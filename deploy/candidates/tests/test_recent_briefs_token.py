"""Unit tests for candidate_sync/recent_briefs_token.py -- the short-lived,
signed, self-attesting `GET /recent-briefs` read-capability token (ADR-0014
Decision 2d's "Correction: how the read token actually reaches a `cloud`
candidate", status ACCEPTED).

BYTE-IDENTICAL (past its module docstring) to
`deploy/delivery/functions/deliver/recent_briefs_token.py` -- this test file is
correspondingly a near-mirror of that copy's own test file
(`deploy/delivery/tests/test_recent_briefs_token.py`), so both independently-
deployed copies of the module are proven correct on their own, in their own
package's test suite, per this repo's hand-duplication testing convention
(mirrors how `feedback_token.py`'s several copies each carry their own tests).

Mirrors `feedback_token.py`'s own test-coverage shape (mint/verify roundtrip,
tamper detection, wrong-key rejection, malformed-input rejection), plus the ONE
genuinely new behavior this scheme adds over the feedback token: an `exp` claim
that must be enforced -- an expired token is rejected even with an otherwise
perfectly valid signature."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from candidate_sync import recent_briefs_token

SIGNING_KEY = "test-recent-briefs-signing-key-abc123"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _sign(signing_key: str, payload_b64url: str) -> str:
    digest = hmac.new(signing_key.encode("utf-8"), payload_b64url.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _build_token(payload: dict, *, signing_key: str = SIGNING_KEY) -> str:
    """Construct a token directly from an arbitrary payload dict, bypassing
    `generate()` -- used to exercise malformed/edge-case payloads `generate()`
    itself would never produce (wrong scope, missing exp, non-int exp, etc.)."""
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64url = _b64url_encode(payload_json.encode("utf-8"))
    sig_b64url = _sign(signing_key, payload_b64url)
    return f"{payload_b64url}.{sig_b64url}"


# ---------------------------------------------------------------------------
# Roundtrip: a freshly-minted token verifies.
# ---------------------------------------------------------------------------


def test_generate_then_verify_roundtrip_succeeds():
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000) is True


def test_generate_then_verify_succeeds_moments_before_expiry():
    """A token is still valid at any time strictly before its `exp` -- confirms
    the boundary isn't off-by-one in the wrong direction."""
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000 + 899) is True


def test_token_shape_is_payload_dot_signature():
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    assert token.count(".") == 1
    payload_b64url, sig_b64url = token.split(".")
    assert payload_b64url and sig_b64url


def test_payload_contains_expected_fields():
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    payload_b64url, _sig = token.split(".")
    payload = json.loads(_b64url_decode(payload_b64url).decode("utf-8"))
    assert payload == {"v": 1, "scope": "recent-briefs", "exp": 1_000_900}


# ---------------------------------------------------------------------------
# Expiry -- the ONE behavior this scheme adds over feedback_token.py.
# ---------------------------------------------------------------------------


def test_expired_token_is_rejected_via_future_now():
    """Mint with a normal TTL, then verify at a `now` past the expiry -- the
    signature is perfectly valid, but the token must still be rejected."""
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000 + 901) is False


def test_expired_token_is_rejected_via_past_exp_at_mint_time():
    """Mint a token whose `exp` is already in the past (e.g. a negative/zero TTL)
    -- it must never verify, even immediately."""
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=-1, now=1_000_000)
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000) is False


def test_token_exactly_at_exp_is_rejected_not_off_by_one_lenient():
    """`exp > now` is the enforced rule -- a `now` exactly equal to `exp` must be
    treated as expired (strictly greater required), not accepted."""
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_900) is False


def test_missing_exp_claim_is_rejected():
    token = _build_token({"v": 1, "scope": "recent-briefs"})
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000) is False


def test_non_integer_exp_claim_is_rejected():
    token = _build_token({"v": 1, "scope": "recent-briefs", "exp": "not-a-number"})
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000) is False


def test_boolean_exp_claim_is_rejected():
    """`bool` is a subclass of `int` in Python -- explicitly guard against a
    `True`/`False` exp claim being silently accepted as 1/0."""
    token = _build_token({"v": 1, "scope": "recent-briefs", "exp": True})
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=0) is False


# ---------------------------------------------------------------------------
# Scope enforcement.
# ---------------------------------------------------------------------------


def test_wrong_scope_is_rejected():
    token = _build_token({"v": 1, "scope": "some-other-scope", "exp": 2_000_000})
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000) is False


def test_missing_scope_is_rejected():
    token = _build_token({"v": 1, "exp": 2_000_000})
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000) is False


def test_wrong_scheme_version_is_rejected():
    token = _build_token({"v": 2, "scope": "recent-briefs", "exp": 2_000_000})
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000) is False


# ---------------------------------------------------------------------------
# Tamper detection -- payload and signature.
# ---------------------------------------------------------------------------


def test_tampered_payload_is_rejected():
    """Flip the scope in the payload without re-signing -- the signature no
    longer matches the (new) payload bytes."""
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    payload_b64url, sig_b64url = token.split(".")
    tampered_payload = _b64url_encode(json.dumps({"v": 1, "scope": "recent-briefs", "exp": 9_999_999_999}).encode())
    tampered_token = f"{tampered_payload}.{sig_b64url}"
    assert recent_briefs_token.verify(SIGNING_KEY, tampered_token, now=1_000_000) is False


def test_tampered_signature_is_rejected():
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    payload_b64url, sig_b64url = token.split(".")
    tampered_sig = sig_b64url[:-4] + ("aaaa" if sig_b64url[-4:] != "aaaa" else "bbbb")
    tampered_token = f"{payload_b64url}.{tampered_sig}"
    assert recent_briefs_token.verify(SIGNING_KEY, tampered_token, now=1_000_000) is False


def test_wrong_signing_key_is_rejected():
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    assert recent_briefs_token.verify("a-completely-different-key", token, now=1_000_000) is False


# ---------------------------------------------------------------------------
# Malformed input -- never raises, always resolves to False.
# ---------------------------------------------------------------------------


def test_none_token_is_rejected():
    assert recent_briefs_token.verify(SIGNING_KEY, None) is False


def test_empty_string_token_is_rejected():
    assert recent_briefs_token.verify(SIGNING_KEY, "") is False


def test_token_with_no_dot_is_rejected():
    assert recent_briefs_token.verify(SIGNING_KEY, "nodothere") is False


def test_token_with_multiple_dots_is_rejected():
    assert recent_briefs_token.verify(SIGNING_KEY, "a.b.c") is False


def test_token_with_empty_payload_segment_is_rejected():
    assert recent_briefs_token.verify(SIGNING_KEY, ".somesignature") is False


def test_token_with_empty_signature_segment_is_rejected():
    assert recent_briefs_token.verify(SIGNING_KEY, "somepayload.") is False


def test_token_with_invalid_base64_payload_is_rejected():
    valid_token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    _payload, sig_b64url = valid_token.split(".")
    assert recent_briefs_token.verify(SIGNING_KEY, f"!!!not-valid-base64!!!.{sig_b64url}", now=1_000_000) is False


def test_token_with_valid_base64_but_non_json_payload_is_rejected():
    non_json_payload = _b64url_encode(b"not json at all")
    sig = _sign(SIGNING_KEY, non_json_payload)
    assert recent_briefs_token.verify(SIGNING_KEY, f"{non_json_payload}.{sig}", now=1_000_000) is False


def test_token_with_json_array_instead_of_object_payload_is_rejected():
    array_payload = _b64url_encode(json.dumps([1, 2, 3]).encode())
    sig = _sign(SIGNING_KEY, array_payload)
    assert recent_briefs_token.verify(SIGNING_KEY, f"{array_payload}.{sig}", now=1_000_000) is False


def test_verify_never_raises_on_a_grab_bag_of_malformed_input():
    """Fail-closed discipline: no attacker-controlled input should ever raise
    (which could crash the caller instead of cleanly rejecting)."""
    for bad_token in [
        "",
        ".",
        "..",
        "a" * 10000,
        "🎉.🎉",
        "not-base64-at-all.also-not-base64",
        None,
    ]:
        assert recent_briefs_token.verify(SIGNING_KEY, bad_token, now=1_000_000) is False


# ---------------------------------------------------------------------------
# Constant-time compare is used (not a short-circuiting `==`).
# ---------------------------------------------------------------------------


def test_constant_time_compare_is_used_for_signature_check(monkeypatch):
    """Pin the implementation choice: `hmac.compare_digest` must be the
    comparison primitive used to check the signature, not a plain `==` (which
    could leak timing information about how many leading bytes match)."""
    calls = []
    real_compare_digest = hmac.compare_digest

    def _spy_compare_digest(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(recent_briefs_token.hmac, "compare_digest", _spy_compare_digest)

    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    assert recent_briefs_token.verify(SIGNING_KEY, token, now=1_000_000) is True
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Default `now` (real wall-clock time) is exercised at least once, so the
# `now=None` code path (not just the injected-now path every other test uses)
# is covered.
# ---------------------------------------------------------------------------


def test_default_now_uses_real_wall_clock_time():
    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900)
    assert recent_briefs_token.verify(SIGNING_KEY, token) is True


# ---------------------------------------------------------------------------
# Cross-copy compatibility: a token minted by THIS copy verifies under the
# SIBLING (deploy/delivery) copy -- pins the two hand-duplicated modules to one
# wire format, mirroring feedback_token.py's own cross-copy compatibility test.
# ---------------------------------------------------------------------------


def test_token_minted_here_verifies_under_the_sibling_delivery_copy():
    import importlib.util
    import sys
    from pathlib import Path

    sibling_path = (
        Path(__file__).resolve().parent.parent.parent
        / "delivery"
        / "functions"
        / "deliver"
        / "recent_briefs_token.py"
    )
    spec = importlib.util.spec_from_file_location("sibling_recent_briefs_token", sibling_path)
    sibling_module = importlib.util.module_from_spec(spec)
    sys.modules["sibling_recent_briefs_token"] = sibling_module
    spec.loader.exec_module(sibling_module)

    token = recent_briefs_token.generate(SIGNING_KEY, ttl_seconds=900, now=1_000_000)
    assert sibling_module.verify(SIGNING_KEY, token, now=1_000_000) is True
