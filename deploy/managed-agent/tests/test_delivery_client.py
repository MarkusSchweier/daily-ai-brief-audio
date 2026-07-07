"""Unit tests for deploy/managed-agent/pipeline/delivery_client.py -- the
MicroVM-side API client for the decoupled delivery boundary (ADR-0015 D3/D4/D8).

A fake HTTP transport is injected into the pure functions (`read_recent_briefs`,
`trigger_and_poll`) so every branch is exercised with no real network. delivery_client
has no module-level AWS/network calls, so it imports directly (like brief_history)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import delivery_client  # noqa: E402


class FakeHttp:
    """Scriptable transport: each call pops the next (status, payload) from `responses`
    (or raises the next exception if it's an Exception). Records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body=None, timeout=delivery_client.HTTP_TIMEOUT_SECONDS):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _no_sleep(_seconds):
    return None


# ---------------------------------------------------------------------------
# read_recent_briefs -- graceful degradation, writes priors.
# ---------------------------------------------------------------------------


def test_read_recent_briefs_writes_each_prior_to_working_folder(tmp_path):
    http = FakeHttp([(200, {"briefs": [
        {"date": "2026-07-03", "markdown": "# Brief 3"},
        {"date": "2026-07-02", "markdown": "# Brief 2"},
    ]})])

    written = delivery_client.read_recent_briefs("https://api", "tok", str(tmp_path), 3, http=http)

    assert written == 2
    assert (tmp_path / "AI Brief - 2026-07-03.md").read_text() == "# Brief 3"
    assert (tmp_path / "AI Brief - 2026-07-02.md").read_text() == "# Brief 2"
    # The read token rode along on the request, count clamped into the query.
    assert http.calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert "count=3" in http.calls[0]["url"]


def test_read_recent_briefs_empty_result_writes_nothing_and_returns_zero(tmp_path):
    http = FakeHttp([(200, {"briefs": []})])
    assert delivery_client.read_recent_briefs("https://api", "tok", str(tmp_path), 3, http=http) == 0
    assert list(tmp_path.iterdir()) == []


def test_read_recent_briefs_degrades_on_non_200(tmp_path):
    http = FakeHttp([(503, {"error": "unavailable"})])
    assert delivery_client.read_recent_briefs("https://api", "tok", str(tmp_path), 3, http=http) == 0


def test_read_recent_briefs_degrades_on_transport_error(tmp_path):
    http = FakeHttp([ConnectionError("dns fail")])
    # Must NOT raise -- a priors-read failure can never abort the run.
    assert delivery_client.read_recent_briefs("https://api", "tok", str(tmp_path), 3, http=http) == 0


def test_read_recent_briefs_skips_when_unconfigured(tmp_path):
    assert delivery_client.read_recent_briefs("", "tok", str(tmp_path), 3) == 0
    assert delivery_client.read_recent_briefs("https://api", "", str(tmp_path), 3) == 0


# ---------------------------------------------------------------------------
# trigger_and_poll -- success + every loud-failure path (ADR-0015 D8).
# ---------------------------------------------------------------------------

_PAYLOAD = {"contractVersion": 2, "brief_markdown": "# B", "listening_script": "s"}


def test_trigger_and_poll_success_returns_summary():
    http = FakeHttp([
        (202, {"deliveryId": "d1", "status": "pending"}),
        (200, {"status": "pending"}),
        (200, {"status": "succeeded", "summary": {"sent_count": 1, "audio_ok": True}}),
    ])

    summary = delivery_client.trigger_and_poll("https://api", "bearer", _PAYLOAD, http=http, sleep=_no_sleep)

    assert summary == {"sent_count": 1, "audio_ok": True}
    assert http.calls[0]["method"] == "POST" and http.calls[0]["url"].endswith("/deliver")
    assert http.calls[1]["url"].endswith("/deliver/d1")


def test_trigger_and_poll_raises_on_non_202_trigger():
    http = FakeHttp([(400, {"error": "bad"})])
    with pytest.raises(delivery_client.DeliveryError, match="DELIVERY_TRIGGER_FAILED"):
        delivery_client.trigger_and_poll("https://api", "bearer", _PAYLOAD, http=http, sleep=_no_sleep)


def test_trigger_and_poll_raises_on_trigger_transport_error():
    http = FakeHttp([ConnectionError("refused")])
    with pytest.raises(delivery_client.DeliveryError, match="transport error"):
        delivery_client.trigger_and_poll("https://api", "bearer", _PAYLOAD, http=http, sleep=_no_sleep)


def test_trigger_and_poll_raises_on_terminal_failed():
    http = FakeHttp([
        (202, {"deliveryId": "d2", "status": "pending"}),
        (200, {"status": "failed", "error": "delivery_failed"}),
    ])
    with pytest.raises(delivery_client.DeliveryError, match="DELIVERY_FAILED"):
        delivery_client.trigger_and_poll("https://api", "bearer", _PAYLOAD, http=http, sleep=_no_sleep)


def test_trigger_and_poll_raises_on_timeout():
    # Always pending -> the monotonic deadline is immediately in the past (timeout=0).
    http = FakeHttp([(202, {"deliveryId": "d3", "status": "pending"})] + [(200, {"status": "pending"})] * 5)
    with pytest.raises(delivery_client.DeliveryError, match="DELIVERY_POLL_TIMEOUT"):
        delivery_client.trigger_and_poll(
            "https://api", "bearer", _PAYLOAD, http=http, poll_timeout=0, sleep=_no_sleep
        )


def test_trigger_and_poll_retries_transient_poll_errors_then_succeeds():
    http = FakeHttp([
        (202, {"deliveryId": "d4", "status": "pending"}),
        TimeoutError("transient"),
        (200, {"status": "succeeded", "summary": {"ok": True}}),
    ])
    summary = delivery_client.trigger_and_poll("https://api", "bearer", _PAYLOAD, http=http, sleep=_no_sleep)
    assert summary == {"ok": True}


def test_trigger_and_poll_handles_idempotent_replay():
    http = FakeHttp([
        (202, {"deliveryId": "d5", "status": "pending", "idempotentReplay": True}),
        (200, {"status": "succeeded", "summary": {}}),
    ])
    summary = delivery_client.trigger_and_poll("https://api", "bearer", _PAYLOAD, http=http, sleep=_no_sleep)
    assert summary == {}


def test_trigger_and_poll_raises_when_unconfigured():
    with pytest.raises(delivery_client.DeliveryError):
        delivery_client.trigger_and_poll("", "bearer", _PAYLOAD)
    with pytest.raises(delivery_client.DeliveryError):
        delivery_client.trigger_and_poll("https://api", "", _PAYLOAD)


# ---------------------------------------------------------------------------
# build_send_payload -- required vs additive artifacts, fan-out gate.
# ---------------------------------------------------------------------------


def test_build_send_payload_reads_all_four_artifacts(tmp_path, monkeypatch):
    (tmp_path / "b.md").write_text("# Brief")
    (tmp_path / "s.txt").write_text("script")
    (tmp_path / "c.json").write_text('{"c": 1}')
    (tmp_path / "u.json").write_text('{"u": 2}')
    monkeypatch.setenv("BRIEF_MARKDOWN_PATH", str(tmp_path / "b.md"))
    monkeypatch.setenv("LISTENING_SCRIPT_PATH", str(tmp_path / "s.txt"))
    monkeypatch.setenv("CANDIDATES_PATH", str(tmp_path / "c.json"))
    monkeypatch.setenv("SOURCE_USAGE_PATH", str(tmp_path / "u.json"))
    monkeypatch.setenv("EMAIL_SUBJECT", "Subj")
    monkeypatch.setenv("BRIEF_DATE", "2026-07-06")
    monkeypatch.setenv("ENABLE_SUBSCRIBER_FANOUT", "1")

    payload = delivery_client.build_send_payload()

    assert payload["contractVersion"] == 2
    assert payload["brief_markdown"] == "# Brief"
    assert payload["candidates"] == '{"c": 1}'
    assert payload["source_usage"] == '{"u": 2}'
    assert payload["metadata"]["enable_subscriber_fanout"] is True
    assert payload["metadata"]["idempotency_key"] == "2026-07-06"


def test_build_send_payload_additive_artifacts_optional(tmp_path, monkeypatch):
    (tmp_path / "b.md").write_text("# Brief")
    (tmp_path / "s.txt").write_text("script")
    monkeypatch.setenv("BRIEF_MARKDOWN_PATH", str(tmp_path / "b.md"))
    monkeypatch.setenv("LISTENING_SCRIPT_PATH", str(tmp_path / "s.txt"))
    monkeypatch.delenv("CANDIDATES_PATH", raising=False)
    monkeypatch.delenv("SOURCE_USAGE_PATH", raising=False)
    monkeypatch.setenv("BRIEF_DATE", "2026-07-06")
    monkeypatch.delenv("ENABLE_SUBSCRIBER_FANOUT", raising=False)

    payload = delivery_client.build_send_payload()

    assert payload["candidates"] is None
    assert payload["source_usage"] is None
    # Fan-out defaults OFF when the env var is absent.
    assert payload["metadata"]["enable_subscriber_fanout"] is False


def test_build_send_payload_missing_required_artifact_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIEF_MARKDOWN_PATH", raising=False)
    monkeypatch.setenv("LISTENING_SCRIPT_PATH", str(tmp_path / "s.txt"))
    with pytest.raises(delivery_client.DeliveryError, match="BRIEF_MARKDOWN_PATH is required"):
        delivery_client.build_send_payload()
