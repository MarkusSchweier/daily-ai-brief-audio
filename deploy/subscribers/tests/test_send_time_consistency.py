"""Drift guard for PRD instant-welcome-brief.md FR-12/AC-10: the live Managed Agents
deployment's schedule (`deploy/managed-agent/deployment.json`) must agree with the
canonical send-time source (`subscriber_common.WEEKDAY_SEND_HOUR/MINUTE/TIMEZONE`) that
the welcome email's prose renders from.

`deployment.json` is applied manually via the Deployments API and cannot import this
runtime module (PRD §7), so this is a *validating* check, not a two-way derivation --
exactly the "pragmatic canonical-consistency guarantee" the PRD calls for. It is the
thing that catches future drift (e.g. someone changes the schedule cron without updating
the constants, or vice versa) rather than letting it go silent.
"""

import json
from pathlib import Path

import subscriber_common as common

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOYMENT_JSON_PATH = REPO_ROOT / "deploy" / "managed-agent" / "deployment.json"


def _load_schedule() -> dict:
    with open(DEPLOYMENT_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data["schedule"]


def test_deployment_json_exists_at_the_expected_path():
    # Fails loudly (not silently) if the file ever moves, rather than the cron/timezone
    # assertions below silently not running against anything.
    assert DEPLOYMENT_JSON_PATH.is_file(), f"expected {DEPLOYMENT_JSON_PATH} to exist"


def test_deployment_json_timezone_matches_the_canonical_send_time_source():
    schedule = _load_schedule()
    assert schedule["timezone"] == common.WEEKDAY_SEND_TIMEZONE


def test_deployment_json_cron_hour_and_minute_match_the_canonical_send_time_source():
    schedule = _load_schedule()
    # Standard 5-field cron: "minute hour day month weekday".
    fields = schedule["cron"].split()
    assert len(fields) == 5, f"expected a 5-field cron expression, got {schedule['cron']!r}"
    minute, hour = fields[0], fields[1]
    assert int(minute) == common.WEEKDAY_SEND_MINUTE
    assert int(hour) == common.WEEKDAY_SEND_HOUR


def test_deployment_json_weekday_send_time_label_matches_the_prose_used_in_welcome_email():
    """Belt-and-suspenders: reconstruct the human-readable label from deployment.json's
    own values and compare it to what the welcome email actually renders, so this test
    also catches a canonical-source edit that forgets to keep hour/minute/timezone
    mutually consistent with each other."""
    schedule = _load_schedule()
    minute, hour = schedule["cron"].split()[0], schedule["cron"].split()[1]
    label_from_deployment_json = f"{int(hour):02d}:{int(minute):02d} ({schedule['timezone']})"
    assert label_from_deployment_json == common.weekday_send_time_label()
