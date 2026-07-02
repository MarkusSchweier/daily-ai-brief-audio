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
| `audio_email.py` | The exact Polly→S3→SES code (mirrors the scheduled task's inline copy); now includes fan-out to confirmed subscribers |
| `iam-policy.json` | Least-privilege IAM policy for `cowork-polly-tts` |
| `audio-mail-integration.md` | Full setup runbook (resource creation, DNS/SES, teardown) for the owner's own delivery |
| `validation-handoff.md` | End-to-end validation runbook (smoke test) for the owner's own delivery |
| `subscribers/` | Public self-service subscribe/confirm/unsubscribe system and fan-out integration; see `subscribers/README.md` |

## Pipeline

listening script → Polly async task → MP3 in S3 → download via `OutputUri` → MIME email
(HTML brief + MP3 attachment) → SES `send_raw_email` to the owner and all confirmed subscribers.
Fail-safe: on any audio error it still sends a text-only email and the brief `.md` is always kept.

## Public subscriptions (new)

In addition to the owner's own daily delivery, the system now supports public self-service
subscriptions: a lightweight static site for subscribe/unsubscribe (`deploy/subscribers/site/`),
three Lambda handlers for subscribe/confirm/unsubscribe workflows (double opt-in, ~48h
expiry), and a DynamoDB table of confirmed subscribers. Each daily run fans the brief out to
all confirmed addresses from `aibriefing@mschweier.com` with a personalized unsubscribe link
in the footer. Failure is isolated per recipient — a bad address never blocks the owner's copy
or any other subscriber.

See `deploy/subscribers/README.md` for the full CDK deploy runbook, prerequisites, manual
setup steps, testing, and teardown. The feature is built, reviewed, and ready for end-to-end
validation in the SES sandbox using test addresses.

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
