# Handoff — Validate & wire up audio for the Daily AI Brief (run in a FRESH Cowork session)

> **Historical snapshot, not current operational truth.** This runbook reflects the setup
> as of its writing, when the sender was `mail@mschweier.com`. As of PR #3 (merged
> 2026-07-02), the pipeline sends from `aibriefing@mschweier.com` instead (recipient
> `mail@mschweier.com` is unchanged). For current state, see [`CLAUDE.md`](../CLAUDE.md)
> and [`deploy/scheduled-task-audio.md`](scheduled-task-audio.md).

**Purpose:** This session's sandbox was provisioned before the network/egress and env-var
changes took effect, so AWS calls fail here. Start a **brand-new Cowork session** (fresh VM)
and follow this runbook to (1) confirm the sandbox can reach AWS, (2) prove the full
Polly→S3→SES chain end-to-end, then (3) wire the audio step into the scheduled task.

Do the steps in order. Do **not** edit the scheduled task until the smoke test (Step 3) passes.

---

## Background (already done, don't redo)

Built on the user's AWS account by Claude Code (verified working from the Mac; a test email
with a playable MP3 was received). Cowork just needs to reach it.

- **Account:** 740353583786 · **Region:** us-east-1 (everything is us-east-1)
- **IAM user:** `cowork-polly-tts`, least-privilege inline policy (Polly synth + S3 RW on the
  one bucket + SES send restricted to From=mail@mschweier.com)
- **S3 bucket:** `cowork-polly-tts-740353583786` (public access blocked, SSE-S3, 7-day
  lifecycle auto-delete on audio)
- **SES identity:** `mschweier.com` verified (DKIM). Account in SES **sandbox** (200/day) —
  fine because sender = recipient = mail@mschweier.com (self-send).
- **Credentials:** delivered via `~/.claude/settings.json` `env` block as
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION=us-east-1`.
  **The new session reads these from the environment — never paste secrets into chat.**
- **Cowork egress:** Settings → Capabilities → Allow network egress ON, Domain allowlist =
  "All domains".

**Architecture decision:** use **async Polly** (`StartSpeechSynthesisTask`) — Polly assembles
one finished MP3 server-side into S3, so the sandbox downloads a single file. No ffmpeg, no
chunk concatenation, handles any brief length.

---

## Step 1 — Confirm env vars propagated into the VM (mask the secret)

```bash
echo "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-<unset>}"
echo "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-<unset>}"
[ -n "$AWS_SECRET_ACCESS_KEY" ] && echo "AWS_SECRET_ACCESS_KEY: set (len=${#AWS_SECRET_ACCESS_KEY})" || echo "AWS_SECRET_ACCESS_KEY: <unset>"
```
Expect region us-east-1, a key id starting `AKIA…`, and the secret reported as set.
If unset → the `env` block isn't reaching the VM; stop and report (fallback: place an AWS
credentials file in the mounted working folder and point `AWS_SHARED_CREDENTIALS_FILE` at it).

## Step 2 — Confirm AWS endpoints are reachable

```bash
pip install boto3 --break-system-packages -q 2>/dev/null
python3 - <<'PY'
import boto3
sts = boto3.client("sts")
print("caller:", sts.get_caller_identity()["Arn"])
PY
```
Expect the `cowork-polly-tts` user ARN. A proxy 403 / endpoint connection error → egress still
blocked for this VM; stop and report.

## Step 3 — Smoke test the full chain (Polly → S3 → SES self-send)

Use **path-style S3 addressing** and the API's `OutputUri` (do not build the S3 key yourself —
Polly inserts a dot: `<prefix>.<TaskId>.mp3`). A wrong key returns **403, not 404** (policy
omits `s3:ListBucket`).

```python
import boto3, time, urllib.parse
from botocore.config import Config
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

REGION="us-east-1"; BUCKET="cowork-polly-tts-740353583786"
polly=boto3.client("polly", region_name=REGION)
s3=boto3.client("s3", region_name=REGION, config=Config(s3={"addressing_style":"path"}))
ses=boto3.client("ses", region_name=REGION)

script="This is a smoke test of the AI brief audio pipeline. If you can hear this, Polly, S3, and SES are all working."
t=polly.start_speech_synthesis_task(Text=script, OutputFormat="mp3", VoiceId="Matthew",
    Engine="neural", OutputS3BucketName=BUCKET, OutputS3KeyPrefix="audio/")
tid=t["SynthesisTask"]["TaskId"]
while True:
    task=polly.get_speech_synthesis_task(TaskId=tid)["SynthesisTask"]
    if task["TaskStatus"]=="completed": break
    if task["TaskStatus"]=="failed": raise RuntimeError(task.get("TaskStatusReason"))
    time.sleep(3)
key=urllib.parse.urlparse(task["OutputUri"]).path.split(f"{BUCKET}/",1)[1]
s3.download_file(BUCKET, key, "smoke.mp3")

msg=MIMEMultipart("mixed")
msg["Subject"]="[smoke test] AI Brief audio pipeline"
msg["From"]="mail@mschweier.com"; msg["To"]="mail@mschweier.com"
msg.attach(MIMEText("Smoke test — MP3 attached.","html"))
with open("smoke.mp3","rb") as f:
    p=MIMEApplication(f.read(), _subtype="mpeg")
    p.add_header("Content-Disposition","attachment",filename="smoke.mp3")
    msg.attach(p)
ses.send_raw_email(Source="mail@mschweier.com", Destinations=["mail@mschweier.com"],
                   RawMessage={"Data": msg.as_string()})
print("sent — check mail@mschweier.com")
```
**Success = an email arrives at mail@mschweier.com with a playable smoke.mp3.** Confirm with
the user before proceeding.

---

## Step 4 — Wire the audio step into the scheduled task (only after Step 3 passes)

Edit **`/Users/markus/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`**. Keep STEPS 1–4
(gather + write the brief .md) unchanged. **Replace the Gmail send (STEP 5)** with the audio +
SES flow below. Rationale: the Cowork Gmail connector can only draft/read — it can't send or
attach. SES does both.

New STEP 5 — Generate the listening script:
- A **plain-text, speech-optimized** version of the brief (NOT the markdown). No URLs, no
  emoji, no markdown, no "Sources:" lines. ~800–1,200 words (~5–8 min at ~150 wpm).
- Spoken intro: "Your AI brief for {Weekday}, {Month} {D}. Top story today…", then the
  headlines as a quick run-through, then the deep dives in flowing prose.
- Normalize for the ear: "$2.5B" → "2.5 billion dollars"; expand or letter-read acronyms
  where it aids comprehension.

New STEP 6 — Synthesize + email (boto3, reads creds from env):
- `start_speech_synthesis_task` (Matthew, neural, mp3) → poll `get_speech_synthesis_task`
  until `completed` → download via `OutputUri` (path-style S3).
- Save the MP3 into the working folder next to the brief: `AI Brief - YYYY-MM-DD.mp3`
  (archive; the 7-day S3 lifecycle handles the bucket copy).
- Build a MIME multipart email: HTML body = the brief (markdown→clean HTML), attachment =
  the MP3 named `AI Brief YYYY-MM-DD.mp3`. Send via `ses.send_raw_email`,
  From/To = mail@mschweier.com. Subject: `Daily AI Brief — DD.MM.YYYY` (optionally add 🎧).
- **Failure handling:** if Polly/SES fails, still keep the saved brief .md and send a
  text-only SES email (or note the failure). Never lose the brief over an audio glitch.

Keep the existing "save the brief file regardless" guarantee.

### Gotchas (carry into the implementation)
- Use `OutputUri`, never construct the S3 key (Polly inserts a dot before the TaskId).
- Wrong key → HTTP **403** (not 404), because `s3:ListBucket` is intentionally omitted.
- SES **From must be exactly `mail@mschweier.com`** — the IAM condition rejects any other From.
- Prefer boto3 `send_raw_email(RawMessage={"Data": msg.as_string()})`; the CLI `fileb://`
  trick does not work inside the nested structure.

### Voice & cost (FYI)
- Voice: **Matthew** (en-US neural, news tone). Alternatives: Ruth/Stephen/Joanna; German:
  Vicki/Daniel. ~$0.11/run (~$3.40/mo) Polly + negligible SES.

---

## Step 5 — Verify the scheduled run

The scheduled task runs as a Claude Code session on the Mac, so it inherits the same
`settings.json` env. After editing, trigger a manual run (or wait for the 06:07 weekday fire)
and confirm the email arrives with the MP3. Confirm with the user.

## Resolved item

Rotated 2026-07-02: a new `cowork-polly-tts` AWS access key was issued (root profile, since
the user's own least-privilege policy has no `iam:*` on itself), written to
`~/.claude/settings.json` and `<working folder>/.aws-cowork/credentials`, and verified working
before the old key was deactivated. A new GitHub classic PAT was issued and verified (the old
one no longer appeared under either Personal Access Tokens list — already dead) and written to
`~/.claude/settings.json`.
