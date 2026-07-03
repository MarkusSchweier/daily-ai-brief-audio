"""Unit tests for latest_brief.py -- the welcome-send Lambda's read-only "most recent
archived brief" resolver. Covers PRD instant-welcome-brief.md AC-3.
"""

import json

import latest_brief


def _put_brief(s3_client, date: str, html: str, audio_key: str | None = None):
    s3_client.put_object(
        Bucket=latest_brief.BUCKET, Key=f"briefs/{date}/brief.html", Body=html.encode("utf-8")
    )
    if audio_key is not None:
        s3_client.put_object(
            Bucket=latest_brief.BUCKET,
            Key=f"briefs/{date}/{latest_brief.AUDIO_POINTER_FILENAME}",
            Body=json.dumps({"audio_key": audio_key}).encode("utf-8"),
        )


def test_empty_store_returns_not_found_without_raising(briefs_bucket):
    result = latest_brief.resolve_latest_brief(briefs_bucket)
    assert result.found is False
    assert result.date is None
    assert result.html is None
    assert result.audio_key is None


def test_single_brief_with_pointer_is_resolved(briefs_bucket):
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today</h1>", audio_key="audio/.today123.mp3")

    result = latest_brief.resolve_latest_brief(briefs_bucket)

    assert result.found is True
    assert result.date == "2026-07-03"
    assert result.html == "<h1>Today</h1>"
    assert result.audio_key == "audio/.today123.mp3"


def test_brief_without_a_pointer_resolves_audio_key_to_none(briefs_bucket):
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today</h1>")  # no audio_key given

    result = latest_brief.resolve_latest_brief(briefs_bucket)

    assert result.found is True
    assert result.html == "<h1>Today</h1>"
    assert result.audio_key is None


def test_multiple_briefs_resolves_the_most_recent_one(briefs_bucket):
    _put_brief(briefs_bucket, "2026-06-30", "<h1>Tuesday</h1>", audio_key="audio/.tue.mp3")
    _put_brief(briefs_bucket, "2026-07-02", "<h1>Thursday</h1>", audio_key="audio/.thu.mp3")
    _put_brief(briefs_bucket, "2026-07-01", "<h1>Wednesday</h1>", audio_key="audio/.wed.mp3")

    result = latest_brief.resolve_latest_brief(briefs_bucket)

    assert result.date == "2026-07-02"
    assert result.html == "<h1>Thursday</h1>"
    assert result.audio_key == "audio/.thu.mp3"


def test_listing_failure_degrades_to_not_found_instead_of_raising():
    class RaisingS3Client:
        def get_paginator(self, name):
            raise RuntimeError("simulated S3 outage")

    result = latest_brief.resolve_latest_brief(RaisingS3Client())

    assert result.found is False


def test_html_read_failure_degrades_to_not_found_instead_of_raising(briefs_bucket):
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today</h1>")
    real_get_object = briefs_bucket.get_object

    class FailsOnHtmlRead:
        def get_paginator(self, name):
            return briefs_bucket.get_paginator(name)

        def get_object(self, **kwargs):
            if kwargs.get("Key", "").endswith("brief.html"):
                raise RuntimeError("simulated transient read failure")
            return real_get_object(**kwargs)

    result = latest_brief.resolve_latest_brief(FailsOnHtmlRead())

    assert result.found is False


def test_does_not_verify_the_pointed_to_mp3_object_actually_exists(briefs_bucket):
    """This resolver's job stops at resolving the pointer's key -- confirming the MP3
    object itself still exists (it may have aged out under the 7-day audio/ lifecycle)
    is the welcome-send handler's job (FR-5/AC-5), not this helper's."""
    _put_brief(briefs_bucket, "2026-07-03", "<h1>Today</h1>", audio_key="audio/.gone-already.mp3")
    # Deliberately never put an object at that audio key.

    result = latest_brief.resolve_latest_brief(briefs_bucket)

    assert result.found is True
    assert result.audio_key == "audio/.gone-already.mp3"
