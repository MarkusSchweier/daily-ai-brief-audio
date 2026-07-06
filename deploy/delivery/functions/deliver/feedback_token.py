"""Signed, self-attesting feedback-link token: generate (here) / validate (feedback
stack) a URL-safe token encoding `(recipient identity, brief date)` with no DB lookup.

See docs/adr/0011-feedback-link-signed-token-scheme.md for the wire format, the HMAC
construction, and the (deliberate) no-expiry decision, and
docs/adr/0012-feedback-standalone-stack-and-token-helper-packaging.md §A for why this
module is hand-duplicated (not a shared package) across independent deploy units.

Duplicated verbatim in:
  - deploy/managed-agent/pipeline/feedback_token.py   (imported by audio_email.py -- the LIVE production copy, untouched by the agent-system-redesign epic)
  - deploy/subscribers/layers/common/python/feedback_token.py  (imported by welcome-send/handler.py)
  - deploy/feedback/functions/submit/feedback_token.py  (imported by the submit handler)
  - deploy/delivery/functions/deliver/feedback_token.py  (this copy — imported by delivery_core.py, the new decoupled delivery Lambda, PRD docs/prd/agent-system-redesign.md / ADR-0014 Decision 2a)

Four independent deploy units (a microVM image, two Lambda layers/functions, and a
new standalone delivery Lambda), kept identical by hand — same convention as
`MAX_AUDIO_ATTACHMENT_BYTES`'s duplicated-constant docstring elsewhere in this repo. A
cross-copy compatibility test (a token `generate`d by one copy `validate`s under
another) pins the copies to one wire format; see the test suite for details. Any
change here MUST be applied to the other three copies too.

Stdlib-only (`hmac`, `hashlib`, `base64`, `json`) — no new dependency in any runtime.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

_SCHEME_VERSION = 1
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class FeedbackTokenResult:
    """Result of `validate()`. On any failure, `valid=False` and no other data is
    populated — never a partial or forged result (ADR-0011's "walk-up anonymous"
    degrade path)."""

    valid: bool
    identity: str | None = None
    brief_date: str | None = None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # Restore padding stripped by _b64url_encode; base64.urlsafe_b64decode requires
    # the input length be a multiple of 4.
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _sign(secret: str, payload_b64url: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64url.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def generate(secret: str, identity: str, brief_date: str) -> str:
    """Build a feedback token for `identity` (recipient email, lowercased/trimmed by
    the caller) and `brief_date` ("YYYY-MM-DD"). Returns `<payload_b64url>.<sig_b64url>`.
    """
    payload = {"v": _SCHEME_VERSION, "id": identity, "d": brief_date}
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64url = _b64url_encode(payload_json.encode("utf-8"))
    sig_b64url = _sign(secret, payload_b64url)
    return f"{payload_b64url}.{sig_b64url}"


def validate(secret: str, token: str | None) -> FeedbackTokenResult:
    """Validate `token` against `secret`. Any failure (malformed shape, bad base64,
    bad JSON, wrong `v`, missing/malformed `id`/`d`, or a signature mismatch) returns
    `FeedbackTokenResult(valid=False)` with no other data — never raises, never leaks
    which check failed (ADR-0011's "invalid token degrades to walk-up anonymous").
    """
    if not token:
        return FeedbackTokenResult(valid=False)

    # Split on the LAST "." — the payload itself is base64url (alphabet has no "."),
    # so a plain split(".") would already be safe, but splitting from the right is the
    # documented, unambiguous rule (ADR-0011 step 1).
    if token.count(".") != 1:
        return FeedbackTokenResult(valid=False)
    payload_b64url, sig_b64url = token.rsplit(".", 1)
    if not payload_b64url or not sig_b64url:
        return FeedbackTokenResult(valid=False)

    try:
        expected_sig_b64url = _sign(secret, payload_b64url)
    except Exception:
        return FeedbackTokenResult(valid=False)

    # Constant-time compare (ADR-0011, mirrors ADR-0003's token-compare choice).
    if not hmac.compare_digest(expected_sig_b64url, sig_b64url):
        return FeedbackTokenResult(valid=False)

    try:
        payload_bytes = _b64url_decode(payload_b64url)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return FeedbackTokenResult(valid=False)

    if not isinstance(payload, dict):
        return FeedbackTokenResult(valid=False)
    if payload.get("v") != _SCHEME_VERSION:
        return FeedbackTokenResult(valid=False)

    identity = payload.get("id")
    brief_date = payload.get("d")
    if not isinstance(identity, str) or not identity:
        return FeedbackTokenResult(valid=False)
    if not isinstance(brief_date, str) or not _DATE_RE.match(brief_date):
        return FeedbackTokenResult(valid=False)

    return FeedbackTokenResult(valid=True, identity=identity, brief_date=brief_date)


__all__ = ["FeedbackTokenResult", "generate", "validate"]
