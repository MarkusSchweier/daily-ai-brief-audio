"""Unit tests for deploy/managed-agent/pipeline/brief_history.py.

Covers docs/adr/0005's core contract: read the single most recent PRIOR brief by date
(not literal date arithmetic), degrade gracefully to None when nothing exists yet or a
listing/read fails, and archive today's brief as a self-contained dated folder.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import brief_history  # noqa: E402


def _put_brief(s3_client, bucket, date, markdown):
    s3_client.put_object(Bucket=bucket, Key=f"briefs/{date}/brief.md", Body=markdown.encode("utf-8"))


def test_first_ever_run_has_no_prior_brief(briefs_bucket):
    result = brief_history.read_most_recent_prior_brief(briefs_bucket, today="2026-07-03")
    assert result is None


def test_reads_the_single_most_recent_prior_brief(briefs_bucket):
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-01", "Wednesday's brief")
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-02", "Thursday's brief")

    result = brief_history.read_most_recent_prior_brief(briefs_bucket, today="2026-07-03")

    assert result is not None
    assert result.date == "2026-07-02"
    assert result.markdown == "Thursday's brief"


def test_monday_after_a_weekend_reads_friday_not_saturday_or_sunday(briefs_bucket):
    # Friday 2026-06-26 ran; the weekend had no runs; today is Monday 2026-06-29.
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-06-26", "Friday's brief")

    result = brief_history.read_most_recent_prior_brief(briefs_bucket, today="2026-06-29")

    assert result is not None
    assert result.date == "2026-06-26"


def test_missed_run_reads_the_last_brief_that_actually_ran(briefs_bucket):
    # A run was missed two days ago; today's read must still find the last real one.
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-06-29", "Monday's brief")
    # (Tuesday 2026-06-30 was missed — no object written.)

    result = brief_history.read_most_recent_prior_brief(briefs_bucket, today="2026-07-01")

    assert result is not None
    assert result.date == "2026-06-29"


def test_never_reads_todays_own_or_a_future_dated_object(briefs_bucket):
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-03", "Today's own brief")
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-04", "A future brief (should not happen, but be safe)")

    result = brief_history.read_most_recent_prior_brief(briefs_bucket, today="2026-07-03")

    assert result is None


def test_listing_failure_degrades_to_none_instead_of_raising():
    class RaisingS3Client:
        def get_paginator(self, name):
            raise RuntimeError("simulated S3 outage")

    result = brief_history.read_most_recent_prior_brief(RaisingS3Client(), today="2026-07-03")

    assert result is None


def test_read_failure_after_a_successful_listing_degrades_to_none(briefs_bucket):
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-02", "Thursday's brief")

    class DeletesObjectBetweenListAndGet:
        """Simulates the dated folder existing in a listing but the object read failing."""

        def __init__(self, inner):
            self._inner = inner

        def get_paginator(self, name):
            return self._inner.get_paginator(name)

        def get_object(self, **kwargs):
            raise RuntimeError("simulated transient read failure")

    result = brief_history.read_most_recent_prior_brief(
        DeletesObjectBetweenListAndGet(briefs_bucket), today="2026-07-03"
    )

    assert result is None


def test_archive_writes_markdown_html_and_script_under_the_dated_prefix(briefs_bucket):
    brief_history.archive_todays_brief(
        briefs_bucket,
        "2026-07-03",
        markdown="# Today's brief",
        html="<h1>Today's brief</h1>",
        listening_script="Your AI brief for today.",
    )

    md = briefs_bucket.get_object(Bucket=brief_history.BUCKET, Key="briefs/2026-07-03/brief.md")
    html = briefs_bucket.get_object(Bucket=brief_history.BUCKET, Key="briefs/2026-07-03/brief.html")
    script = briefs_bucket.get_object(Bucket=brief_history.BUCKET, Key="briefs/2026-07-03/listening-script.txt")

    assert md["Body"].read().decode("utf-8") == "# Today's brief"
    assert html["Body"].read().decode("utf-8") == "<h1>Today's brief</h1>"
    assert script["Body"].read().decode("utf-8") == "Your AI brief for today."


def test_archive_markdown_only_omits_html_and_script_objects(briefs_bucket):
    brief_history.archive_todays_brief(briefs_bucket, "2026-07-03", markdown="# Today's brief")

    md = briefs_bucket.get_object(Bucket=brief_history.BUCKET, Key="briefs/2026-07-03/brief.md")
    assert md["Body"].read().decode("utf-8") == "# Today's brief"

    import botocore.exceptions
    import pytest

    with pytest.raises(botocore.exceptions.ClientError):
        briefs_bucket.get_object(Bucket=brief_history.BUCKET, Key="briefs/2026-07-03/brief.html")


def test_archive_failure_is_logged_not_raised(capsys):
    class RaisingS3Client:
        def put_object(self, **kwargs):
            raise RuntimeError("simulated S3 outage")

    # Must not raise — archiving is best-effort and must never take down the run
    # after the send has already succeeded.
    brief_history.archive_todays_brief(RaisingS3Client(), "2026-07-03", markdown="# Today's brief")

    captured = capsys.readouterr()
    assert "BRIEF_ARCHIVE_FAILED" in captured.out


def test_next_day_reads_back_what_was_just_archived(briefs_bucket):
    brief_history.archive_todays_brief(briefs_bucket, "2026-07-03", markdown="# Wednesday's brief")

    result = brief_history.read_most_recent_prior_brief(briefs_bucket, today="2026-07-04")

    assert result is not None
    assert result.date == "2026-07-03"
    assert result.markdown == "# Wednesday's brief"
