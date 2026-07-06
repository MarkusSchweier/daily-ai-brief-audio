"""Unit tests for `delivery_core.synthesize_audio()` -- the refactored-into-a-
callable port of `audio_email.py`'s top-level Polly try/except block
(:168-198). Same fail-safe: never raises, degrades to `audio_ok=False` on ANY
failure (CLAUDE.md: "never lose the brief over an audio/email glitch").
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


class _FakeS3ClientDownloads:
    """Writes deterministic fake MP3 bytes to the requested local path on
    download_file(), so the caller can read them back."""

    def __init__(self, content: bytes):
        self._content = content
        self.downloaded = []

    def download_file(self, bucket, key, local_path):
        self.downloaded.append((bucket, key, local_path))
        with open(local_path, "wb") as f:
            f.write(self._content)


def test_synthesize_audio_success_returns_audio_ok_true_and_mp3_bytes(tmp_path):
    output_uri = f"https://s3.amazonaws.com/{delivery_core.BUCKET}/audio/task-123.mp3"
    polly_client = _FakePollyClientSuccess(output_uri)
    s3_client = _FakeS3ClientDownloads(b"fake-mp3-content")
    mp3_out_path = str(tmp_path / "brief.mp3")

    audio_ok, audio_s3_key, mp3_bytes = delivery_core.synthesize_audio(polly_client, s3_client, "script text", mp3_out_path)

    assert audio_ok is True
    assert audio_s3_key == "audio/task-123.mp3"
    assert mp3_bytes == b"fake-mp3-content"
    assert s3_client.downloaded == [(delivery_core.BUCKET, "audio/task-123.mp3", mp3_out_path)]


def test_synthesize_audio_uses_output_uri_derived_key_never_a_hand_built_one(tmp_path):
    """CLAUDE.md invariant: 'use OutputUri, never build the S3 key'. This proves
    the derived key comes from parsing OutputUri, not from any
    task-id-based construction -- by using a deliberately unusual OutputUri
    shape and confirming the exact same path segment is extracted."""
    output_uri = f"https://s3.amazonaws.com/{delivery_core.BUCKET}/audio/some-totally-different-name.mp3"
    polly_client = _FakePollyClientSuccess(output_uri)
    s3_client = _FakeS3ClientDownloads(b"content")
    mp3_out_path = str(tmp_path / "out.mp3")

    _, audio_s3_key, _ = delivery_core.synthesize_audio(polly_client, s3_client, "script", mp3_out_path)

    assert audio_s3_key == "audio/some-totally-different-name.mp3"


def test_synthesize_audio_task_failure_returns_audio_ok_false_never_raises(tmp_path):
    polly_client = _FakePollyClientFailed()
    s3_client = _FakeS3ClientDownloads(b"unused")
    mp3_out_path = str(tmp_path / "brief.mp3")

    audio_ok, audio_s3_key, mp3_bytes = delivery_core.synthesize_audio(polly_client, s3_client, "script", mp3_out_path)

    assert audio_ok is False
    assert audio_s3_key is None
    assert mp3_bytes is None


def test_synthesize_audio_start_failure_returns_audio_ok_false_never_raises(tmp_path):
    polly_client = _FakePollyClientRaisesOnStart()
    s3_client = _FakeS3ClientDownloads(b"unused")
    mp3_out_path = str(tmp_path / "brief.mp3")

    # Must not raise.
    audio_ok, audio_s3_key, mp3_bytes = delivery_core.synthesize_audio(polly_client, s3_client, "script", mp3_out_path)

    assert audio_ok is False
    assert audio_s3_key is None
    assert mp3_bytes is None


def test_synthesize_audio_polling_timeout_returns_audio_ok_false(monkeypatch, tmp_path):
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
    s3_client = _FakeS3ClientDownloads(b"unused")
    mp3_out_path = str(tmp_path / "brief.mp3")

    start = time.time()
    audio_ok, audio_s3_key, mp3_bytes = delivery_core.synthesize_audio(polly_client, s3_client, "script", mp3_out_path)
    elapsed = time.time() - start

    assert audio_ok is False
    assert audio_s3_key is None
    assert mp3_bytes is None
    assert elapsed < 5  # sanity: didn't actually wait anywhere near the real 300s default


def test_synthesize_audio_drops_attachment_bytes_when_oversized_but_keeps_ok_true(tmp_path, monkeypatch):
    """Mirrors audio_email.py:196-198: an oversized MP3 is dropped from the EMAIL
    attachment (mp3_bytes=None) but the underlying synthesis itself still
    succeeded (audio_ok stays True, and a real audio_s3_key is still returned --
    the S3 object exists and is still a valid archive pointer, only the email
    attachment is skipped to avoid an SES raw-message-size rejection)."""
    monkeypatch.setattr(delivery_core, "MAX_AUDIO_ATTACHMENT_BYTES", 10)  # tiny, forces the oversized path

    output_uri = f"https://s3.amazonaws.com/{delivery_core.BUCKET}/audio/big-file.mp3"
    polly_client = _FakePollyClientSuccess(output_uri)
    s3_client = _FakeS3ClientDownloads(b"this content is longer than ten bytes")
    mp3_out_path = str(tmp_path / "brief.mp3")

    audio_ok, audio_s3_key, mp3_bytes = delivery_core.synthesize_audio(polly_client, s3_client, "script", mp3_out_path)

    assert audio_ok is True
    assert audio_s3_key == "audio/big-file.mp3"
    assert mp3_bytes is None
