# daily-ai-brief-audio

Source-of-truth for the **audio + mail delivery** of the daily AI brief: it narrates the day's
brief with **Amazon Polly** and emails it with **Amazon SES** (MP3 attached, brief as the HTML
body), unattended. It consumes the output of the `daily-ai-brief` skill and is **already live** —
it runs as steps 5–8 of the weekday scheduled task. This repo versions the deployment artifacts;
it is not a standalone app.

## What's here (`deploy/`)

| File | What it is |
|---|---|
| `scheduled-task-audio.md` | The audio+mail flow (steps 5–8) and how it runs in production |
| `audio_email.py` | The exact Polly→S3→SES code (mirrors the scheduled task's inline copy) |
| `iam-policy.json` | Least-privilege IAM policy for `cowork-polly-tts` |
| `audio-mail-integration.md` | Full setup runbook (resource creation, DNS/SES, teardown) |
| `validation-handoff.md` | End-to-end validation runbook (smoke test) |

## Pipeline

listening script → Polly async task → MP3 in S3 → download via `OutputUri` → MIME email
(HTML brief + MP3 attachment) → SES `send_raw_email`. Fail-safe: on any audio error it still
sends a text-only email and the brief `.md` is always kept.

## Validate / change

```bash
python3 -m py_compile deploy/audio_email.py     # syntax
python3 -m json.tool deploy/iam-policy.json      # policy is valid JSON
# end-to-end smoke test: see deploy/validation-handoff.md
```

When `audio_email.py` changes, update **both** this file and the inline copy in
`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`.

## Credentials

Never committed. The sandbox reads `<working folder>/.aws-cowork/credentials`
(`AWS_SHARED_CREDENTIALS_FILE`); on the Mac, env vars come from `~/.claude/settings.json`.

> ⚠️ Open security item (from the validation handoff): rotate the AWS secret access key +
> GitHub PAT that were exposed in a screenshot, then update the credentials source.
