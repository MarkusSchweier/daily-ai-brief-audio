"""Unit tests for `delivery_core.synthesize_audio()` -- the refactored-into-a-
callable port of `audio_email.py`'s top-level Polly try/except block
(:168-198). Same fail-safe: never raises, degrades to `audio_ok=False` on ANY
failure (CLAUDE.md: "never lose the brief over an audio/email glitch").

ADR-0017 D8 (audio-as-link delivery): this function no longer downloads the MP3
at all -- with the attachment gone, nothing needs the bytes -- so it takes no
`s3_client`/`mp3_out_path` and returns only `(audio_ok, audio_s3_key)`. The
`HeadObject`-based expiry read that replaces the download is
`delivery_core.resolve_audio_metadata()`, tested separately below.
"""

from __future__ import annotations

import time

import pytest

import delivery_core


class _FakePollyClientSuccess:
    """Synthesis succeeds on the first poll -- exercises the OutputUri-derived key
    path (CLAUDE.md: "use OutputUri, never build the S3 key")."""

    def __init__(self, output_uri: str):
        self._output_uri = output_uri

    def start_speech_synthesis_task(self, **kwargs):
        return {"SynthesisTask": {"TaskId": "task-123"}}

    def get_speech_synthesis_task(self, TaskId):
        assert TaskId == "task-123"
        return {"SynthesisTask": {"TaskStatus": "completed", "OutputUri": self._output_uri}}


class _FakePollyClientFailed:
    def start_speech_synthesis_task(self, **kwargs):
        return {"SynthesisTask": {"TaskId": "task-456"}}

    def get_speech_synthesis_task(self, TaskId):
        return {"SynthesisTask": {"TaskStatus": "failed", "TaskStatusReason": "simulated Polly failure"}}


class _FakePollyClientRaisesOnStart:
    def start_speech_synthesis_task(self, **kwargs):
        raise RuntimeError("simulated Polly outage on start")


def test_synthesize_audio_success_returns_audio_ok_true_and_the_output_uri_derived_key():
    output_uri = f"https://s3.amazonaws.com/{delivery_core.BUCKET}/audio/task-123.mp3"
    polly_client = _FakePollyClientSuccess(output_uri)

    audio_ok, audio_s3_key = delivery_core.synthesize_audio(polly_client, "script text")

    assert audio_ok is True
    assert audio_s3_key == "audio/task-123.mp3"


def test_synthesize_audio_uses_output_uri_derived_key_never_a_hand_built_one():
    """CLAUDE.md invariant: 'use OutputUri, never build the S3 key'. This proves
    the derived key comes from parsing OutputUri, not from any
    task-id-based construction -- by using a deliberately unusual OutputUri
    shape and confirming the exact same path segment is extracted."""
    output_uri = f"https://s3.amazonaws.com/{delivery_core.BUCKET}/audio/some-totally-different-name.mp3"
    polly_client = _FakePollyClientSuccess(output_uri)

    _, audio_s3_key = delivery_core.synthesize_audio(polly_client, "script")

    assert audio_s3_key == "audio/some-totally-different-name.mp3"


def test_synthesize_audio_task_failure_returns_audio_ok_false_never_raises():
    polly_client = _FakePollyClientFailed()

    audio_ok, audio_s3_key = delivery_core.synthesize_audio(polly_client, "script")

    assert audio_ok is False
    assert audio_s3_key is None


def test_synthesize_audio_start_failure_returns_audio_ok_false_never_raises():
    polly_client = _FakePollyClientRaisesOnStart()

    # Must not raise.
    audio_ok, audio_s3_key = delivery_core.synthesize_audio(polly_client, "script")

    assert audio_ok is False
    assert audio_s3_key is None


def test_synthesize_audio_polling_timeout_returns_audio_ok_false(monkeypatch):
    """A synthesis task stuck 'in progress' forever must time out rather than hang
    the Lambda past its own timeout budget -- verbatim fail-safe from
    audio_email.py's `deadline = time.time() + 300` / `TimeoutError`."""

    class _NeverCompletesPollyClient:
        def start_speech_synthesis_task(self, **kwargs):
            return {"SynthesisTask": {"TaskId": "task-stuck"}}

        def get_speech_synthesis_task(self, TaskId):
            return {"SynthesisTask": {"TaskStatus": "inProgress"}}

    # Speed up the test: make the deadline check trip almost immediately by
    # monkeypatching the timeout constant to a tiny value, and the poll interval
    # to something sub-second so the test doesn't actually sleep for real.
    monkeypatch.setattr(delivery_core, "POLLY_SYNTHESIS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(delivery_core, "POLLY_POLL_INTERVAL_SECONDS", 0.01)

    polly_client = _NeverCompletesPollyClient()

    start = time.time()
    audio_ok, audio_s3_key = delivery_core.synthesize_audio(polly_client, "script")
    elapsed = time.time() - start

    assert audio_ok is False
    assert audio_s3_key is None
    assert elapsed < 5  # sanity: didn't actually wait anywhere near the real 300s default


# ---------------------------------------------------------------------------
# resolve_audio_metadata() -- the HeadObject-based expiry read that replaces the
# download (ADR-0017 D1/D8).
# ---------------------------------------------------------------------------


class _FakeS3ClientHeadObject:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.head_object_calls = []

    def head_object(self, Bucket, Key):
        self.head_object_calls.append((Bucket, Key))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def test_resolve_audio_metadata_returns_available_true_with_expiration_header_on_success():
    s3_client = _FakeS3ClientHeadObject(
        response={"Expiration": 'expiry-date="Tue, 04 Aug 2026 00:00:00 GMT", rule-id="expire-audio-7-days"'}
    )

    audio_available, expiration_header = delivery_core.resolve_audio_metadata(s3_client, "audio/.abc.mp3")

    assert audio_available is True
    assert expiration_header == 'expiry-date="Tue, 04 Aug 2026 00:00:00 GMT", rule-id="expire-audio-7-days"'
    assert s3_client.head_object_calls == [(delivery_core.BUCKET, "audio/.abc.mp3")]


def test_resolve_audio_metadata_returns_available_true_expiration_none_when_header_missing():
    """200 but no Expiration header at all -- D1's generic-note case."""
    s3_client = _FakeS3ClientHeadObject(response={})

    audio_available, expiration_header = delivery_core.resolve_audio_metadata(s3_client, "audio/.abc.mp3")

    assert audio_available is True
    assert expiration_header is None


def test_resolve_audio_metadata_returns_available_false_on_head_object_error_never_raises():
    """404/NoSuchKey/any HeadObject failure -- D1: showing a link would be a
    known-broken link."""
    s3_client = _FakeS3ClientHeadObject(raise_exc=RuntimeError("simulated NoSuchKey"))

    audio_available, expiration_header = delivery_core.resolve_audio_metadata(s3_client, "audio/.abc.mp3")

    assert audio_available is False
    assert expiration_header is None


def test_resolve_audio_metadata_head_object_failure_log_line_does_not_leak_the_full_audio_key(capsys):
    """Review finding (Low): the full audio_key is a still-live public URL
    fragment -- the AUDIO_LINK_HEAD_OBJECT_FAILED log line must carry a
    redacted form, never the real key, while staying greppable by name."""
    real_key = "audio/.00000000-0000-4000-8000-123456789abc.mp3"
    s3_client = _FakeS3ClientHeadObject(raise_exc=RuntimeError("simulated NoSuchKey"))

    delivery_core.resolve_audio_metadata(s3_client, real_key)

    printed = capsys.readouterr().out
    assert "AUDIO_LINK_HEAD_OBJECT_FAILED" in printed
    assert real_key not in printed
    assert "00000000-0000-4000-8000-123456789abc" not in printed  # the redacted middle segment


# ---------------------------------------------------------------------------
# _redact_audio_key() -- the log-safety helper itself.
# ---------------------------------------------------------------------------


def test_redact_audio_key_never_raises_on_non_string_input():
    # A logging helper must never itself become a new failure mode.
    assert delivery_core._redact_audio_key(12345) == "<unloggable audio key>"


def test_redact_audio_key_keeps_a_short_prefix_and_the_length_masks_the_rest():
    key = "audio/.00000000-0000-4000-8000-123456789abc.mp3"

    redacted = delivery_core._redact_audio_key(key)

    assert redacted.startswith(key[:12])
    assert str(len(key)) in redacted
    assert key not in redacted
    assert "123456789abc" not in redacted  # the tail of the real task id must not leak either


def test_resolve_audio_metadata_returns_available_false_when_audio_key_is_none():
    """AC-11: synthesis failed, so there is no audio_key at all -- resolves to
    no-audio without ever calling HeadObject."""
    s3_client = _FakeS3ClientHeadObject()

    audio_available, expiration_header = delivery_core.resolve_audio_metadata(s3_client, None)

    assert audio_available is False
    assert expiration_header is None
    assert s3_client.head_object_calls == []


@pytest.mark.parametrize("empty_key", [None, ""])
def test_resolve_audio_metadata_never_calls_head_object_for_a_falsy_key(empty_key):
    s3_client = _FakeS3ClientHeadObject()

    delivery_core.resolve_audio_metadata(s3_client, empty_key)

    assert s3_client.head_object_calls == []
