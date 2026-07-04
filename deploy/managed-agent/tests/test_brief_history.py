"""Unit tests for deploy/managed-agent/pipeline/brief_history.py.

Covers docs/adr/0005's core contract: read the N most recent PRIOR briefs by date
(not literal date arithmetic), degrade gracefully to an empty/partial list when fewer
than N exist or a listing/read fails, and archive today's brief as a self-contained
dated folder.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import brief_history  # noqa: E402


def _put_brief(s3_client, bucket, date, markdown):
    s3_client.put_object(Bucket=bucket, Key=f"briefs/{date}/brief.md", Body=markdown.encode("utf-8"))


def test_first_ever_run_has_no_prior_briefs(briefs_bucket):
    result = brief_history.read_recent_prior_briefs(briefs_bucket, today="2026-07-03")
    assert result == []


def test_reads_up_to_three_most_recent_prior_briefs_most_recent_first(briefs_bucket):
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-06-30", "Tuesday's brief")
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-01", "Wednesday's brief")
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-02", "Thursday's brief")

    result = brief_history.read_recent_prior_briefs(briefs_bucket, today="2026-07-03")

    assert [r.date for r in result] == ["2026-07-02", "2026-07-01", "2026-06-30"]
    assert result[0].markdown == "Thursday's brief"
    assert result[1].markdown == "Wednesday's brief"
    assert result[2].markdown == "Tuesday's brief"


def test_default_count_is_three_even_with_more_history_available(briefs_bucket):
    for date, markdown in [
        ("2026-06-28", "Sunday"),
        ("2026-06-29", "Monday"),
        ("2026-06-30", "Tuesday"),
        ("2026-07-01", "Wednesday"),
        ("2026-07-02", "Thursday"),
    ]:
        _put_brief(briefs_bucket, brief_history.BUCKET, date, markdown)

    result = brief_history.read_recent_prior_briefs(briefs_bucket, today="2026-07-03")

    assert [r.date for r in result] == ["2026-07-02", "2026-07-01", "2026-06-30"]


def test_count_is_overridable(briefs_bucket):
    for date in ["2026-06-30", "2026-07-01", "2026-07-02"]:
        _put_brief(briefs_bucket, brief_history.BUCKET, date, f"brief {date}")

    result = brief_history.read_recent_prior_briefs(briefs_bucket, today="2026-07-03", count=1)

    assert [r.date for r in result] == ["2026-07-02"]


def test_fewer_than_count_available_returns_whatever_exists(briefs_bucket):
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-02", "Thursday's brief")

    result = brief_history.read_recent_prior_briefs(briefs_bucket, today="2026-07-03")

    assert [r.date for r in result] == ["2026-07-02"]


def test_monday_after_a_weekend_reads_friday_not_saturday_or_sunday(briefs_bucket):
    # Friday 2026-06-26 ran; the weekend had no runs; today is Monday 2026-06-29.
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-06-26", "Friday's brief")

    result = brief_history.read_recent_prior_briefs(briefs_bucket, today="2026-06-29")

    assert [r.date for r in result] == ["2026-06-26"]


def test_missed_run_reads_the_last_briefs_that_actually_ran(briefs_bucket):
    # A run was missed two days ago; today's read must still find the real ones.
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-06-28", "Sunday's brief")
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-06-29", "Monday's brief")
    # (Tuesday 2026-06-30 was missed — no object written.)

    result = brief_history.read_recent_prior_briefs(briefs_bucket, today="2026-07-01")

    assert [r.date for r in result] == ["2026-06-29", "2026-06-28"]


def test_never_reads_todays_own_or_a_future_dated_object(briefs_bucket):
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-03", "Today's own brief")
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-04", "A future brief (should not happen, but be safe)")

    result = brief_history.read_recent_prior_briefs(briefs_bucket, today="2026-07-03")

    assert result == []


def test_listing_failure_degrades_to_empty_list_instead_of_raising():
    class RaisingS3Client:
        def get_paginator(self, name):
            raise RuntimeError("simulated S3 outage")

    result = brief_history.read_recent_prior_briefs(RaisingS3Client(), today="2026-07-03")

    assert result == []


def test_read_failure_on_one_date_is_skipped_not_fatal_to_the_others(briefs_bucket):
    """A transient read failure on one of the N dates must not lose the other, readable
    ones — the whole point of fetching several is resilience, not an all-or-nothing read."""
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-01", "Wednesday's brief")
    _put_brief(briefs_bucket, brief_history.BUCKET, "2026-07-02", "Thursday's brief")

    real_get_object = briefs_bucket.get_object

    class FailsOnlyOnOneKey:
        def __init__(self, inner):
            self._inner = inner

        def get_paginator(self, name):
            return self._inner.get_paginator(name)

        def get_object(self, **kwargs):
            if kwargs.get("Key") == "briefs/2026-07-01/brief.md":
                raise RuntimeError("simulated transient read failure")
            return real_get_object(**kwargs)

    result = brief_history.read_recent_prior_briefs(FailsOnlyOnOneKey(briefs_bucket), today="2026-07-03")

    assert [r.date for r in result] == ["2026-07-02"]


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


def test_archive_with_audio_key_writes_a_pointer_object(briefs_bucket):
    """AC-1: a successful audio run's archive includes a durable pointer to that run's
    actual OutputUri-derived key, alongside the existing brief.md/html/script."""
    import json

    brief_history.archive_todays_brief(
        briefs_bucket,
        "2026-07-03",
        markdown="# Today's brief",
        html="<h1>Today's brief</h1>",
        listening_script="Your AI brief for today.",
        audio_key="audio/.abc123.mp3",
    )

    pointer = briefs_bucket.get_object(
        Bucket=brief_history.BUCKET,
        Key=f"briefs/2026-07-03/{brief_history.AUDIO_POINTER_FILENAME}",
    )
    body = json.loads(pointer["Body"].read().decode("utf-8"))
    assert body == {"audio_key": "audio/.abc123.mp3"}


def test_archive_without_audio_key_writes_no_pointer(briefs_bucket):
    """AC-2: an audio-failure day (caller passes audio_key=None, the default) must not
    leave a pointer object behind -- the read helper later treats that as "brief, no
    audio", not a stale/wrong pointer."""
    brief_history.archive_todays_brief(
        briefs_bucket, "2026-07-03", markdown="# Today's brief", html="<h1>Today's brief</h1>"
    )

    import botocore.exceptions
    import pytest

    with pytest.raises(botocore.exceptions.ClientError):
        briefs_bucket.get_object(
            Bucket=brief_history.BUCKET,
            Key=f"briefs/2026-07-03/{brief_history.AUDIO_POINTER_FILENAME}",
        )


def test_pointer_write_failure_is_logged_not_raised(capsys):
    """AC-1: a pointer-write failure must never raise -- best-effort, same as the
    existing brief/html/script writes, and must not be masked by (or mask) their own
    success/failure."""

    class RaisingOnlyOnPointer:
        def put_object(self, **kwargs):
            if kwargs.get("Key", "").endswith(brief_history.AUDIO_POINTER_FILENAME):
                raise RuntimeError("simulated S3 outage on the pointer write")
            return {}

    # Must not raise.
    brief_history.archive_todays_brief(
        RaisingOnlyOnPointer(), "2026-07-03", markdown="# Today's brief", audio_key="audio/.xyz.mp3"
    )

    captured = capsys.readouterr()
    assert "BRIEF_ARCHIVE_FAILED" in captured.out
    assert brief_history.AUDIO_POINTER_FILENAME in captured.out
    # The markdown write (a different key) must have succeeded independently.
    assert "BRIEF_ARCHIVED briefs/2026-07-03/brief.md" in captured.out


def test_next_day_reads_back_what_was_just_archived(briefs_bucket):
    brief_history.archive_todays_brief(briefs_bucket, "2026-07-03", markdown="# Wednesday's brief")

    result = brief_history.read_recent_prior_briefs(briefs_bucket, today="2026-07-04")

    assert [r.date for r in result] == ["2026-07-03"]
    assert result[0].markdown == "# Wednesday's brief"


# --- archive_candidates_file (PRD docs/prd/eval-harness.md FR-4/AC-5, ADR-0013 §D) -----


def test_candidates_file_missing_is_handled_gracefully(briefs_bucket, tmp_path):
    """AC-5 / the developer task's own "missing file handled gracefully" requirement:
    an older run, or a run whose skill version doesn't yet emit candidates.json,
    must not raise and must not archive anything."""
    archived = brief_history.archive_candidates_file(
        briefs_bucket, "2026-07-03", working_folder=str(tmp_path)
    )

    assert archived is False

    import botocore.exceptions
    import pytest as _pytest

    with _pytest.raises(botocore.exceptions.ClientError):
        briefs_bucket.get_object(
            Bucket=brief_history.BUCKET,
            Key=f"briefs/2026-07-03/{brief_history.CANDIDATES_FILENAME}",
        )


def test_candidates_file_present_is_archived_correctly(briefs_bucket, tmp_path):
    """AC-5: a run whose skill wrote candidates.json gets it archived verbatim
    alongside the rest of that day's artifacts."""
    payload = json.dumps(
        [
            {"title": "Story A", "source": "TechCrunch", "disposition": "included"},
            {"title": "Story B", "source": "The Verge", "disposition": "excluded"},
        ]
    )
    (tmp_path / brief_history.CANDIDATES_FILENAME).write_text(payload, encoding="utf-8")

    archived = brief_history.archive_candidates_file(
        briefs_bucket, "2026-07-03", working_folder=str(tmp_path)
    )

    assert archived is True
    obj = briefs_bucket.get_object(
        Bucket=brief_history.BUCKET,
        Key=f"briefs/2026-07-03/{brief_history.CANDIDATES_FILENAME}",
    )
    assert obj["Body"].read().decode("utf-8") == payload


def test_candidates_file_write_failure_is_logged_not_raised(tmp_path, capsys):
    (tmp_path / brief_history.CANDIDATES_FILENAME).write_text("[]", encoding="utf-8")

    class RaisingS3Client:
        def put_object(self, **kwargs):
            raise RuntimeError("simulated S3 outage")

    archived = brief_history.archive_candidates_file(
        RaisingS3Client(), "2026-07-03", working_folder=str(tmp_path)
    )

    assert archived is False
    captured = capsys.readouterr()
    assert "BRIEF_ARCHIVE_FAILED" in captured.out
