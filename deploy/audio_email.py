"""Source-of-truth extraction of the audio + email step that runs in production.

The live copy is embedded inline in the weekday scheduled task
(`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`, STEP 6); this file is the verbatim,
version-controlled copy. Reads file paths from the environment; AWS credentials come from the
ambient credential chain (`AWS_SHARED_CREDENTIALS_FILE` / env) — no secrets live in this file.

Env in: LISTENING_SCRIPT_PATH, BRIEF_HTML_PATH, MP3_OUT_PATH, EMAIL_SUBJECT.
Optional env in: SUBSCRIBERS_TABLE_NAME (default "brief-subscribers"),
SUBSCRIBERS_API_BASE_URL (base URL used to build each subscriber's personalized
unsubscribe link; e.g. the API Gateway execute-api URL or, once wired, the custom domain).
Behavior: Polly (async) -> S3 -> download via OutputUri -> MIME email (HTML body + MP3) -> SES,
sent to the owner (unchanged) and fanned out to every confirmed subscriber (additive; see
docs/prd/public-subscriptions.md and docs/adr/0002/0003).
Fail-safe: on any audio error, still sends a text-only email (brief body) and reports it, for
every recipient. Fail-safe: a bad subscriber address never blocks the owner's send or any other
subscriber's send (per-recipient try/except; see SES_SEND_FAILED / SES_SENT_SUMMARY log lines).
"""

import boto3, time, urllib.parse, os
from botocore.config import Config
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

REGION = "us-east-1"; BUCKET = "cowork-polly-tts-740353583786"
# Both sends now go from aibriefing@ (owner's inbox address, RECIP, is unchanged).
SENDER = "aibriefing@mschweier.com"; RECIP = "mail@mschweier.com"
SUBSCRIBER_SENDER = "aibriefing@mschweier.com"
SUBSCRIBERS_TABLE_NAME = os.environ.get("SUBSCRIBERS_TABLE_NAME", "brief-subscribers")
SUBSCRIBERS_API_BASE_URL = os.environ.get("SUBSCRIBERS_API_BASE_URL", "")
script = open(os.environ["LISTENING_SCRIPT_PATH"], encoding="utf-8").read()
brief_html = open(os.environ["BRIEF_HTML_PATH"], encoding="utf-8").read()
mp3_out = os.environ["MP3_OUT_PATH"]; subject = os.environ["EMAIL_SUBJECT"]
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
    SES_SENT_SUMMARY lines the production run relies on for operational visibility.
    """
    sent_count = 0
    failed_count = 0

    # Sign-up prompt + AI-curation disclaimer, prepended once and shared by every
    # recipient (owner included — they're the most likely person to forward their copy).
    brief_html = _html_with_header(brief_html)

    # 1) Owner's copy — sent from aibriefing@mschweier.com to mail@mschweier.com (recipient
    # unchanged), always attempted first and never gated on subscriber sends succeeding
    # (PRD AC-6/AC-15, FR-15).
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
    # (PRD FR-12..14, AC-8).
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
    dynamodb = boto3.client("dynamodb", region_name=REGION)
    send_all(ses, dynamodb, subject, brief_html, mp3_bytes, os.path.basename(mp3_out), SUBSCRIBERS_TABLE_NAME)
