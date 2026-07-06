"""Short-lived, signed, self-attesting read-capability token for `GET
/recent-briefs`: mint (here, `candidate_sync/trigger.py`) / verify (the
delivery Lambda) a `<payload_b64url>.<sig_b64url>` HMAC-SHA256 token that
expires within minutes.

See docs/adr/0014-agent-system-redesign-topology.md, Decision 2d's "Correction
(2026-07-06): how the read token actually reaches a `cloud` candidate" section
(status: ACCEPTED, ratified by the human 2026-07-06) for the full rationale:
`initial_events` (the task prompt) is the ONLY channel available today to get a
per-run value into a `cloud` candidate's sandbox, and `initial_events` is echoed
into the session transcript -- so the injected value must be NON-DURABLE rather
than a long-lived secret needing rotation after every run. This module is that
non-durable capability: even when it lands in a transcript, it is dead within
minutes, and a leaked transcript token cannot be replayed.

This is the `feedback_token.py` scheme (ADR-0011/0012:
`<payload_b64url>.<sig_b64url>`, `HMAC-SHA256(secret, payload_b64url)`,
stdlib-only, constant-time verify) almost verbatim -- the ONE addition is an
`exp` (expiry) claim, since this token IS a short-lived capability (unlike the
feedback token, which deliberately omits expiry -- that one is an
attribution-integrity token, not a capability grant). The signing/verification
key is the SAME `daily-ai-brief/recent-briefs-read-bearer-secret` that already
gates `GET /recent-briefs` (`recent_briefs_auth.py`) -- no new secret, no new
IAM; it is simply now used as an HMAC key rather than compared directly as a
static bearer value. `candidate_sync/trigger.py` reads that secret's value from
a LOCAL environment variable (`RECENT_BRIEFS_SIGNING_KEY`, populated
out-of-band by the operator from Secrets Manager) -- NOT via any AWS call --
preserving this package's "pure local tool, no AWS" property.

Duplicated verbatim (past this docstring) in:
  - deploy/delivery/functions/deliver/recent_briefs_token.py  (the VERIFY
    side, imported by recent_briefs_auth.py / handler.py)
  - deploy/candidates/candidate_sync/recent_briefs_token.py  (this copy --
    the MINT side, imported by candidate_sync/trigger.py, which mints a token
    per triggered run and substitutes it into a candidate's task prompt)

Two independent deploy units, kept byte-identical by hand past this docstring --
same convention `feedback_token.py`'s own module docstring documents (ADR-0012
§A: hand-duplicated, not a shared package, across independent deploy units). Any
change here MUST be applied to the sibling copy too.

Stdlib-only (`hmac`, `hashlib`, `base64`, `json`, `time`) -- no new dependency in
either runtime.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

_SCHEME_VERSION = 1
_SCOPE = "recent-briefs"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # Restore padding stripped by _b64url_encode; base64.urlsafe_b64decode requires
    # the input length be a multiple of 4.
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _sign(signing_key: str, payload_b64url: str) -> str:
    digest = hmac.new(signing_key.encode("utf-8"), payload_b64url.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def generate(signing_key: str, *, ttl_seconds: int, now: int | None = None) -> str:
    """Mint a fresh recent-briefs read token, valid for `ttl_seconds` from `now`
    (defaulting to the real current time). Returns `<payload_b64url>.<sig_b64url>`.

    Payload: `{"v": 1, "scope": "recent-briefs", "exp": <unix ts>}` -- no identity
    field is needed (the capability is uniform: "read the last N public briefs"),
    mirroring the ADR's suggested shape exactly."""
    if now is None:
        now = int(time.time())
    payload = {"v": _SCHEME_VERSION, "scope": _SCOPE, "exp": now + ttl_seconds}
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64url = _b64url_encode(payload_json.encode("utf-8"))
    sig_b64url = _sign(signing_key, payload_b64url)
    return f"{payload_b64url}.{sig_b64url}"


def verify(signing_key: str, token: str | None, *, now: int | None = None) -> bool:
    """True only if `token` carries a valid signature under `signing_key`, the
    `recent-briefs` scope, and an `exp` that has not yet passed (as of `now`,
    defaulting to the real current time). Any failure -- malformed shape, bad
    base64, bad JSON, wrong `v`, wrong/missing `scope`, missing/malformed `exp`,
    an expired token, or a signature mismatch -- returns False; this function
    NEVER raises on attacker-controlled input and never leaks which check
    failed (mirrors `feedback_token.validate()`'s fail-closed, non-leaking
    degrade path). Callers must treat False as unauthorized (401), never a
    fall-open."""
    if not token:
        return False

    # Split on the LAST "." -- the payload itself is base64url (alphabet has no
    # "."), so this is unambiguous; mirrors feedback_token.validate()'s own rule.
    if token.count(".") != 1:
        return False
    payload_b64url, sig_b64url = token.rsplit(".", 1)
    if not payload_b64url or not sig_b64url:
        return False

    try:
        expected_sig_b64url = _sign(signing_key, payload_b64url)
    except Exception:
        return False

    # Constant-time compare (mirrors feedback_token.py / ADR-0011's own choice) --
    # checked BEFORE decoding the payload, so a tampered signature is rejected
    # without ever needing to parse (or trust) the payload it claims to sign.
    if not hmac.compare_digest(expected_sig_b64url, sig_b64url):
        return False

    try:
        payload_bytes = _b64url_decode(payload_b64url)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return False

    if not isinstance(payload, dict):
        return False
    if payload.get("v") != _SCHEME_VERSION:
        return False
    if payload.get("scope") != _SCOPE:
        return False

    exp = payload.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool):
        return False

    if now is None:
        now = int(time.time())
    return exp > now


__all__ = ["generate", "verify"]
