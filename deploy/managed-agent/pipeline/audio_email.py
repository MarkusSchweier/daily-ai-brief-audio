"""STEP 6 (audio + email) adapted for the self-hosted Managed Agents microVM.

This is a **port**, not a redesign, of the live `deploy/audio_email.py` — the Polly
synth -> S3 -> SES owner-copy + subscriber-fan-out logic is unchanged (docs/prd/
managed-agents-migration.md: "byte-for-byte equivalent in intent to today's"). Two
things differ, both required by the runtime, not by any content change:

1. **No credential-file loading.** The live script's counterpart in
   `~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md` sources
   `AWS_SHARED_CREDENTIALS_FILE` from a file in the mounted working folder. Inside the
   microVM there is no such file and none is needed: boto3 picks up the microVM's IAM
   execution role automatically via IMDSv2 (docs/adr/0004) — every `boto3.client(...)`
   call below takes no explicit credentials, exactly like Lambda/EC2 code.
2. **S3-based recent-priors read + "today's brief" archive** (docs/adr/0005), via
   `deploy.managed-agent.pipeline.brief_history`, replacing the local
   `Daily AI Briefs/` folder the research step used to read/write across runs.
   Wired in here (not in the skill) because this is the module that already knows how
   to talk to the pipeline's S3 bucket; the research/writing skill
   (deploy/managed-agent/skills/daily-ai-brief/SKILL.md) invokes the read/archive
   behavior via this module's `read-recent-briefs` / (no-argument, send mode)
   CLI subcommands (see `__main__` below) so the skill itself never needs its own AWS
   plumbing.

Everything else is verbatim from `deploy/audio_email.py`: async Polly synthesis via
`OutputUri` (never a hand-built S3 key), the owner copy (to `mail@mschweier.com`, from
`aibriefing@mschweier.com`, MP3 attached, text-only fail-safe on Polly failure, never
gated on subscriber sends), the subscriber fan-out (DynamoDB `brief-subscribers`
GSI `status-index`, per-recipient failure isolation), and the sign-up/disclaimer header
+ per-subscriber unsubscribe footer injected into the HTML body.

Env in for send mode (unchanged from deploy/audio_email.py): LISTENING_SCRIPT_PATH,
BRIEF_HTML_PATH, MP3_OUT_PATH, EMAIL_SUBJECT. Not required for `read-recent-briefs`
mode — see below.
Optional env in (unchanged): SUBSCRIBERS_TABLE_NAME (default "brief-subscribers"),
SUBSCRIBERS_API_BASE_URL.
New optional env in: BRIEF_MARKDOWN_PATH (the brief Markdown to archive to S3 after a
successful send; if unset, archiving is skipped rather than failing the run — the
send is never gated on archiving succeeding). WORKING_FOLDER (default "/workspace",
matching the skill's own Configuration section) — where `read-recent-briefs` mode
writes each fetched prior brief. PIPELINE_TIMEZONE (default "Europe/Berlin", matching the
deployment's schedule timezone — ADR-0006) — the local-date basis for both the S3
archive key and for resolving "today" when reading the most recent prior briefs.
ENABLE_SUBSCRIBER_FANOUT ("1"/"true"/"yes" to enable, unset/anything else disables) —
opt-IN gate for the subscriber fan-out, flipped from an earlier opt-out design
(docs/adr's eval-harness security fix, 2026-07-04). The live scheduled production
deployment's initial_prompt explicitly exports this before running this script (see
deploy/managed-agent/deployment.json); every OTHER caller -- most importantly a
triggered evaluation run (deploy/eval/functions/trigger/handler.py), but also any
future error path, prompt-reordering bug, or forgotten export -- safely defaults to
NO fan-out with zero cooperation required, because unset (or anything other than
1/true/yes) means "skip". This is a deliberate fail-CLOSED flip: previously this was
SKIP_SUBSCRIBER_FANOUT, an opt-OUT var that defaulted to fanout-ON if unset -- i.e.
fail-OPEN, so a forgotten `export SKIP_SUBSCRIBER_FANOUT=1` (or any code path that
skipped it) would silently fan out to every real confirmed subscriber. See
SKIP_SUBSCRIBER_FANOUT below for the redundant, always-wins kill-switch this keeps.
SKIP_SUBSCRIBER_FANOUT ("1"/"true"/"yes") — an explicit, redundant kill-switch that
ALWAYS wins over ENABLE_SUBSCRIBER_FANOUT even if both are somehow set (belt-and-
suspenders). When either resolves to "skip fan-out", only the owner's copy is sent;
the DynamoDB subscriber query and fan-out loop are skipped entirely (see
send_all()'s docstring).

Post-send owner confirmation (docs/prd/send-confirmation-summary.md, FR-1..FR-8): after
send_all() returns in the send-mode __main__ path, a short, separate confirmation email
is sent to RECIP (mail@mschweier.com) from SENDER (aibriefing@mschweier.com) — in
addition to, not instead of, the owner's daily brief copy above. It states the
subscriber-only send count (excluding the owner's own send), the subscriber failure
count when non-zero, and the run's local date in PIPELINE_TIMEZONE. When
SKIP_SUBSCRIBER_FANOUT was set, it says so explicitly instead of reporting a
subscriber count (no real fan-out happened). When the subscriber DynamoDB query itself
failed (as opposed to a genuine zero-subscriber day), it says the lookup failed rather
than a plain "0 subscribers". Building/sending this confirmation is wrapped in its own
try/except: any failure is logged, never raised, and never blocks the brief-archival
step that follows it (see _build_confirmation_email() / send_all()'s
subscriber_query_failed return value).
"""

import boto3, time, urllib.parse, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from botocore.config import Config
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# brief_history.py / feedback_token.py live alongside this file both in the repo
# (deploy/managed-agent/pipeline/) and in the microVM image (/opt/pipeline/, per the
# Dockerfile) — a same-directory sys.path insert keeps the import working in both
# layouts without requiring the pipeline/ directory to be installed as a package at
# runtime.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brief_history  # noqa: E402
import feedback_token  # noqa: E402 - docs/adr/0011/0012: this copy is hand-synced with

REGION = "us-east-1"; BUCKET = "cowork-polly-tts-740353583786"
# Both sends go from aibriefing@ (owner's inbox address, RECIP, is unchanged).
SENDER = "aibriefing@mschweier.com"; RECIP = "mail@mschweier.com"
SUBSCRIBER_SENDER = "aibriefing@mschweier.com"
SUBSCRIBERS_TABLE_NAME = os.environ.get("SUBSCRIBERS_TABLE_NAME", "brief-subscribers")
SUBSCRIBERS_API_BASE_URL = os.environ.get("SUBSCRIBERS_API_BASE_URL", "")
# Feedback link (docs/prd/reader-feedback.md FR-5, ADR-0011/ADR-0012): both must be set
# for a feedback link to be embedded at all -- see _get_feedback_signing_secret() and
# _feedback_link() below. Neither being set is the expected state before the feedback
# stack is deployed and wired in (backward-compatible, never blocks the send).
FEEDBACK_TOKEN_SECRET_ARN = os.environ.get("FEEDBACK_TOKEN_SECRET_ARN", "")
FEEDBACK_BASE_URL = os.environ.get("FEEDBACK_BASE_URL", "")
# Where the skill looks for prior briefs to read (its own "Configuration: WORKING_FOLDER"
# section) and where this module writes today's brief's derived artifacts — matched to the
# skill's own default so `read-recent-briefs` mode's output lands where the skill will
# actually look for it via its normal "search WORKING_FOLDER" behavior, no skill-side
# special-casing.
WORKING_FOLDER = os.environ.get("WORKING_FOLDER", "/workspace")
# Date basis for the briefs/ archive: the run's local calendar date in the pipeline's
# timezone (docs/adr/0005 / docs/adr/0006), so keys line up with "today" the same way
# the native schedule.cron's timezone field does, independent of UTC. Must match the
# deployment's schedule.timezone (ADR-0006) -- a mismatch would archive/read under the
# wrong date near midnight in either zone.
PIPELINE_TIMEZONE = os.environ.get("PIPELINE_TIMEZONE", "Europe/Berlin")


def _today_local_date() -> str:
    return datetime.now(ZoneInfo(PIPELINE_TIMEZONE)).strftime("%Y-%m-%d")


# No AWS_SHARED_CREDENTIALS_FILE, no explicit credentials anywhere below — boto3
# resolves the microVM's IAM execution role via IMDSv2 automatically (docs/adr/0004).
s3 = boto3.client("s3", region_name=REGION, config=Config(s3={"addressing_style": "path"}))

# --- read-recent-briefs mode: must run before any send-mode env var is required below ---
# (an earlier version of this branch lived after the send-mode env reads, which meant
# invoking it as designed -- before today's brief exists -- crashed immediately on a
# missing LISTENING_SCRIPT_PATH/etc.) Named for what it now does: fetches up to the last
# few prior briefs (default brief_history.DEFAULT_RECENT_BRIEFS_COUNT, not just one), so
# the skill can both avoid repeating recent stories and correctly identify genuine
# multi-day follow-ups -- "read-yesterday" was renamed because it stopped being accurate
# once this covered more than a single day.
if len(sys.argv) > 1 and sys.argv[1] == "read-recent-briefs":
    count = int(sys.argv[2]) if len(sys.argv) > 2 else brief_history.DEFAULT_RECENT_BRIEFS_COUNT
    priors = brief_history.read_recent_prior_briefs(s3, _today_local_date(), count=count)
    if priors:
        # Write each under the skill's own dated-filename convention
        # (`AI Brief - YYYY-MM-DD.md`), using each brief's OWN actual date (not "N days
        # ago" arithmetic -- could span a weekend/holiday/missed run), so the skill's
        # normal WORKING_FOLDER search finds all of them and reasons about "how long ago
        # was this" correctly rather than mislabeling an older story as an immediate
        # follow-up.
        written = []
        for prior in priors:
            dated_path = os.path.join(WORKING_FOLDER, f"AI Brief - {prior.date}.md")
            with open(dated_path, "w", encoding="utf-8") as f:
                f.write(prior.markdown)
            written.append(dated_path)
        print(f"PRIOR_BRIEFS_FOUND count={len(priors)} dates={','.join(p.date for p in priors)} wrote={written}")
    else:
        print("PRIOR_BRIEFS_NOT_FOUND")
    raise SystemExit(0)

# --- send mode only below this point ---
script = open(os.environ["LISTENING_SCRIPT_PATH"], encoding="utf-8").read()
brief_html = open(os.environ["BRIEF_HTML_PATH"], encoding="utf-8").read()
mp3_out = os.environ["MP3_OUT_PATH"]; subject = os.environ["EMAIL_SUBJECT"]
polly = boto3.client("polly", region_name=REGION)
ses = boto3.client("ses", region_name=REGION)
audio_ok = True
audio_s3_key = None  # set below from OutputUri only if synthesis succeeds; stays None on
# an audio-failure day so archive_todays_brief's `audio_key` param (and thus whether a
# pointer is written at all, PRD instant-welcome-brief.md AC-1/AC-2) always reflects
# whether THIS run's audio is actually usable.
try:
    t = polly.start_speech_synthesis_task(Text=script, OutputFormat="mp3", VoiceId="Matthew",
        Engine="neural", OutputS3BucketName=BUCKET, OutputS3KeyPrefix="audio/")
    tid = t["SynthesisTask"]["TaskId"]; deadline = time.time() + 300
    while True:
        task = polly.get_speech_synthesis_task(TaskId=tid)["SynthesisTask"]; st = task["TaskStatus"]
        if st == "completed": break
        if st == "failed": raise RuntimeError(task.get("TaskStatusReason", "polly failed"))
        if time.time() > deadline: raise TimeoutError("polly timed out")
        time.sleep(5)
    audio_s3_key = urllib.parse.urlparse(task["OutputUri"]).path.split(f"{BUCKET}/", 1)[1]  # use OutputUri, never build the key
    s3.download_file(BUCKET, audio_s3_key, mp3_out)
except Exception as e:
    print("AUDIO_STEP_FAILED:", repr(e)); audio_ok = False; audio_s3_key = None

# MP3 bytes are read once (if audio_ok) and reused across every recipient's message below,
# never re-read per recipient.
#
# MAX_AUDIO_ATTACHMENT_BYTES mirrors deploy/subscribers/functions/welcome-send/handler.py's
# constant of the same name/value -- two independent deploy units, kept in sync by hand, same
# convention as latest_brief.py's duplicated-constants docstring. An oversized MP3 is dropped
# (never sent unattached-of-brief) rather than risking an SES raw-message-size rejection that
# would otherwise cost the recipient the written brief too.
MAX_AUDIO_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15 MB
mp3_bytes = None
if audio_ok:
    with open(mp3_out, "rb") as f:
        mp3_bytes = f.read()
    if len(mp3_bytes) > MAX_AUDIO_ATTACHMENT_BYTES:
        print("AUDIO_TOO_LARGE_SKIPPING_ATTACHMENT:", len(mp3_bytes))
        mp3_bytes = None


def _build_message(sender, recipient, subject, html_body, mp3_bytes, mp3_filename):
    """Build the MIME message shared by the owner send and every subscriber send."""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject; msg["From"] = sender; msg["To"] = recipient
    alt = MIMEMultipart("alternative"); alt.attach(MIMEText(html_body, "html", "utf-8")); msg.attach(alt)
    if mp3_bytes is not None:
        p = MIMEApplication(mp3_bytes, _subtype="mpeg")
        p.add_header("Content-Disposition", "attachment", filename=mp3_filename)
        msg.attach(p)
    return msg


def _query_confirmed_subscribers(dynamodb_client, table_name):
    """Query-only (never Scan) the status-index GSI for confirmed subscribers.

    Scoped IAM: dynamodb:Query on the status-index GSI ARN only (docs/adr/0002 §B).
    Returns a (subscribers, query_failed) tuple: `subscribers` is a list of dicts with
    email/firstName/unsubscribeToken (empty on either a genuine zero-subscriber day or
    a query failure); `query_failed` is True only when the query itself raised, so
    callers (send_all(), and in turn the confirmation email — PRD
    send-confirmation-summary.md FR-8/AC-7) can distinguish "0 because empty" from "0
    because the lookup broke". A query failure never blocks the owner's send -- the
    empty list on failure preserves that behavior unchanged.
    """
    subscribers = []
    query_failed = False
    try:
        paginator = dynamodb_client.get_paginator("query")
        for page in paginator.paginate(
            TableName=table_name,
            IndexName="status-index",
            KeyConditionExpression="#status = :confirmed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":confirmed": {"S": "confirmed"}},
        ):
            for item in page.get("Items", []):
                subscribers.append({
                    "email": item.get("email", {}).get("S", ""),
                    "firstName": item.get("firstName", {}).get("S", ""),
                    "unsubscribeToken": item.get("unsubscribeToken", {}).get("S", ""),
                })
    except Exception as e:
        print("SUBSCRIBERS_QUERY_FAILED:", repr(e))
        query_failed = True
        subscribers = []
    return subscribers, query_failed


def _unsubscribe_link(email, token):
    base = SUBSCRIBERS_API_BASE_URL.rstrip("/")
    return f"{base}/unsubscribe?email={urllib.parse.quote(email)}&token={urllib.parse.quote(token)}"


SUBSCRIBE_SITE_URL = "https://briefing.mschweier.com"

# --- Feedback link (docs/prd/reader-feedback.md FR-5, ADR-0011, ADR-0012 §B) ---------
#
# Fetched once per cold start (module-level cache), mirroring the launcher's
# `_get_secret` shape (deploy/managed-agent/microvm/launcher/launcher.py). Fail-safe
# (CLAUDE.md "never lose the brief over an audio/email glitch", already this module's
# pattern for the audio attachment): if FEEDBACK_BASE_URL / FEEDBACK_TOKEN_SECRET_ARN
# is unset, or the secret fetch fails, or token generation fails for any reason, the
# feedback link is skipped entirely for that run -- the send proceeds exactly as
# before. Never raises.
_feedback_secret_cache = None
_feedback_secret_fetch_attempted = False


def _get_feedback_signing_secret():
    """Best-effort, cached-once fetch of the feedback-token signing secret. Returns
    None (never raises) when FEEDBACK_TOKEN_SECRET_ARN is unset or the fetch fails."""
    global _feedback_secret_cache, _feedback_secret_fetch_attempted
    if _feedback_secret_fetch_attempted:
        return _feedback_secret_cache
    _feedback_secret_fetch_attempted = True
    if not FEEDBACK_TOKEN_SECRET_ARN:
        return None
    try:
        client = boto3.client("secretsmanager", region_name=REGION)
        response = client.get_secret_value(SecretId=FEEDBACK_TOKEN_SECRET_ARN)
        _feedback_secret_cache = response["SecretString"]
    except Exception as e:  # noqa: BLE001 - fail-safe: a secret-fetch glitch must never block the send
        print("FEEDBACK_LINK_SKIPPED: secret fetch failed:", repr(e))
        _feedback_secret_cache = None
    return _feedback_secret_cache


def _feedback_link(email, brief_date):
    """Build a per-recipient feedback link, or return None if unavailable for any
    reason (missing config, secret-fetch failure, token-generation failure). Never
    raises -- the caller must be able to send the email regardless (see module
    docstring / CLAUDE.md fail-safe convention)."""
    if not FEEDBACK_BASE_URL:
        print("FEEDBACK_LINK_SKIPPED: FEEDBACK_BASE_URL not set")
        return None
    secret = _get_feedback_signing_secret()
    if not secret:
        print("FEEDBACK_LINK_SKIPPED: signing secret unavailable")
        return None
    try:
        token = feedback_token.generate(secret, email, brief_date)
    except Exception as e:  # noqa: BLE001 - fail-safe: token generation must never block the send
        print("FEEDBACK_LINK_SKIPPED: token generation failed:", repr(e))
        return None
    base = FEEDBACK_BASE_URL.rstrip("/")
    return f"{base}/?t={urllib.parse.quote(token)}"


def _html_with_header(html_body, feedback_link=None):
    """Prepend the top banner: feedback prompt (when available) + forward-friendly
    sign-up prompt + AI-curation disclaimer, all in one box.

    The feedback link is per-recipient (each person's own token), so this is now
    called once per recipient rather than once shared across the whole send --
    moved from a small gray footer line to the most prominent line in the top
    banner, first thing every recipient sees (previously `_html_with_feedback_link`
    appended it after the entire brief body; PRD FR-5 doesn't mandate a position,
    this is a UX call). Omitted gracefully when unavailable (PRD fail-safe: never
    blocks or degrades the send) -- the banner still renders with just the
    subscribe prompt + disclaimer, exactly as before this change.

    Added to every recipient's copy (owner included) since the owner is the most
    likely person to forward their own copy along to someone else.
    """
    feedback_line = (
        f'<p style="margin:0 0 6px 0;">💬 Have thoughts on today\'s brief? '
        f'<a href="{feedback_link}">Share feedback</a> — we process every submission.</p>'
        if feedback_link else ""
    )
    header = (
        '<div style="background:#f5f5f7;border-radius:8px;padding:12px 16px;'
        'margin-bottom:20px;font-size:12px;color:#555;line-height:1.5;">'
        f"{feedback_line}"
        '<p style="margin:0 0 6px 0;">📬 Received this as a forward? Anyone can get '
        f'their own daily copy — <a href="{SUBSCRIBE_SITE_URL}">subscribe here</a>.</p>'
        '<p style="margin:0;">This brief is curated and written by an AI agent, '
        "which may make mistakes. For anything important, please verify with "
        "original sources and do your own research.</p>"
        "</div>"
    )
    return header + html_body


def _html_with_unsubscribe_footer(html_body, unsubscribe_link):
    footer = (
        '<hr><p style="font-size:12px;color:#666;">'
        f'You are receiving this because you subscribed to the daily AI brief. '
        f'<a href="{unsubscribe_link}">Unsubscribe</a> at any time.</p>'
    )
    return html_body + footer


def _resolve_skip_subscriber_fanout(env: dict) -> bool:
    """Resolve whether subscriber fan-out should be skipped, from `env` (normally
    `os.environ`, injected here so this is unit-testable without process-level env
    mutation). Fail-CLOSED opt-in gate (security fix, 2026-07-04, following the eval
    harness's security review): fan-out requires an explicit, positive
    `ENABLE_SUBSCRIBER_FANOUT=1/true/yes` -- unset, or any other value, means SKIP.
    `SKIP_SUBSCRIBER_FANOUT=1/true/yes`, if set, is a redundant kill-switch that
    ALWAYS wins even if `ENABLE_SUBSCRIBER_FANOUT` is also set (belt-and-suspenders).

    Previously this was the reverse: an opt-OUT `SKIP_SUBSCRIBER_FANOUT` that
    defaulted to fan-out ON when unset -- i.e. fail-OPEN. A forgotten export, a
    prompt-reordering bug, or any unanticipated error path before that export ran
    would silently fan out to every real confirmed subscriber using the shared
    production environment's live SES/DynamoDB grants (this pipeline and the eval
    harness's triggered runs share one `MicroVmExecutionRole`/environment). The live
    scheduled deployment's initial_prompt now explicitly exports
    ENABLE_SUBSCRIBER_FANOUT=1 (see deploy/managed-agent/deployment.json) so
    production behavior is unchanged; every other caller (a triggered evaluation
    run, a forgotten export, a future error path) now safely defaults to skipping
    fan-out with zero cooperation required.
    """

    def _truthy(name: str) -> bool:
        return env.get(name, "").strip().lower() in ("1", "true", "yes")

    if _truthy("SKIP_SUBSCRIBER_FANOUT"):
        return True
    return not _truthy("ENABLE_SUBSCRIBER_FANOUT")


def send_all(
    ses_client, dynamodb_client, subject, brief_html, mp3_bytes, mp3_filename, table_name,
    *, skip_subscriber_fanout=False,
):
    """Send the owner's copy, then fan out to every confirmed subscriber.

    Isolated as its own function (rather than inline top-level script code) so the
    failure-isolation loop logic is unit-testable without invoking Polly/S3. Returns
    (sent_count, failed_count, subscriber_sent_count, subscriber_failed_count,
    subscriber_query_failed) and prints the same SES_SENT / SES_SEND_FAILED /
    SES_SENT_SUMMARY log lines the production run relies on for operational visibility
    (the Managed Agents run-history/webhook is the new consumer of these lines, PRD
    FR-19/AC-17, on top of the same manual log inspection the local task supports).
    `sent_count`/`failed_count` are unchanged from before (they include the owner's own
    send, first); `subscriber_sent_count`/`subscriber_failed_count` are the new
    subscriber-only breakdown (PRD send-confirmation-summary.md FR-2), and
    `subscriber_query_failed` is True only when the DynamoDB subscriber query itself
    raised, never for a genuine zero-subscriber day (FR-8/AC-7). When
    `skip_subscriber_fanout` is True, the subscriber-only fields are all
    0/0/False -- no fan-out was attempted, so there is nothing to report and no query
    to have failed.

    `skip_subscriber_fanout` (this function parameter -- the CALLER resolves it from
    the ENABLE_SUBSCRIBER_FANOUT / SKIP_SUBSCRIBER_FANOUT env vars, see __main__
    below) is True for any run that has not explicitly opted in to fan-out --
    manual validation runs, triggered evaluation runs (deploy/eval/), and any
    unanticipated error path all default here. It is False only when the caller has
    resolved ENABLE_SUBSCRIBER_FANOUT=1 (and SKIP_SUBSCRIBER_FANOUT is not also set,
    which always wins). The scheduled production deployment's initial_prompt
    explicitly exports ENABLE_SUBSCRIBER_FANOUT=1, so the normal production path is
    unchanged; every other caller fails toward "no fan-out" by default (fail-closed,
    not fail-open) with zero cooperation required.
    """
    sent_count = 0
    failed_count = 0
    subscriber_sent_count = 0
    subscriber_failed_count = 0
    subscriber_query_failed = False

    brief_date = _today_local_date()

    # 1) Owner's copy — sent from aibriefing@mschweier.com to mail@mschweier.com (recipient
    # unchanged), always attempted first and never gated on subscriber sends succeeding
    # (PRD AC-8/FR-11). The feedback link (if available) uses the owner's RECIP address
    # as identity (PRD FR-5/AC-7 — the owner's own copy is attributable even though the
    # owner has no subscribers-table row). The header (sign-up prompt + AI-curation
    # disclaimer + feedback prompt) is per-recipient now that the feedback link is
    # embedded in it, rather than shared once across the whole send.
    owner_feedback_link = _feedback_link(RECIP, brief_date)
    owner_html = _html_with_header(brief_html, owner_feedback_link)
    owner_msg = _build_message(SENDER, RECIP, subject, owner_html, mp3_bytes, mp3_filename)
    try:
        r = ses_client.send_raw_email(
            Source=SENDER, Destinations=[RECIP], RawMessage={"Data": owner_msg.as_string()}
        )
        print("SES_SENT", r["MessageId"], "audio_attached=", mp3_bytes is not None)
        sent_count += 1
    except Exception as e:
        # Even the owner's send is failure-isolated from the rest of the script; log and
        # continue so a transient SES error here still lets the summary line and exit
        # happen cleanly.
        print("SES_SEND_FAILED:", RECIP, repr(e))
        failed_count += 1

    # 2) Subscriber fan-out — from aibriefing@mschweier.com, one send per confirmed
    # subscriber, each failure isolated so one bad address never blocks anyone else
    # (PRD FR-12, AC-10/AC-11). Skippable only for manual validation runs (see above).
    # Each subscriber's feedback link (if available) uses their own email as identity.
    if skip_subscriber_fanout:
        print("SUBSCRIBER_FANOUT_SKIPPED (manual validation run)")
    else:
        subscribers, subscriber_query_failed = _query_confirmed_subscribers(dynamodb_client, table_name)
        for subscriber in subscribers:
            email = subscriber["email"]
            if not email:
                continue
            try:
                unsubscribe_link = _unsubscribe_link(email, subscriber.get("unsubscribeToken", ""))
                subscriber_feedback_link = _feedback_link(email, brief_date)
                subscriber_html = _html_with_header(brief_html, subscriber_feedback_link)
                subscriber_html = _html_with_unsubscribe_footer(subscriber_html, unsubscribe_link)
                subscriber_msg = _build_message(
                    SUBSCRIBER_SENDER, email, subject, subscriber_html, mp3_bytes, mp3_filename
                )
                r = ses_client.send_raw_email(
                    Source=SUBSCRIBER_SENDER,
                    Destinations=[email],
                    RawMessage={"Data": subscriber_msg.as_string()},
                )
                print("SES_SENT", r["MessageId"], "recipient=", email, "audio_attached=", mp3_bytes is not None)
                sent_count += 1
                subscriber_sent_count += 1
            except Exception as e:
                print("SES_SEND_FAILED:", email, repr(e))
                failed_count += 1
                subscriber_failed_count += 1

    print(f"SES_SENT_SUMMARY sent={sent_count} failed={failed_count}")
    return sent_count, failed_count, subscriber_sent_count, subscriber_failed_count, subscriber_query_failed


def _build_confirmation_email(
    run_date, subscriber_sent_count, subscriber_failed_count, *, skipped, subscriber_query_failed,
):
    """Build the short post-send owner confirmation (PRD send-confirmation-summary.md
    FR-1..FR-5, FR-8). Pure string-building, no I/O, so it's unit-testable on its own —
    kept separate from send_confirmation_email() so a bad subject/body computation and
    an SES transport failure are both exercisable independently.

    Returns (subject, body) — short, plain text, no full brief content (FR-4/AC-6).
    """
    subject = f"AI Brief sent — {run_date}"
    lines = [f"Today's AI brief ({run_date}) was sent."]

    if skipped:
        # Manual-validation-only run: never imply real subscribers were mailed (FR-5/AC-3).
        lines.append("Fan-out skipped for this validation run — no subscribers were mailed.")
    elif subscriber_query_failed:
        # Distinguish "0 because empty" from "0 because the lookup broke" (FR-8/AC-7).
        lines.append("0 subscribers (subscriber lookup failed — please check).")
    else:
        lines.append(f"Sent to {subscriber_sent_count} subscriber{'s' if subscriber_sent_count != 1 else ''}.")
        if subscriber_failed_count > 0:
            lines.append(f"{subscriber_failed_count} subscriber send{'s' if subscriber_failed_count != 1 else ''} failed.")

    body = "\n".join(lines)
    return subject, body


def send_confirmation_email(
    ses_client, run_date, subscriber_sent_count, subscriber_failed_count, *, skipped, subscriber_query_failed,
):
    """Send the short post-send owner confirmation (PRD send-confirmation-summary.md).

    Best-effort only: any exception (building the message or the SES call itself) is
    caught and logged here, never raised — the caller (send-mode __main__) must be able
    to always proceed to the brief-archival step regardless of this function's outcome
    (FR-6/AC-4). Uses the existing SENDER/RECIP constants and the existing `ses` client
    only -- no new AWS resource, IAM permission, or secret (FR-7/AC-5).
    """
    try:
        subject, body = _build_confirmation_email(
            run_date, subscriber_sent_count, subscriber_failed_count,
            skipped=skipped, subscriber_query_failed=subscriber_query_failed,
        )
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject; msg["From"] = SENDER; msg["To"] = RECIP
        msg.attach(MIMEText(body, "plain", "utf-8"))
        r = ses_client.send_raw_email(Source=SENDER, Destinations=[RECIP], RawMessage={"Data": msg.as_string()})
        print("CONFIRMATION_SENT", r["MessageId"])
    except Exception as e:
        # Never raised: the brief and fan-out have already completed by the time this
        # runs, so a confirmation glitch must never fail the pipeline or block archival.
        print("CONFIRMATION_SEND_FAILED:", repr(e))


if __name__ == "__main__":
    # By this point, `read-recent-briefs` mode has already been handled and exited
    # above (before the send-mode env vars were even read) -- this block is send mode
    # only.
    #
    #   python3 audio_email.py read-recent-briefs [count]
    #                                                -> writes up to `count` (default
    #                                                   brief_history.DEFAULT_RECENT_BRIEFS_COUNT)
    #                                                   prior briefs, each to
    #                                                   {WORKING_FOLDER}/AI Brief - <its
    #                                                   own actual date>.md; see above
    #   python3 audio_email.py            (no args) -> send today's brief (as today, via
    #                                                   the module-level script above),
    #                                                   then archive it to S3 if
    #                                                   BRIEF_MARKDOWN_PATH is set
    # Fail-CLOSED opt-in gate -- see _resolve_skip_subscriber_fanout()'s docstring
    # for the full security rationale (this repo's eval-harness security review,
    # 2026-07-04).
    skip_fanout = _resolve_skip_subscriber_fanout(os.environ)

    dynamodb = boto3.client("dynamodb", region_name=REGION)
    _sent, _failed, subscriber_sent_count, subscriber_failed_count, subscriber_query_failed = send_all(
        ses, dynamodb, subject, brief_html, mp3_bytes, os.path.basename(mp3_out), SUBSCRIBERS_TABLE_NAME,
        skip_subscriber_fanout=skip_fanout,
    )

    # Short, separate post-send owner confirmation (PRD send-confirmation-summary.md
    # FR-1..FR-8) -- additive to, not a replacement for, the owner's brief copy already
    # sent above by send_all(). Best-effort: send_confirmation_email() itself never
    # raises, so a confirmation glitch can never fail this run or block archival below.
    send_confirmation_email(
        ses, _today_local_date(), subscriber_sent_count, subscriber_failed_count,
        skipped=skip_fanout, subscriber_query_failed=subscriber_query_failed,
    )

    # Archive today's brief for tomorrow's "read-recent-briefs" step and as the owner's
    # durable record (PRD FR-9/AC-6). Best-effort: never gates or retries the send
    # above, which has already completed by this point.
    markdown_path = os.environ.get("BRIEF_MARKDOWN_PATH")
    if markdown_path and os.path.exists(markdown_path):
        with open(markdown_path, encoding="utf-8") as f:
            markdown = f.read()
        brief_history.archive_todays_brief(
            s3, _today_local_date(), markdown=markdown, html=brief_html, listening_script=script,
            # Only pass a pointer when THIS run's audio actually succeeded (PRD
            # instant-welcome-brief.md AC-2) -- audio_s3_key is already None whenever
            # audio_ok is False, but the explicit guard keeps the "no pointer on an
            # audio-failure day" invariant obvious at the call site, not just implicit
            # in the audio step's own exception handling above.
            audio_key=audio_s3_key if audio_ok else None,
        )
    else:
        print("BRIEF_ARCHIVE_SKIPPED: BRIEF_MARKDOWN_PATH not set or file missing")

    # Archive the skill's "candidates considered" research artifact, if this run's
    # skill version wrote one (PRD docs/prd/eval-harness.md FR-4, ADR-0013 §D). A
    # separate, independent best-effort step from the brief archival above: an older
    # run (or a run before the live Skills-API version push lands, ADR-0008) simply
    # has no candidates.json to find, which archive_candidates_file() treats as the
    # expected common case, not a failure -- never gates or affects the send that has
    # already completed by this point.
    brief_history.archive_candidates_file(s3, _today_local_date(), working_folder=WORKING_FOLDER)
