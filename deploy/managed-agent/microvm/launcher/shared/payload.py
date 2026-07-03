"""Run-hook payload construction.

Ported verbatim from AWS's reference implementation (src/functions/shared/payload.py)
— see shared/constants.py for the porting note.

Builds the per-session dispatch blob delivered to the MicroVM via runHookPayload
(the request body of the /run lifecycle hook). Contains only non-secret data:
session id, environment id, region, and a *reference* to the Secrets Manager
secret holding the environment key. The environment key itself is never placed
in this blob (ADR-0004's credential-boundary design: the launcher never reads or
forwards the environment key, only its secret ARN).
"""

from __future__ import annotations

import json
from typing import Any

from shared.constants import RUN_HOOK_PAYLOAD_VERSION
from shared.types import LauncherConfig, WebhookEvent

# Keys that must never appear anywhere in the run hook payload.
_FORBIDDEN_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_ENVIRONMENT_KEY")


def build_run_hook_payload(event: WebhookEvent, cfg: LauncherConfig) -> str:
    """Build the run hook payload JSON string for a started session."""
    session: dict[str, Any] = {
        "ANTHROPIC_SESSION_ID": event.session_id,
        "ANTHROPIC_ENVIRONMENT_ID": cfg.environment_id,
        "ENVIRONMENT_KEY_SECRET_ID": cfg.environment_key_secret_id,
        "AWS_REGION": cfg.aws_region,
    }
    if cfg.base_url is not None:
        session["ANTHROPIC_BASE_URL"] = cfg.base_url

    payload = json.dumps({"version": RUN_HOOK_PAYLOAD_VERSION, "session": session})
    for forbidden in _FORBIDDEN_KEYS:
        assert forbidden not in payload, f"forbidden key {forbidden!r} leaked into run hook payload"
    return payload
