# Daily AI Brief → Audio (Amazon Polly + SES) — Cowork integration handoff

> Migrated into this project 2026-07-01 as the authoritative spec. The live IAM access key ID
> was **redacted** to `<AWS_ACCESS_KEY_ID>`; real credentials live only in `.env` /
> `~/cowork-polly-credentials.txt` and are never committed.

Set up 2026-06-30. Everything the Cowork scheduled task needs to turn the written brief
into an MP3 and **email it with the MP3 attached**, fully unattended. Audio = Polly,
email = SES. The Cowork Gmail connector is **not** used (it can only draft/read — it
can't send or attach).

## What was created in AWS (account 740353583786, us-east-1)

| Resource | Name / ID | Purpose |
|---|---|---|
| IAM user | `cowork-polly-tts` | Dedicated least-privilege identity for Cowork |
| Inline policy | `cowork-polly-tts-least-priv` | Polly synthesis + S3 read/write + SES send |
| Access key | `<AWS_ACCESS_KEY_ID>` | Static creds for the Cowork sandbox |
| S3 bucket | `cowork-polly-tts-740353583786` | Polly writes the MP3 here; Cowork reads it back |
| Lifecycle rule | `expire-audio-7-days` | Auto-deletes audio after 7 days (no buildup) |
| SES identity | `mschweier.com` (us-east-1) | Verified sender; carries the brief body + MP3 |

The bucket has all public access blocked and SSE-S3 default encryption.

## Why async Polly (no ffmpeg needed)

ffmpeg/audio-stitching would only be needed in the **sync** path, and it would run in
**Cowork's sandbox**, not in AWS. We use **async** `StartSpeechSynthesisTask` instead:
Polly assembles one finished MP3 server-side and drops it in S3, so the sandbox just
downloads a single file. No ffmpeg, no chunk concatenation, handles any brief length.

## Credentials for the Cowork sandbox

The secret is **not** in this file. It is stored locally on the Mac at:

    /Users/markus/cowork-polly-credentials.txt   (chmod 600, outside any git repo)

Copy the three values into Cowork's environment (env vars are simplest for an unattended sandbox):

    AWS_ACCESS_KEY_ID=<AWS_ACCESS_KEY_ID>
    AWS_SECRET_ACCESS_KEY=<in the file above>
    AWS_DEFAULT_REGION=us-east-1

A local AWS CLI profile `[cowork-polly]` was also created on the Mac for testing.

## The least-privilege policy (for audit)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "PollySynthesis", "Effect": "Allow",
      "Action": ["polly:StartSpeechSynthesisTask","polly:GetSpeechSynthesisTask",
                 "polly:ListSpeechSynthesisTasks","polly:SynthesizeSpeech"],
      "Resource": "*" },
    { "Sid": "S3AudioReadWrite", "Effect": "Allow",
      "Action": ["s3:PutObject","s3:GetObject"],
      "Resource": "arn:aws:s3:::cowork-polly-tts-740353583786/*" },
    { "Sid": "SesSendFromMschweier", "Effect": "Allow",
      "Action": ["ses:SendEmail","ses:SendRawEmail"],
      "Resource": "arn:aws:ses:us-east-1:740353583786:identity/mschweier.com",
      "Condition": { "StringEquals": { "ses:FromAddress": "mail@mschweier.com" } } }
  ]
}
```
Polly synthesis actions have no resource-level scoping in IAM, so `Resource:"*"` is
required there — it still only grants text-to-speech. S3 is locked to the one bucket
(`s3:ListBucket` intentionally omitted — see gotcha). SES is locked to the verified
domain identity and the exact From address.

## The flow Cowork should add to the task (after the brief .md is written)

1. Generate the **listening script** (plain text, no URLs/emoji/markdown, ~800–1,200 words).
2. `StartSpeechSynthesisTask` → async job writes one MP3 to S3.
3. Poll `GetSpeechSynthesisTask` until `completed`.
4. Download the object **using the `OutputUri` the API returns** (do not build the key yourself).
5. Assemble a MIME email (HTML brief body + MP3 attachment) and **send via SES** (`send_raw_email`).

### Validated bash — Polly part (tested end-to-end)

```bash
BUCKET=cowork-polly-tts-740353583786
REGION=us-east-1

TID=$(aws polly start-speech-synthesis-task --region "$REGION" \
        --output-format mp3 --voice-id Matthew --engine neural \
        --output-s3-bucket-name "$BUCKET" --output-s3-key-prefix audio/ \
        --text file://listening-script.txt \
        --query 'SynthesisTask.TaskId' --output text)

while :; do
  read ST URI < <(aws polly get-speech-synthesis-task --task-id "$TID" --region "$REGION" \
                    --query 'SynthesisTask.[TaskStatus,OutputUri]' --output text)
  [ "$ST" = completed ] && break
  [ "$ST" = failed ] && { echo "polly failed"; exit 1; }
  sleep 3
done

KEY=${URI#https://s3.$REGION.amazonaws.com/$BUCKET/}   # use OutputUri, not a self-built key
aws s3 cp "s3://$BUCKET/$KEY" brief.mp3 --region "$REGION"
```

### boto3 — Polly + SES end to end

```python
import boto3, time, urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

REGION = "us-east-1"; BUCKET = "cowork-polly-tts-740353583786"
polly = boto3.client("polly", region_name=REGION)
s3    = boto3.client("s3", region_name=REGION)
ses   = boto3.client("ses", region_name=REGION)

# 1. synthesize (async -> one MP3 in S3)
t = polly.start_speech_synthesis_task(
    Text=script, OutputFormat="mp3", VoiceId="Matthew", Engine="neural",
    OutputS3BucketName=BUCKET, OutputS3KeyPrefix="audio/")
tid = t["SynthesisTask"]["TaskId"]
while True:
    task = polly.get_speech_synthesis_task(TaskId=tid)["SynthesisTask"]
    if task["TaskStatus"] == "completed": break
    if task["TaskStatus"] == "failed": raise RuntimeError(task.get("TaskStatusReason"))
    time.sleep(3)
key = urllib.parse.urlparse(task["OutputUri"]).path.split(f"{BUCKET}/", 1)[1]
s3.download_file(BUCKET, key, "brief.mp3")

# 2. email it with the MP3 attached
msg = MIMEMultipart("mixed")
msg["Subject"] = "Your AI Brief — 2026-06-30"
msg["From"] = "mail@mschweier.com"
msg["To"]   = "mail@mschweier.com"
msg.attach(MIMEText(brief_html, "html"))
with open("brief.mp3", "rb") as f:
    part = MIMEApplication(f.read(), _subtype="mpeg")
    part.add_header("Content-Disposition", "attachment", filename="AI Brief 2026-06-30.mp3")
    msg.attach(part)
ses.send_raw_email(Source="mail@mschweier.com",
                   Destinations=["mail@mschweier.com"],
                   RawMessage={"Data": msg.as_string()})
```

## SES domain verification (one-time, your action — DNS)

Add these 3 CNAMEs to the `mschweier.com` zone. Easy DKIM = ownership proof **and**
signing in one step (no separate verification TXT needed):

| Name (full) | Type | Value |
|---|---|---|
| `nvzzlqcw5oynise3o5ceafoyrrily3c5._domainkey.mschweier.com` | CNAME | `nvzzlqcw5oynise3o5ceafoyrrily3c5.dkim.amazonses.com` |
| `yzvmmttcqukbpsotuboranvgg4zmalgc._domainkey.mschweier.com` | CNAME | `yzvmmttcqukbpsotuboranvgg4zmalgc.dkim.amazonses.com` |
| `oqvgwwtvzmmt2iuctwskuop5khemeeb2._domainkey.mschweier.com` | CNAME | `oqvgwwtvzmmt2iuctwskuop5khemeeb2.dkim.amazonses.com` |

Most DNS hosts auto-append the zone → enter just `<token>._domainkey` in the host field.
Verify status: `aws sesv2 get-email-identity --email-identity mschweier.com --region us-east-1`
→ `VerifiedForSendingStatus: true` when DNS has propagated (minutes–hours).

**Sandbox:** the account is in the SES sandbox (200 emails/day). That's fine here because
both sender and recipient are the verified `mail@mschweier.com` (self-send). Production
access is only needed to email *other* people.

**Optional hardening (recommended, add later):** custom MAIL FROM `bounce.mschweier.com`
(`MX 10 feedback-smtp.us-east-1.amazonses.com` + SPF `TXT "v=spf1 include:amazonses.com ~all"`)
and DMARC `_dmarc.mschweier.com TXT "v=DMARC1; p=none; rua=mailto:mail@mschweier.com"`.

## Gotchas worth remembering

- **Use `OutputUri`, never construct the key.** Polly names the object
  `<prefix>.<TaskId>.mp3` — note the dot it inserts (e.g. `audio/.<id>.mp3`).
- **A wrong key returns HTTP 403, not 404**, because the policy omits `s3:ListBucket`.
  If you see 403 on download, it's almost always a key mismatch, not a permissions bug.
- Polly async needs the caller to hold `s3:PutObject` (Polly writes as the caller) — granted.
- **SES From must be exactly `mail@mschweier.com`** — the IAM condition rejects any other From.
- **CLI `send-raw-email`:** `--raw-message Data=fileb://msg.eml` does **not** work (fileb:// isn't
  expanded inside the nested structure). Pass base64 inline: `Data=$(base64 < msg.eml | tr -d '\n')`.
  boto3's `send_raw_email(RawMessage={"Data": msg.as_string()})` has no such issue — prefer it.

## Voice & cost

- Recommended voice: **Matthew** (en-US, neural, news-anchor tone). Alternatives:
  **Ruth** / **Stephen** / **Joanna** (en-US). German: **Vicki** / **Daniel** (de-DE).
- Neural Polly ≈ $16 / 1M chars → ~7k-char brief ≈ **$0.11/run** (~$3.40/mo). SES ≈ $0.10/1k emails (negligible).
- Output ≈ 48 kbps mono MP3; a 6-minute brief ≈ ~2 MB — plays inline with one tap in iOS Mail / Gmail.

## Teardown (if ever needed)

```bash
aws iam delete-access-key --user-name cowork-polly-tts --access-key-id <AWS_ACCESS_KEY_ID>
aws iam delete-user-policy --user-name cowork-polly-tts --policy-name cowork-polly-tts-least-priv
aws iam delete-user --user-name cowork-polly-tts
aws s3 rb s3://cowork-polly-tts-740353583786 --force
aws sesv2 delete-email-identity --email-identity mschweier.com --region us-east-1
```
