# Audio + mail step — what runs in production

This project owns the **audio + email delivery** of the daily brief. It runs today as steps
5–8 of the weekday scheduled task, whose live skill is:

    ~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md   (also: daily-ai-brief-backup/)

Steps 1–4 of that task (gather + write the brief `.md`) belong to the `daily-ai-brief` skill,
**not** this project. This repo is the versioned source-of-truth for steps 5–8 and the AWS
resources behind them; `deploy/audio_email.py` is the verbatim copy of the STEP 6 code.

## The flow (steps 5–8)

- **STEP 5 — derive two files from the brief:**
  - `/tmp/brief.html` — the brief Markdown converted to clean, inbox-readable HTML.
  - `/tmp/listening-script.txt` — plain-text, speech-optimized narration (no URLs/emoji/
    Markdown/"Sources:"), ~800–1,200 words (~5–8 min at ~150 wpm). Spoken intro → headline
    run-through → deep dives in prose. Normalize for the ear ("$2.5B" → "2.5 billion dollars").
- **STEP 6 — synthesize + email** (`deploy/audio_email.py`): async Polly (Matthew, neural, mp3)
  → S3 → download via `OutputUri` → MIME email (HTML body + MP3 attachment) → SES
  `send_raw_email`. The MP3 is also archived next to the brief as `AI Brief <YYYY-MM-DD>.mp3`.
- **STEP 7 — fail-safe:** the brief `.md` is saved regardless. If audio fails, `audio_email.py`
  prints `AUDIO_STEP_FAILED` and still sends a **text-only** email; never block on an audio glitch.
- **STEP 8 — finish:** one-line highlight + whether audio attached + a `computer://` link.

## Credentials (never committed)

In the Cowork sandbox the scheduled task points the AWS SDK at a credentials file mounted with
the working folder:

    AWS_SHARED_CREDENTIALS_FILE=<working folder>/.aws-cowork/credentials

Env-var delivery (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` via `~/.claude/settings.json`)
is the alternative when running as a Claude Code session on the Mac. Either way, the secret
never appears in this repo. Required runtime env for `audio_email.py`:
`LISTENING_SCRIPT_PATH`, `BRIEF_HTML_PATH`, `MP3_OUT_PATH`, `EMAIL_SUBJECT`.

## Gotchas (carry into any change)

- Use the API's `OutputUri`; **never build the S3 key** — Polly inserts a dot: `audio/.<TaskId>.mp3`.
- A wrong key returns **HTTP 403, not 404** (the policy omits `s3:ListBucket`).
- **SES From must be exactly `mail@mschweier.com`** — the IAM condition rejects any other From.
- Prefer boto3 `send_raw_email(RawMessage={"Data": msg.as_string()})`; the CLI `fileb://` trick
  does not expand inside the nested raw-message structure.

## Keeping this in sync

When STEP 6 changes, update **both** `deploy/audio_email.py` here and the inline copy in the
scheduled task's `SKILL.md`. (Productizing into a single imported module is a possible future
step; today the two are kept intentionally identical.)
