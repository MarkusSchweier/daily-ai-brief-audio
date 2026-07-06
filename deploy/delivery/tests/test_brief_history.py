"""Unit tests for deploy/delivery/functions/deliver/brief_history.py.

This file is hand-duplicated from deploy/managed-agent/pipeline/brief_history.py
(see that module's docstring) into this independent Lambda deployment unit, so its
test coverage is likewise hand-duplicated from
deploy/managed-agent/tests/test_brief_history.py -- specifically the
archive_candidates_file()/archive_source_usage_file() coverage, since this app's
brief_history.py copy has no history-reading responsibilities of its own beyond what
those two archival functions provide (the delivery Lambda always receives the brief
markdown/listening-script directly in its request body, not by reading it back from
S3 -- see docs/prd/agent-system-redesign.md FR-2).
"""

import json
import os

import pytest

import brief_history


# --- archive_candidates_file (PRD docs/prd/eval-harness.md FR-4/AC-5, ADR-0013 §D) -----


def test_candidates_file_missing_is_handled_gracefully(briefs_bucket, tmp_path):
    """AC-5: an older run, or a run whose skill version doesn't yet emit
    candidates.json, must not raise and must not archive anything."""
    archived = brief_history.archive_candidates_file(
        briefs_bucket, "2026-07-06", working_folder=str(tmp_path)
    )

    assert archived is False

    import botocore.exceptions

    with pytest.raises(botocore.exceptions.ClientError):
        briefs_bucket.get_object(
            Bucket=brief_history.BUCKET,
            Key=f"briefs/2026-07-06/{brief_history.CANDIDATES_FILENAME}",
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
        briefs_bucket, "2026-07-06", working_folder=str(tmp_path)
    )

    assert archived is True
    obj = briefs_bucket.get_object(
        Bucket=brief_history.BUCKET,
        Key=f"briefs/2026-07-06/{brief_history.CANDIDATES_FILENAME}",
    )
    assert obj["Body"].read().decode("utf-8") == payload


def test_candidates_file_write_failure_is_logged_not_raised(tmp_path, capsys):
    (tmp_path / brief_history.CANDIDATES_FILENAME).write_text("[]", encoding="utf-8")

    class RaisingS3Client:
        def put_object(self, **kwargs):
            raise RuntimeError("simulated S3 outage")

    archived = brief_history.archive_candidates_file(
        RaisingS3Client(), "2026-07-06", working_folder=str(tmp_path)
    )

    assert archived is False
    captured = capsys.readouterr()
    assert "BRIEF_ARCHIVE_FAILED" in captured.out


# --- archive_source_usage_file (PRD docs/prd/agent-system-redesign.md FR-8a, ADR-0014;
# realizes GitHub issue #28) -- a direct sibling of archive_candidates_file above, same
# coverage shape --------------------------------------------------------------------


def test_source_usage_file_missing_is_handled_gracefully(briefs_bucket, tmp_path):
    """AC-8a: an older run, a run before this feature shipped, or a run whose skill
    version doesn't yet emit source-usage.json, must not raise and must not archive
    anything."""
    archived = brief_history.archive_source_usage_file(
        briefs_bucket, "2026-07-06", working_folder=str(tmp_path)
    )

    assert archived is False

    import botocore.exceptions

    with pytest.raises(botocore.exceptions.ClientError):
        briefs_bucket.get_object(
            Bucket=brief_history.BUCKET,
            Key=f"briefs/2026-07-06/{brief_history.SOURCE_USAGE_FILENAME}",
        )


def test_source_usage_file_present_is_archived_correctly(briefs_bucket, tmp_path):
    """AC-8a: a run whose skill wrote source-usage.json gets it archived verbatim
    alongside the rest of that day's artifacts."""
    payload = json.dumps(
        [
            {"source": "Anthropic", "tier": 1, "featured": True},
            {"source": "TechCrunch — AI", "tier": 4, "featured": False},
        ]
    )
    (tmp_path / brief_history.SOURCE_USAGE_FILENAME).write_text(payload, encoding="utf-8")

    archived = brief_history.archive_source_usage_file(
        briefs_bucket, "2026-07-06", working_folder=str(tmp_path)
    )

    assert archived is True
    obj = briefs_bucket.get_object(
        Bucket=brief_history.BUCKET,
        Key=f"briefs/2026-07-06/{brief_history.SOURCE_USAGE_FILENAME}",
    )
    assert obj["Body"].read().decode("utf-8") == payload


def test_source_usage_file_read_failure_is_logged_not_raised(tmp_path, capsys):
    """A read failure (e.g. a permissions glitch) must degrade the same way a missing
    file does -- logged, never raised, never gating the run."""
    source_usage_path = tmp_path / brief_history.SOURCE_USAGE_FILENAME
    source_usage_path.write_text("[]", encoding="utf-8")
    source_usage_path.chmod(0o000)

    try:
        if os.access(source_usage_path, os.R_OK):
            pytest.skip("test process can read a chmod 000 file (e.g. running as root) -- cannot simulate a read failure this way")

        archived = brief_history.archive_source_usage_file(
            "unused-s3-client", "2026-07-06", working_folder=str(tmp_path)
        )

        assert archived is False
        captured = capsys.readouterr()
        assert "SOURCE_USAGE_ARCHIVE_READ_FAILED" in captured.out
    finally:
        source_usage_path.chmod(0o644)


def test_source_usage_file_write_failure_is_logged_not_raised(tmp_path, capsys):
    (tmp_path / brief_history.SOURCE_USAGE_FILENAME).write_text("[]", encoding="utf-8")

    class RaisingS3Client:
        def put_object(self, **kwargs):
            raise RuntimeError("simulated S3 outage")

    archived = brief_history.archive_source_usage_file(
        RaisingS3Client(), "2026-07-06", working_folder=str(tmp_path)
    )

    assert archived is False
    captured = capsys.readouterr()
    assert "BRIEF_ARCHIVE_FAILED" in captured.out
