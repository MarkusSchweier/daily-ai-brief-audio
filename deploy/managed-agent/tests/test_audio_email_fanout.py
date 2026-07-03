"""Unit tests for deploy/managed-agent/pipeline/audio_email.py's fan-out/failure-isolation
logic — the microVM-adapted port of deploy/audio_email.py.

Covers the same PRD acceptance criteria the live module's tests cover (this is a
faithful port, not a redesign): AC-8 (owner's send always attempted, never gated on
subscriber sends), AC-9 (audio failure still text-only-emails everyone, fail-safe
preserved), AC-11 (one bad subscriber address never blocks the others or the owner) —
plus AC-13 (no credential-file loading; boto3 authenticates via the ambient credential
chain only, standing in for the microVM's IMDSv2-delivered execution role).
"""

from __future__ import annotations

import email
import os


class FakeSesClient:
    """Minimal stand-in for boto3's SES client, with per-recipient failure injection."""

    def __init__(self, failing_recipients=None):
        self.failing_recipients = set(failing_recipients or [])
        self.sent_to = []

    def send_raw_email(self, Source, Destinations, RawMessage):
        recipient = Destinations[0]
        if recipient in self.failing_recipients:
            raise RuntimeError(f"simulated SES failure for {recipient}")
        self.sent_to.append({"source": Source, "recipient": recipient, "raw": RawMessage["Data"]})
        return {"MessageId": f"fake-message-id-{len(self.sent_to)}"}


class FakeDynamoDBPaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class FakeDynamoDBClient:
    """Minimal stand-in for boto3's DynamoDB client Query paginator."""

    def __init__(self, subscriber_items=None, raise_on_query=False):
        self._subscriber_items = subscriber_items or []
        self._raise_on_query = raise_on_query

    def get_paginator(self, operation_name):
        assert operation_name == "query"
        if self._raise_on_query:
            raise RuntimeError("simulated DynamoDB outage")
        return FakeDynamoDBPaginator([{"Items": self._subscriber_items}])


def _ddb_item(email_address, first_name="Test", unsubscribe_token="tok"):
    return {
        "email": {"S": email_address},
        "firstName": {"S": first_name},
        "unsubscribeToken": {"S": unsubscribe_token},
    }


def _html_body_text(raw_mime: str) -> str:
    """Decode the HTML alternative part out of a raw MIME message for content assertions."""
    parsed = email.message_from_string(raw_mime)
    for part in parsed.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            return payload.decode("utf-8")
    return ""


def _plain_body_text(raw_mime: str) -> str:
    """Decode the text/plain part out of a raw MIME message (the confirmation email's
    body) for content assertions."""
    parsed = email.message_from_string(raw_mime)
    for part in parsed.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            return payload.decode("utf-8")
    return ""


def test_no_credential_file_loading_anywhere_in_the_module(audio_email_module):
    """AC-13/ADR-0004: the module must not read AWS_SHARED_CREDENTIALS_FILE or any
    other credential-file mechanism at runtime — the microVM authenticates purely via
    the ambient boto3 credential chain (IMDSv2-delivered execution role in production;
    moto's dummy env-var credentials in this test). Checks the *executable* source
    (comments/strings stripped via tokenize) so the module's own explanatory docstring
    — which necessarily names the mechanism it does NOT use, for contrast with the live
    deploy/audio_email.py — doesn't produce a false positive."""
    import inspect
    import io
    import tokenize

    source = inspect.getsource(audio_email_module)
    code_tokens = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE)
    ]
    code_only = " ".join(code_tokens)

    assert "AWS_SHARED_CREDENTIALS_FILE" not in code_only
    assert "aws_access_key_id" not in code_only.lower()
    assert os.environ.get("AWS_SHARED_CREDENTIALS_FILE") is None


def test_owner_always_sent_with_zero_subscribers(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[])

    sent, failed, sub_sent, sub_failed, query_failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
    )

    assert sent == 1
    assert failed == 0
    assert sub_sent == 0
    assert sub_failed == 0
    assert query_failed is False
    assert len(ses_client.sent_to) == 1
    assert ses_client.sent_to[0]["recipient"] == audio_email_module.RECIP
    assert ses_client.sent_to[0]["source"] == audio_email_module.SENDER


def test_skip_subscriber_fanout_sends_only_the_owner(audio_email_module):
    """The manual-validation-only escape hatch: with skip_subscriber_fanout=True, the
    owner's copy still goes out but the DynamoDB query / subscriber loop never runs at
    all -- proven here by a DynamoDB client that raises if queried, not just one that
    returns zero subscribers (which wouldn't prove the query was skipped)."""

    class RaisesIfQueried:
        def get_paginator(self, name):
            raise AssertionError("subscriber fan-out must not query DynamoDB when skipped")

    ses_client = FakeSesClient()

    sent, failed, sub_sent, sub_failed, query_failed = audio_email_module.send_all(
        ses_client,
        RaisesIfQueried(),
        "Subject",
        "<p>brief</p>",
        None,
        "brief.mp3",
        "brief-subscribers-test",
        skip_subscriber_fanout=True,
    )

    assert sent == 1
    assert failed == 0
    assert sub_sent == 0
    assert sub_failed == 0
    assert query_failed is False
    assert len(ses_client.sent_to) == 1
    assert ses_client.sent_to[0]["recipient"] == audio_email_module.RECIP


def test_owner_and_all_confirmed_subscribers_receive_the_brief(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(
        subscriber_items=[
            _ddb_item("alice@example.com", unsubscribe_token="tok-a"),
            _ddb_item("bob@example.com", unsubscribe_token="tok-b"),
        ]
    )

    sent, failed, sub_sent, sub_failed, query_failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", b"fake-mp3-bytes", "brief.mp3", "brief-subscribers-test"
    )

    assert sent == 3  # owner + 2 subscribers
    assert failed == 0
    assert sub_sent == 2  # subscriber-only, owner excluded
    assert sub_failed == 0
    assert query_failed is False
    recipients = {entry["recipient"] for entry in ses_client.sent_to}
    assert recipients == {audio_email_module.RECIP, "alice@example.com", "bob@example.com"}

    subscriber_sends = [e for e in ses_client.sent_to if e["recipient"] != audio_email_module.RECIP]
    assert all(e["source"] == audio_email_module.SUBSCRIBER_SENDER for e in subscriber_sends)
    alice_raw = next(e["raw"] for e in ses_client.sent_to if e["recipient"] == "alice@example.com")
    assert "tok-a" in _html_body_text(alice_raw)
    bob_raw = next(e["raw"] for e in ses_client.sent_to if e["recipient"] == "bob@example.com")
    assert "tok-b" in _html_body_text(bob_raw)


def test_one_bad_subscriber_does_not_block_others_or_the_owner(audio_email_module):
    ses_client = FakeSesClient(failing_recipients={"broken@example.com"})
    ddb_client = FakeDynamoDBClient(
        subscriber_items=[
            _ddb_item("good1@example.com", unsubscribe_token="tok-1"),
            _ddb_item("broken@example.com", unsubscribe_token="tok-2"),
            _ddb_item("good2@example.com", unsubscribe_token="tok-3"),
        ]
    )

    sent, failed, sub_sent, sub_failed, query_failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", b"fake-mp3-bytes", "brief.mp3", "brief-subscribers-test"
    )

    assert sent == 3  # owner + good1 + good2
    assert failed == 1  # broken@example.com
    assert sub_sent == 2  # good1 + good2, owner excluded
    assert sub_failed == 1  # broken@example.com
    assert query_failed is False
    recipients = {entry["recipient"] for entry in ses_client.sent_to}
    assert recipients == {audio_email_module.RECIP, "good1@example.com", "good2@example.com"}
    assert "broken@example.com" not in recipients


def test_owner_send_failure_does_not_block_subscriber_sends(audio_email_module):
    ses_client = FakeSesClient(failing_recipients={audio_email_module.RECIP})
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("carol@example.com")])

    sent, failed, sub_sent, sub_failed, query_failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
    )

    assert failed == 1  # owner's send failed
    assert sent == 1  # but the subscriber still got theirs
    assert sub_sent == 1  # carol
    assert sub_failed == 0
    assert query_failed is False
    recipients = {entry["recipient"] for entry in ses_client.sent_to}
    assert recipients == {"carol@example.com"}


def test_dynamodb_query_outage_still_lets_owner_send_succeed(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(raise_on_query=True)

    sent, failed, sub_sent, sub_failed, query_failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
    )

    assert sent == 1
    assert failed == 0
    assert sub_sent == 0
    assert sub_failed == 0
    # AC-7: a genuine query failure must be surfaced distinctly, not indistinguishable
    # from a real zero-subscriber day.
    assert query_failed is True
    assert ses_client.sent_to[0]["recipient"] == audio_email_module.RECIP


def test_mp3_bytes_are_reused_verbatim_across_every_recipient(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(
        subscriber_items=[_ddb_item("dave@example.com"), _ddb_item("erin@example.com")]
    )
    mp3_bytes = b"identical-mp3-bytes-for-everyone"

    audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", mp3_bytes, "brief.mp3", "brief-subscribers-test"
    )

    assert len(ses_client.sent_to) == 3
    for entry in ses_client.sent_to:
        parsed = email.message_from_string(entry["raw"])
        attachment_parts = [
            part for part in parsed.walk() if part.get_content_disposition() == "attachment"
        ]
        assert len(attachment_parts) == 1
        assert attachment_parts[0].get_payload(decode=True) == mp3_bytes


def test_signup_header_and_disclaimer_present_for_owner_and_subscribers(audio_email_module):
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("grace@example.com", unsubscribe_token="tok-g")])

    audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
    )

    assert len(ses_client.sent_to) == 2  # owner + grace
    for entry in ses_client.sent_to:
        body = _html_body_text(entry["raw"])
        assert audio_email_module.SUBSCRIBE_SITE_URL in body
        assert "curated and written by an AI agent" in body
        assert "brief</p>" in body  # original brief content still present


def test_audio_failure_still_sends_text_only_email_to_everyone(audio_email_module):
    """AC-9 generalized to all recipients: mp3_bytes=None must not attach a part, but
    must still send. This also exercises the real module-level fail-safe: moto does not
    implement Polly's async task API, so importing audio_email_module already forced
    audio_ok=False / mp3_bytes=None at module load, matching this scenario."""
    assert audio_email_module.mp3_bytes is None
    # PRD instant-welcome-brief.md AC-2: on an audio-failure day, audio_s3_key must also
    # be None, so the later archive_todays_brief(..., audio_key=...) call writes no
    # pointer for this run.
    assert audio_email_module.audio_s3_key is None

    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("frank@example.com")])

    sent, failed, sub_sent, sub_failed, query_failed = audio_email_module.send_all(
        ses_client,
        ddb_client,
        "Subject",
        "<p>brief</p>",
        audio_email_module.mp3_bytes,
        "brief.mp3",
        "brief-subscribers-test",
    )

    assert sent == 2
    assert failed == 0
    assert sub_sent == 1
    assert sub_failed == 0
    assert query_failed is False
    for entry in ses_client.sent_to:
        parsed = email.message_from_string(entry["raw"])
        attachment_parts = [
            part for part in parsed.walk() if part.get_content_disposition() == "attachment"
        ]
        assert attachment_parts == []


# ---------------------------------------------------------------------------
# Post-send owner confirmation email (docs/prd/send-confirmation-summary.md,
# FR-1..FR-8 / AC-1..AC-7): _build_confirmation_email() (pure string-building) and
# send_confirmation_email() (the SES-wrapping, never-raises layer around it).
# ---------------------------------------------------------------------------


class RaisesOnSend:
    """SES stand-in whose send_raw_email always raises -- proves
    send_confirmation_email() swallows the failure instead of propagating it (FR-6/AC-4)."""

    def send_raw_email(self, **kwargs):
        raise RuntimeError("simulated SES failure for confirmation send")


def test_confirmation_reports_subscriber_only_count_and_failures(audio_email_module):
    subject, body = audio_email_module._build_confirmation_email(
        "2026-07-03", 5, 1, skipped=False, subscriber_query_failed=False,
    )
    assert "2026-07-03" in subject
    assert "2026-07-03" in body
    assert "Sent to 5 subscribers" in body
    assert "1 subscriber send failed" in body


def test_confirmation_zero_subscribers_no_failure_mention(audio_email_module):
    """AC-2: a genuine zero-subscriber day reports 0, not the owner, and omits any
    failure-count line since there were none."""
    subject, body = audio_email_module._build_confirmation_email(
        "2026-07-03", 0, 0, skipped=False, subscriber_query_failed=False,
    )
    assert "Sent to 0 subscribers" in body
    assert "failed" not in body.lower()


def test_confirmation_skip_mode_wording_does_not_imply_real_send(audio_email_module):
    """AC-3: SKIP_SUBSCRIBER_FANOUT wording must not report a count implying real
    subscribers were mailed, regardless of what counts are passed in."""
    subject, body = audio_email_module._build_confirmation_email(
        "2026-07-03", 0, 0, skipped=True, subscriber_query_failed=False,
    )
    assert "skipped" in body.lower()
    assert "validation run" in body.lower()
    assert "Sent to" not in body


def test_confirmation_query_failure_disambiguated_from_genuine_zero(audio_email_module):
    """AC-7: a query failure must read differently from a plain "0 subscribers"."""
    subject, body = audio_email_module._build_confirmation_email(
        "2026-07-03", 0, 0, skipped=False, subscriber_query_failed=True,
    )
    assert "lookup failed" in body.lower()
    assert "Sent to 0 subscribers" not in body


def test_confirmation_singular_wording_for_exactly_one(audio_email_module):
    """Grammar edge case: exactly 1 sent and exactly 1 failed must both use the
    singular form ("1 subscriber" / "1 subscriber send failed"), not "1 subscribers"
    or "1 subscriber sends failed"."""
    subject, body = audio_email_module._build_confirmation_email(
        "2026-07-03", 1, 1, skipped=False, subscriber_query_failed=False,
    )
    assert "Sent to 1 subscriber." in body
    assert "1 subscribers" not in body
    assert "1 subscriber send failed." in body
    assert "1 subscriber sends failed" not in body


def test_send_confirmation_email_sends_to_owner_from_sender(audio_email_module):
    ses_client = FakeSesClient()

    audio_email_module.send_confirmation_email(
        ses_client, "2026-07-03", 3, 0, skipped=False, subscriber_query_failed=False,
    )

    assert len(ses_client.sent_to) == 1
    sent = ses_client.sent_to[0]
    assert sent["recipient"] == audio_email_module.RECIP
    assert sent["source"] == audio_email_module.SENDER
    assert "3 subscribers" in _plain_body_text(sent["raw"])


def test_send_confirmation_email_failure_is_swallowed_not_raised(audio_email_module):
    """FR-6/AC-4: any exception building/sending the confirmation is caught and
    logged, never raised -- proving the pipeline can always proceed to archival
    regardless of this call's outcome."""
    ses_client = RaisesOnSend()

    # Must not raise.
    audio_email_module.send_confirmation_email(
        ses_client, "2026-07-03", 2, 0, skipped=False, subscriber_query_failed=False,
    )


def test_query_failure_from_send_all_flows_through_to_confirmation_wording(audio_email_module):
    """End-to-end AC-7: send_all()'s subscriber_query_failed signal, when passed
    straight through to send_confirmation_email(), produces the disambiguated wording
    -- not a plain zero-subscribers message -- proving the two pieces are wired
    correctly together, not just individually correct."""
    ses_client = FakeSesClient()
    ddb_client = FakeDynamoDBClient(raise_on_query=True)

    _sent, _failed, sub_sent, sub_failed, query_failed = audio_email_module.send_all(
        ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
    )
    assert query_failed is True

    confirmation_ses = FakeSesClient()
    audio_email_module.send_confirmation_email(
        confirmation_ses, "2026-07-03", sub_sent, sub_failed,
        skipped=False, subscriber_query_failed=query_failed,
    )

    assert len(confirmation_ses.sent_to) == 1
    payload = _plain_body_text(confirmation_ses.sent_to[0]["raw"])
    assert "lookup failed" in payload.lower()
    assert "Sent to 0 subscribers" not in payload


def test_send_confirmation_email_never_raises_even_with_broken_inputs(audio_email_module):
    """Belt-and-braces on FR-6: even a wildly malformed call (e.g. a client missing the
    expected method entirely) must not escape as an exception."""

    class NoSendMethodAtAll:
        pass

    audio_email_module.send_confirmation_email(
        NoSendMethodAtAll(), "2026-07-03", 1, 0, skipped=False, subscriber_query_failed=False,
    )


# ---------------------------------------------------------------------------
# `read-recent-briefs` CLI mode — the exact bug this section guards against:
# this mode is meant to run BEFORE today's brief/HTML/script exist, so it must
# not require LISTENING_SCRIPT_PATH/BRIEF_HTML_PATH/MP3_OUT_PATH/EMAIL_SUBJECT,
# and it must write each prior brief under ITS OWN actual date (not assumed
# "N days ago" arithmetic), or the skill could mislabel an older story as an
# immediate follow-up. Renamed from `read-yesterday` once it started fetching
# more than a single day (brief_history.DEFAULT_RECENT_BRIEFS_COUNT, 3 by
# default). Loaded as a *separate* module instance from `audio_email_module`
# (which is fixed to send-mode via conftest.py) because module-level behavior
# differs by sys.argv, matching how the real Lambda invokes this file twice.
# ---------------------------------------------------------------------------

import importlib.util
import sys
from pathlib import Path

from moto import mock_aws

_PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
_AUDIO_EMAIL_PATH = _PIPELINE_DIR / "audio_email.py"


def _run_read_recent_briefs_mode(working_folder, seeds=(), count_arg=None):
    """Load audio_email.py fresh in `read-recent-briefs` mode against a mocked S3.

    `seeds`, if given, is an iterable of (date, markdown) written to the briefs/
    store before the module loads. `count_arg`, if given, is passed as the CLI's
    optional count argument. Deliberately does NOT set LISTENING_SCRIPT_PATH/
    BRIEF_HTML_PATH/MP3_OUT_PATH/EMAIL_SUBJECT — proving this mode doesn't need them.
    """
    env_overrides = {
        "WORKING_FOLDER": str(working_folder),
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    old_env = {k: os.environ.get(k) for k in env_overrides}
    old_shared_cred_file = os.environ.pop("AWS_SHARED_CREDENTIALS_FILE", None)
    old_argv = sys.argv
    os.environ.update(env_overrides)
    argv = ["audio_email.py", "read-recent-briefs"]
    if count_arg is not None:
        argv.append(str(count_arg))
    sys.argv = argv

    module_name = "managed_agent_audio_email_read_recent_briefs_under_test"
    try:
        with mock_aws():
            import boto3

            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="cowork-polly-tts-740353583786")
            for date, markdown in seeds:
                s3.put_object(
                    Bucket="cowork-polly-tts-740353583786",
                    Key=f"briefs/{date}/brief.md",
                    Body=markdown.encode("utf-8"),
                )

            spec = importlib.util.spec_from_file_location(module_name, _AUDIO_EMAIL_PATH)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except SystemExit:
                pass  # read-recent-briefs mode always exits 0 -- expected, not a failure
    finally:
        sys.argv = old_argv
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if old_shared_cred_file is not None:
            os.environ["AWS_SHARED_CREDENTIALS_FILE"] = old_shared_cred_file
        sys.modules.pop(module_name, None)


def test_read_recent_briefs_mode_needs_no_send_mode_env_vars(tmp_path):
    """The regression this test guards: this mode used to sit after the module-level
    reads of LISTENING_SCRIPT_PATH/BRIEF_HTML_PATH/MP3_OUT_PATH/EMAIL_SUBJECT, so
    invoking it as designed -- before today's brief exists -- crashed with a KeyError.
    None of those env vars are set here; a clean run (no exception) is the assertion."""
    _run_read_recent_briefs_mode(tmp_path)  # must not raise


def test_read_recent_briefs_mode_writes_each_priors_own_actual_date(tmp_path):
    """Three priors spanning a weekend gap must each be written under their own real
    date, not guessed "N days ago" arithmetic -- mislabeling dates is exactly what
    would make the skill call an older story an immediate follow-up."""
    _run_read_recent_briefs_mode(
        tmp_path,
        seeds=[
            ("2026-06-24", "Wednesday's brief content"),
            ("2026-06-25", "Thursday's brief content"),
            ("2026-06-26", "Friday's brief content"),
        ],
    )

    expected = {
        "AI Brief - 2026-06-24.md": "Wednesday's brief content",
        "AI Brief - 2026-06-25.md": "Thursday's brief content",
        "AI Brief - 2026-06-26.md": "Friday's brief content",
    }
    written = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("AI Brief - *.md")}
    assert written == expected


def test_read_recent_briefs_mode_defaults_to_three(tmp_path):
    """With five priors available, the default (no count argument) must write only
    the three most recent -- not all five, and not just one."""
    _run_read_recent_briefs_mode(
        tmp_path,
        seeds=[
            ("2026-06-22", "brief 1"),
            ("2026-06-23", "brief 2"),
            ("2026-06-24", "brief 3"),
            ("2026-06-25", "brief 4"),
            ("2026-06-26", "brief 5"),
        ],
    )

    written_dates = sorted(p.name for p in tmp_path.glob("AI Brief - *.md"))
    assert written_dates == [
        "AI Brief - 2026-06-24.md",
        "AI Brief - 2026-06-25.md",
        "AI Brief - 2026-06-26.md",
    ]


def test_read_recent_briefs_mode_count_argument_is_honored(tmp_path):
    _run_read_recent_briefs_mode(
        tmp_path,
        seeds=[("2026-06-25", "brief 4"), ("2026-06-26", "brief 5")],
        count_arg=1,
    )

    written = list(tmp_path.glob("AI Brief - *.md"))
    assert [p.name for p in written] == ["AI Brief - 2026-06-26.md"]


def test_read_recent_briefs_mode_with_no_prior_briefs_writes_nothing(tmp_path):
    _run_read_recent_briefs_mode(tmp_path, seeds=())

    assert list(tmp_path.glob("AI Brief - *.md")) == []


# ---------------------------------------------------------------------------
# Feedback link (docs/prd/reader-feedback.md FR-5, ADR-0011, ADR-0012 §B): a fresh
# module instance per test (not the session-scoped `audio_email_module` fixture) so
# each test can set its own FEEDBACK_BASE_URL / FEEDBACK_TOKEN_SECRET_ARN combination
# -- mirrors the `read-recent-briefs` loader above, adapted for send mode.
# ---------------------------------------------------------------------------

FEEDBACK_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:740353583786:secret:feedback-test-xxxxx"
FEEDBACK_SECRET_VALUE = "test-feedback-signing-secret"


def _load_audio_email_module_for_feedback_test(tmp_path, *, feedback_base_url="", feedback_secret_arn=""):
    """Load a fresh instance of audio_email.py (send mode) with its own
    FEEDBACK_BASE_URL / FEEDBACK_TOKEN_SECRET_ARN env vars, against a mocked AWS
    session that (when a secret ARN is given) has the feedback signing secret
    pre-created so `_get_feedback_signing_secret()` can fetch it."""
    script_path = tmp_path / "listening-script.txt"
    html_path = tmp_path / "brief.html"
    mp3_path = tmp_path / "brief.mp3"
    script_path.write_text("This is the listening script.", encoding="utf-8")
    html_path.write_text("<html><body><h1>Brief</h1></body></html>", encoding="utf-8")

    env_overrides = {
        "LISTENING_SCRIPT_PATH": str(script_path),
        "BRIEF_HTML_PATH": str(html_path),
        "MP3_OUT_PATH": str(mp3_path),
        "EMAIL_SUBJECT": "Test AI Brief",
        "FEEDBACK_BASE_URL": feedback_base_url,
        "FEEDBACK_TOKEN_SECRET_ARN": feedback_secret_arn,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    old_env = {k: os.environ.get(k) for k in env_overrides}
    old_shared_cred_file = os.environ.pop("AWS_SHARED_CREDENTIALS_FILE", None)
    os.environ.update(env_overrides)

    module_name = f"managed_agent_audio_email_feedback_test_{id(tmp_path)}"
    mock = mock_aws()
    mock.start()
    try:
        if feedback_secret_arn:
            import boto3

            secretsmanager = boto3.client("secretsmanager", region_name="us-east-1")
            secretsmanager.create_secret(Name="feedback-test-secret", SecretString=FEEDBACK_SECRET_VALUE)
            # moto assigns its own ARN on create_secret; re-fetch and monkeypatch the
            # module's expected ARN after load isn't needed -- instead, describe the
            # secret to discover the ARN moto actually assigned, and pass THAT as the
            # env var so the module's GetSecretValue(SecretId=<that ARN>) call resolves.
            described = secretsmanager.describe_secret(SecretId="feedback-test-secret")
            os.environ["FEEDBACK_TOKEN_SECRET_ARN"] = described["ARN"]

        spec = importlib.util.spec_from_file_location(module_name, _AUDIO_EMAIL_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module, mock
    except Exception:
        mock.stop()
        raise
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if old_shared_cred_file is not None:
            os.environ["AWS_SHARED_CREDENTIALS_FILE"] = old_shared_cred_file


def test_feedback_link_present_with_valid_config(tmp_path):
    module, mock = _load_audio_email_module_for_feedback_test(
        tmp_path, feedback_base_url="https://feedback.mschweier.com", feedback_secret_arn="placeholder"
    )
    try:
        ses_client = FakeSesClient()
        ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("alice@example.com")])

        module.send_all(
            ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
        )

        assert len(ses_client.sent_to) == 2  # owner + alice
        for entry in ses_client.sent_to:
            body = _html_body_text(entry["raw"])
            assert "feedback.mschweier.com/?t=" in body
            assert "Share feedback" in body
    finally:
        mock.stop()


def test_feedback_link_gracefully_absent_when_config_missing_send_still_succeeds(tmp_path):
    module, mock = _load_audio_email_module_for_feedback_test(
        tmp_path, feedback_base_url="", feedback_secret_arn=""
    )
    try:
        ses_client = FakeSesClient()
        ddb_client = FakeDynamoDBClient(subscriber_items=[_ddb_item("bob@example.com")])

        sent, failed, sub_sent, sub_failed, query_failed = module.send_all(
            ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
        )

        assert sent == 2  # owner + bob -- send is unaffected
        assert failed == 0
        assert len(ses_client.sent_to) == 2
        for entry in ses_client.sent_to:
            body = _html_body_text(entry["raw"])
            assert "Share feedback" not in body
            assert "/?t=" not in body
    finally:
        mock.stop()


def test_feedback_link_gracefully_absent_when_base_url_missing_but_secret_configured(tmp_path):
    module, mock = _load_audio_email_module_for_feedback_test(
        tmp_path, feedback_base_url="", feedback_secret_arn="placeholder"
    )
    try:
        ses_client = FakeSesClient()
        ddb_client = FakeDynamoDBClient(subscriber_items=[])

        sent, failed, _, _, _ = module.send_all(
            ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
        )

        assert sent == 1
        assert failed == 0
        body = _html_body_text(ses_client.sent_to[0]["raw"])
        assert "Share feedback" not in body
    finally:
        mock.stop()


def test_feedback_link_uses_correct_per_recipient_identity(tmp_path):
    """AC-5/AC-7: the owner's link attributes to RECIP; each subscriber's link
    attributes to their own email -- proven here by decoding each recipient's token
    payload and checking the embedded identity matches who actually got that email."""
    module, mock = _load_audio_email_module_for_feedback_test(
        tmp_path, feedback_base_url="https://feedback.mschweier.com", feedback_secret_arn="placeholder"
    )
    try:
        ses_client = FakeSesClient()
        ddb_client = FakeDynamoDBClient(
            subscriber_items=[
                _ddb_item("alice@example.com", unsubscribe_token="tok-a"),
                _ddb_item("bob@example.com", unsubscribe_token="tok-b"),
            ]
        )

        module.send_all(
            ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
        )

        secret = module._get_feedback_signing_secret()
        assert secret == FEEDBACK_SECRET_VALUE

        for entry in ses_client.sent_to:
            body = _html_body_text(entry["raw"])
            token = body.split("/?t=")[1].split('"')[0]
            result = module.feedback_token.validate(secret, token)
            assert result.valid is True
            assert result.identity == entry["recipient"]
    finally:
        mock.stop()


def test_feedback_link_generation_failure_never_blocks_send(tmp_path, monkeypatch):
    """Belt-and-braces on the fail-safe: even if token generation itself raises for
    some unexpected reason, the send must proceed without the link, never raise."""
    module, mock = _load_audio_email_module_for_feedback_test(
        tmp_path, feedback_base_url="https://feedback.mschweier.com", feedback_secret_arn="placeholder"
    )
    try:
        monkeypatch.setattr(
            module.feedback_token, "generate", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        ses_client = FakeSesClient()
        ddb_client = FakeDynamoDBClient(subscriber_items=[])

        sent, failed, _, _, _ = module.send_all(
            ses_client, ddb_client, "Subject", "<p>brief</p>", None, "brief.mp3", "brief-subscribers-test"
        )

        assert sent == 1
        assert failed == 0
        body = _html_body_text(ses_client.sent_to[0]["raw"])
        assert "Share feedback" not in body
    finally:
        mock.stop()
