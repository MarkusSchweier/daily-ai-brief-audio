"""delivery_client.py -- the MicroVM-side API client for the decoupled delivery
boundary (ADR-0015 D3/D4, full production decouple).

After cut-over this REPLACES the in-VM Polly/SES/S3 delivery that
`audio_email.py` does today: the content-generation agent produces the four
content artifacts (brief markdown, listening script, candidates.json,
source-usage.json), and this client hands them across the network to the
`deploy/delivery/` boundary's `POST /deliver` (which derives HTML, synthesizes
audio, sends, and archives all four) and reads recent priors via
`GET /recent-briefs`. It holds NO AWS credentials or IAM at all (PRD FR-1) -- its
only capabilities are two bearer-authed HTTP calls.

It mirrors `audio_email.py`'s two CLI shapes so `deployment.json` swaps
`audio_email.py` -> `delivery_client.py` with no other prompt change:

  * `python3 delivery_client.py read-recent-briefs [count]` -- fetch the last
    `count` prior briefs from `GET /recent-briefs` and write each to
    `WORKING_FOLDER/AI Brief - <date>.md` (exactly where the skill's own
    WORKING_FOLDER search looks), replacing audio_email.py's S3-backed step 0.
  * `python3 delivery_client.py` (no args, send mode) -- read the four artifacts,
    `POST /deliver`, and poll `GET /deliver/{id}` to a terminal state.

Fail-safe (ADR-0015 D8 -- "never lose the brief" across the new network hop):
  * SEND mode fails LOUDLY, never silently: an unreachable/erroring trigger before
    a deliveryId is returned, a terminal `failed`, or a poll timeout all raise a
    non-zero exit with a clear DELIVERY_* log line. The four artifacts remain in
    the workspace, so a failed production delivery is a visible, re-drivable event,
    not a dropped brief. The caller-supplied idempotency key (the run's brief_date)
    makes a re-trigger safe (server dedupes -- ADR-0015 D7).
  * READ-RECENT-BRIEFS mode degrades GRACEFULLY: a missing/erroring priors read is
    logged and treated as "no priors" (an empty result is the normal cold-start
    case), never aborting the run -- exactly matching audio_email.py /
    brief_history.read_recent_prior_briefs()'s own graceful-degradation contract.

The HTTP layer is a single injectable module function (`_http_request`), so every
branch here is unit-testable with a fake transport and no real network -- see
tests/test_delivery_client.py.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

CONTRACT_VERSION = 2
DEFAULT_RECENT_BRIEFS_COUNT = 3

# Poll budget for a real delivery: Polly's own ~5-minute allowance plus the SES
# fan-out loop -- generously above the real runtime, matching the delivery Lambda's
# own 10-minute timeout (ADR-0014 Decision 2a "Why async").
POLL_TIMEOUT_SECONDS = 8 * 60
POLL_INTERVAL_SECONDS = 6
HTTP_TIMEOUT_SECONDS = 30


class DeliveryError(RuntimeError):
    """A send-mode failure that must surface loudly (non-zero exit) -- never a
    silently-swallowed dropped brief (ADR-0015 D8)."""


def _today_local_date(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")


def _http_request(method: str, url: str, headers: dict, body: bytes | None = None, timeout: int = HTTP_TIMEOUT_SECONDS):
    """The ONE real network primitive -- returns (status_code, parsed_json_or_None).
    Isolated as a module function so tests inject a fake transport instead. A
    non-2xx HTTP status is returned as its code (never raised as HTTPError) so
    callers branch on the code explicitly; a genuine transport failure (DNS,
    connection refused, timeout) raises urllib.error.URLError, which callers handle
    per their own fail-safe policy."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return e.code, parsed


# ---------------------------------------------------------------------------
# read-recent-briefs mode (ADR-0015 D4) -- GET /recent-briefs, write priors.
# ---------------------------------------------------------------------------


def read_recent_briefs(base_url: str, read_token: str, working_folder: str, count: int, *, http=_http_request) -> int:
    """Fetch up to `count` recent prior briefs from `GET /recent-briefs` and write
    each to `working_folder/AI Brief - <date>.md`. Returns how many were written.

    Graceful degradation (never raises): a missing base URL/token, a non-200
    response, or a transport error all log a DELIVERY_RECENT_BRIEFS_* line and
    return 0 -- a run with no priors is the normal cold-start case and must proceed,
    exactly like audio_email.py's S3-backed step 0 degrading to an empty read."""
    if not base_url or not read_token:
        print("DELIVERY_RECENT_BRIEFS_SKIPPED: base URL or read token not configured")
        return 0
    url = f"{base_url}/recent-briefs?count={int(count)}"
    headers = {"Authorization": f"Bearer {read_token}"}
    try:
        status, payload = http("GET", url, headers, None)
    except Exception as e:  # noqa: BLE001 - graceful degrade: priors read must never abort the run
        print("DELIVERY_RECENT_BRIEFS_FAILED:", repr(e))
        return 0
    if status != 200 or not isinstance(payload, dict):
        print(f"DELIVERY_RECENT_BRIEFS_FAILED: HTTP {status}")
        return 0

    written = 0
    for prior in payload.get("briefs", []):
        date = prior.get("date")
        markdown = prior.get("markdown")
        if not date or markdown is None:
            continue
        path = os.path.join(working_folder, f"AI Brief - {date}.md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(markdown)
            print("DELIVERY_RECENT_BRIEF_WROTE", path)
            written += 1
        except Exception as e:  # noqa: BLE001 - one bad write must not lose the others
            print("DELIVERY_RECENT_BRIEF_WRITE_FAILED:", path, repr(e))
    print(f"DELIVERY_RECENT_BRIEFS_DONE wrote={written}")
    return written


# ---------------------------------------------------------------------------
# send mode (ADR-0015 D3) -- POST /deliver, poll to terminal.
# ---------------------------------------------------------------------------


def trigger_and_poll(
    base_url: str,
    bearer: str,
    payload: dict,
    *,
    http=_http_request,
    poll_timeout: int = POLL_TIMEOUT_SECONDS,
    poll_interval: int = POLL_INTERVAL_SECONDS,
    sleep=time.sleep,
) -> dict:
    """POST `payload` to `/deliver`, then poll `/deliver/{deliveryId}` to a terminal
    state. Returns the terminal summary dict on success.

    Raises DeliveryError (loud, non-zero exit) on: a non-202 trigger response, a
    trigger transport failure, a terminal `failed`, or a poll timeout -- so a
    production delivery that did not demonstrably succeed is never mistaken for one
    that did (ADR-0015 D8). The brief's four artifacts remain in the workspace for a
    safe re-drive (the idempotency key dedupes a retry, ADR-0015 D7)."""
    if not base_url or not bearer:
        raise DeliveryError("DELIVERY_TRIGGER_FAILED: delivery base URL or bearer not configured")

    headers = {"content-type": "application/json", "Authorization": f"Bearer {bearer}"}
    body = json.dumps(payload).encode("utf-8")
    try:
        status, resp = http("POST", f"{base_url}/deliver", headers, body)
    except Exception as e:  # noqa: BLE001 - a pre-id transport failure is safe to surface + retry
        raise DeliveryError(f"DELIVERY_TRIGGER_FAILED: transport error {e!r}") from e
    if status != 202 or not isinstance(resp, dict) or not resp.get("deliveryId"):
        raise DeliveryError(f"DELIVERY_TRIGGER_FAILED: HTTP {status} resp={resp!r}")

    delivery_id = resp["deliveryId"]
    if resp.get("idempotentReplay"):
        print("DELIVERY_TRIGGER_IDEMPOTENT_REPLAY delivery_id=", delivery_id)
    else:
        print("DELIVERY_TRIGGERED delivery_id=", delivery_id)

    poll_headers = {"Authorization": f"Bearer {bearer}"}
    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        try:
            status, poll = http("GET", f"{base_url}/deliver/{delivery_id}", poll_headers, None)
        except Exception as e:  # noqa: BLE001 - a transient poll error is retried until the deadline
            print("DELIVERY_POLL_TRANSIENT_ERROR:", repr(e))
            sleep(poll_interval)
            continue
        state = poll.get("status") if isinstance(poll, dict) else None
        if state == "succeeded":
            summary = poll.get("summary", {})
            print("DELIVERY_SUCCEEDED delivery_id=", delivery_id, "summary=", json.dumps(summary))
            return summary
        if state == "failed":
            raise DeliveryError(f"DELIVERY_FAILED delivery_id={delivery_id} error={poll.get('error')!r}")
        sleep(poll_interval)

    raise DeliveryError(f"DELIVERY_POLL_TIMEOUT delivery_id={delivery_id} after {poll_timeout}s")


def _read_artifact(env_var: str, *, required: bool) -> str | None:
    """Read a file whose path is in `env_var`. A required artifact missing (no env
    var, or unreadable) raises DeliveryError before any send (no brief to deliver);
    an optional (additive) artifact missing returns None -- it will simply not be
    archived, never blocking the send (ADR-0015 D2)."""
    path = os.environ.get(env_var)
    if not path:
        if required:
            raise DeliveryError(f"DELIVERY_TRIGGER_FAILED: {env_var} is required but not set")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception as e:  # noqa: BLE001
        if required:
            raise DeliveryError(f"DELIVERY_TRIGGER_FAILED: cannot read {env_var}={path}: {e!r}") from e
        print(f"DELIVERY_ARTIFACT_SKIPPED: cannot read optional {env_var}={path}: {e!r}")
        return None


def _truthy(env_var: str) -> bool:
    return os.environ.get(env_var, "").strip().lower() in ("1", "true", "yes")


def build_send_payload() -> dict:
    """Assemble the contract-v2 POST /deliver body from env + workspace artifacts.
    brief_markdown + listening_script are required; candidates + source_usage are
    additive (best-effort). The idempotency key is the run's brief_date."""
    timezone = os.environ.get("PIPELINE_TIMEZONE", "Europe/Berlin")
    brief_date = os.environ.get("BRIEF_DATE") or _today_local_date(timezone)
    return {
        "contractVersion": CONTRACT_VERSION,
        "brief_markdown": _read_artifact("BRIEF_MARKDOWN_PATH", required=True),
        "listening_script": _read_artifact("LISTENING_SCRIPT_PATH", required=True),
        "candidates": _read_artifact("CANDIDATES_PATH", required=False),
        "source_usage": _read_artifact("SOURCE_USAGE_PATH", required=False),
        "metadata": {
            "email_subject": os.environ.get("EMAIL_SUBJECT", "Daily AI Brief"),
            "brief_date": brief_date,
            # Belt-and-suspenders, matching audio_email.py: fan-out is OFF unless
            # explicitly enabled (a non-production/validation run never fans out).
            "enable_subscriber_fanout": _truthy("ENABLE_SUBSCRIBER_FANOUT"),
            "idempotency_key": brief_date,
        },
    }


def _send_mode() -> int:
    base_url = os.environ.get("DELIVERY_BASE_URL", "").rstrip("/")
    bearer = os.environ.get("DELIVERY_BEARER_TOKEN", "")
    payload = build_send_payload()
    summary = trigger_and_poll(base_url, bearer, payload)
    # A successful delivery whose audio failed is still a success (text-only
    # fail-safe on the delivery side) -- surface it but do not fail the run.
    if summary.get("audio_ok") is False:
        print("DELIVERY_NOTE: audio synthesis failed on the delivery side; brief was sent text-only")
    return 0


def _read_recent_briefs_mode(count: int) -> int:
    base_url = os.environ.get("DELIVERY_BASE_URL", "").rstrip("/")
    read_token = os.environ.get("RECENT_BRIEFS_TOKEN", "")
    working_folder = os.environ.get("WORKING_FOLDER", "/workspace")
    read_recent_briefs(base_url, read_token, working_folder, count)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "read-recent-briefs":
        count = int(argv[2]) if len(argv) > 2 else DEFAULT_RECENT_BRIEFS_COUNT
        return _read_recent_briefs_mode(count)
    return _send_mode()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except DeliveryError as e:
        # Loud, non-zero exit -- the run visibly fails rather than silently
        # "succeeding" with no brief delivered (ADR-0015 D8).
        print(str(e), file=sys.stderr)
        sys.exit(1)
