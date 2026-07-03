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
2. **S3-based "yesterday's brief" read + "today's brief" archive** (docs/adr/0005),
   via `deploy.managed-agent.pipeline.brief_history`, replacing the local
   `Daily AI Briefs/` folder the research step used to read/write across runs.
   Wired in here (not in the skill) because this is the module that already knows how
   to talk to the pipeline's S3 bucket; the research/writing skill
   (deploy/managed-agent/skills/daily-ai-brief/SKILL.md) invokes the read/archive
   helpers via this module's `read_yesterdays_brief` / `archive_todays_brief` CLI
   subcommands (see `__main__` below) so the skill itself never needs its own AWS
   plumbing.

Everything else is verbatim from `deploy/audio_email.py`: async Polly synthesis via
`OutputUri` (never a hand-built S3 key), the owner copy (to `mail@mschweier.com`, from
`aibriefing@mschweier.com`, MP3 attached, text-only fail-safe on Polly failure, never
gated on subscriber sends), the subscriber fan-out (DynamoDB `brief-subscribers`
GSI `status-index`, per-recipient failure isolation), and the sign-up/disclaimer header
+ per-subscriber unsubscribe footer injected into the HTML body.

Env in (unchanged from deploy/audio_email.py): LISTENING_SCRIPT_PATH, BRIEF_HTML_PATH,
MP3_OUT_PATH, EMAIL_SUBJECT.
Optional env in (unchanged): SUBSCRIBERS_TABLE_NAME (default "brief-subscribers"),
SUBSCRIBERS_API_BASE_URL.
New optional env in: BRIEF_MARKDOWN_PATH (the brief Markdown to archive to S3 after a
successful send; if unset, archiving is skipped rather than failing the run — the
send is never gated on archiving succeeding).
"""

import boto3, time, urllib.parse, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from botocore.config import Config
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# brief_history.py lives alongside this file both in the repo (deploy/managed-agent/
# pipeline/) and in the microVM image (/opt/pipeline/, per the Dockerfile) — a
# same-directory sys.path insert keeps the import working in both layouts without
# requiring the pipeline/ directory to be installed as a package at runtime.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brief_history  # noqa: E402

REGION = "us-east-1"; BUCKET = "cowork-polly-tts-740353583786"
# Both sends go from aibriefing@ (owner's inbox address, RECIP, is unchanged).
SENDER = "aibriefing@mschweier.com"; RECIP = "mail@mschweier.com"
SUBSCRIBER_SENDER = "aibriefing@mschweier.com"
SUBSCRIBERS_TABLE_NAME = os.environ.get("SUBSCRIBERS_TABLE_NAME", "brief-subscribers")
SUBSCRIBERS_API_BASE_URL = os.environ.get("SUBSCRIBERS_API_BASE_URL", "")
# Date basis for the briefs/ archive: the run's local calendar date in the pipeline's
# timezone (docs/adr/0005 / docs/adr/0006), so keys line up with "today" the same way
# the native schedule.cron's timezone field does, independent of UTC.
PIPELINE_TIMEZONE = os.environ.get("PIPELINE_TIMEZONE", "America/Los_Angeles")


def _today_local_date() -> str:
    return datetime.now(ZoneInfo(PIPELINE_TIMEZONE)).strftime("%Y-%m-%d")


script = open(os.environ["LISTENING_SCRIPT_PATH"], encoding="utf-8").read()
brief_html = open(os.environ["BRIEF_HTML_PATH"], encoding="utf-8").read()
mp3_out = os.environ["MP3_OUT_PATH"]; subject = os.environ["EMAIL_SUBJECT"]
# No AWS_SHARED_CREDENTIALS_FILE, no explicit credentials anywhere below — boto3
# resolves the microVM's IAM execution role via IMDSv2 automatically (docs/adr/0004).
polly = boto3.client("polly", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION, config=Config(s3={"addressing_style": "path"}))
ses = boto3.client("ses", region_name=REGION)
audio_ok = True
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
    key = urllib.parse.urlparse(task["OutputUri"]).path.split(f"{BUCKET}/", 1)[1]  # use OutputUri, never build the key
    s3.download_file(BUCKET, key, mp3_out)
except Exception as e:
    print("AUDIO_STEP_FAILED:", repr(e)); audio_ok = False

# MP3 bytes are read once (if audio_ok) and reused across every recipient's message below,
# never re-read per recipient.
mp3_bytes = None
if audio_ok:
    with open(mp3_out, "rb") as f:
        mp3_bytes = f.read()


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
    Returns a list of dicts with email/firstName/unsubscribeToken; a query failure is
    treated the same as "no subscribers" so it never blocks the owner's send.
    """
    subscribers = []
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
    return subscribers


def _unsubscribe_link(email, token):
    base = SUBSCRIBERS_API_BASE_URL.rstrip("/")
    return f"{base}/unsubscribe?email={urllib.parse.quote(email)}&token={urllib.parse.quote(token)}"


SUBSCRIBE_SITE_URL = "https://briefing.mschweier.com"


def _html_with_header(html_body):
    """Prepend the forward-friendly sign-up prompt + AI-curation disclaimer.

    Added to every recipient's copy (owner included) since the owner is the most
    likely person to forward their own copy along to someone else.
    """
    header = (
        '<div style="background:#f5f5f7;border-radius:8px;padding:12px 16px;'
        'margin-bottom:20px;font-size:12px;color:#555;line-height:1.5;">'
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


def send_all(ses_client, dynamodb_client, subject, brief_html, mp3_bytes, mp3_filename, table_name):
    """Send the owner's copy, then fan out to every confirmed subscriber.

    Isolated as its own function (rather than inline top-level script code) so the
    failure-isolation loop logic is unit-testable without invoking Polly/S3. Returns
    (sent_count, failed_count) and prints the same SES_SENT / SES_SEND_FAILED /
    SES_SENT_SUMMARY log lines the production run relies on for operational visibility
    (the Managed Agents run-history/webhook is the new consumer of these lines, PRD
    FR-19/AC-17, on top of the same manual log inspection the local task supports).
    """
    sent_count = 0
    failed_count = 0

    # Sign-up prompt + AI-curation disclaimer, prepended once and shared by every
    # recipient (owner included — they're the most likely person to forward their copy).
    brief_html = _html_with_header(brief_html)

    # 1) Owner's copy — sent from aibriefing@mschweier.com to mail@mschweier.com (recipient
    # unchanged), always attempted first and never gated on subscriber sends succeeding
    # (PRD AC-8/FR-11).
    owner_msg = _build_message(SENDER, RECIP, subject, brief_html, mp3_bytes, mp3_filename)
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
    # (PRD FR-12, AC-10/AC-11).
    subscribers = _query_confirmed_subscribers(dynamodb_client, table_name)
    for subscriber in subscribers:
        email = subscriber["email"]
        if not email:
            continue
        try:
            unsubscribe_link = _unsubscribe_link(email, subscriber.get("unsubscribeToken", ""))
            subscriber_html = _html_with_unsubscribe_footer(brief_html, unsubscribe_link)
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
        except Exception as e:
            print("SES_SEND_FAILED:", email, repr(e))
            failed_count += 1

    print(f"SES_SENT_SUMMARY sent={sent_count} failed={failed_count}")
    return sent_count, failed_count


if __name__ == "__main__":
    # Two invocation modes, both driven by argv[1] so the ported skill (a thin
    # orchestration prompt + this module) can call this file both to read yesterday's
    # brief (docs/adr/0005) before writing today's, and to send + archive after.
    #
    #   python3 audio_email.py read-yesterday       -> prints yesterday's brief Markdown
    #                                                   to stdout, or nothing if none exists
    #   python3 audio_email.py            (no args) -> send today's brief (as today, via
    #                                                   the module-level script above),
    #                                                   then archive it to S3 if
    #                                                   BRIEF_MARKDOWN_PATH is set
    if len(sys.argv) > 1 and sys.argv[1] == "read-yesterday":
        prior = brief_history.read_most_recent_prior_brief(s3, _today_local_date())
        if prior is not None:
            print(prior.markdown)
        raise SystemExit(0)

    dynamodb = boto3.client("dynamodb", region_name=REGION)
    send_all(ses, dynamodb, subject, brief_html, mp3_bytes, os.path.basename(mp3_out), SUBSCRIBERS_TABLE_NAME)

    # Archive today's brief for tomorrow's "read yesterday" step and as the owner's
    # durable record (PRD FR-9/AC-6). Best-effort: never gates or retries the send
    # above, which has already completed by this point.
    markdown_path = os.environ.get("BRIEF_MARKDOWN_PATH")
    if markdown_path and os.path.exists(markdown_path):
        with open(markdown_path, encoding="utf-8") as f:
            markdown = f.read()
        brief_history.archive_todays_brief(
            s3, _today_local_date(), markdown=markdown, html=brief_html, listening_script=script
        )
    else:
        print("BRIEF_ARCHIVE_SKIPPED: BRIEF_MARKDOWN_PATH not set or file missing")
