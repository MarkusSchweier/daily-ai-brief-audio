"""Source-of-truth extraction of the audio + email step that runs in production.

The live copy is embedded inline in the weekday scheduled task
(`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`, STEP 6); this file is the verbatim,
version-controlled copy. Reads file paths from the environment; AWS credentials come from the
ambient credential chain (`AWS_SHARED_CREDENTIALS_FILE` / env) — no secrets live in this file.

Env in: LISTENING_SCRIPT_PATH, BRIEF_HTML_PATH, MP3_OUT_PATH, EMAIL_SUBJECT.
Behavior: Polly (async) -> S3 -> download via OutputUri -> MIME email (HTML body + MP3) -> SES.
Fail-safe: on any audio error, still sends a text-only email (brief body) and reports it.
"""

import boto3, time, urllib.parse, os
from botocore.config import Config
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

REGION = "us-east-1"; BUCKET = "cowork-polly-tts-740353583786"
SENDER = "mail@mschweier.com"; RECIP = "mail@mschweier.com"
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
msg = MIMEMultipart("mixed"); msg["Subject"] = subject; msg["From"] = SENDER; msg["To"] = RECIP
alt = MIMEMultipart("alternative"); alt.attach(MIMEText(brief_html, "html", "utf-8")); msg.attach(alt)
if audio_ok:
    with open(mp3_out, "rb") as f:
        p = MIMEApplication(f.read(), _subtype="mpeg")
        p.add_header("Content-Disposition", "attachment", filename=os.path.basename(mp3_out))
        msg.attach(p)
r = ses.send_raw_email(Source=SENDER, Destinations=[RECIP], RawMessage={"Data": msg.as_string()})
print("SES_SENT", r["MessageId"], "audio_attached=", audio_ok)
