# Project Memory — Daily AI Brief: Audio & Mail

Loaded alongside the global operating manual (`~/.claude/CLAUDE.md`). Keep this to what's
**specific to this repo**; the workflow, roles, gates, and conventions are inherited globally.

---

## Project overview

- **Name:** daily-ai-brief-audio
- **What it does:** Turns the day's written AI brief into **narrated audio** (Amazon Polly) and
  **emails it** (Amazon SES) with the MP3 attached and the brief as the HTML body — unattended.
- **Status: LIVE and working.** It runs today as steps 5–8 of the weekday scheduled task
  (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`), with the AWS resources already
  provisioned. This repo is the **versioned source-of-truth** for those deployment artifacts —
  it is not a standalone app. `deploy/audio_email.py` is the verbatim copy of the STEP 6 code.
- **Not this project:** generating the brief text (that's the `daily-ai-brief` skill, steps 1–4).
- **Brief source (this machine):** `/Users/markus/Claude Working Folder/Daily AI Briefs/`.

## Repo layout (source-of-truth for what runs)

- `deploy/scheduled-task-audio.md` — the audio+mail flow (steps 5–8) and how it runs.
- `deploy/audio_email.py` — the exact Polly→S3→SES code (mirrors the scheduled task's inline copy).
- `deploy/iam-policy.json` — the least-privilege IAM policy for user `cowork-polly-tts`.
- `deploy/audio-mail-integration.md` — full setup/runbook (resource creation, DNS/SES, teardown).
- `deploy/validation-handoff.md` — end-to-end validation runbook (smoke test).

## AWS resources (personal account `740353583786`, us-east-1)

- IAM user `cowork-polly-tts` — least-priv (Polly synth + S3 rw on the one bucket + SES send
  from `aibriefing@mschweier.com` only, plus a GSI-scoped DynamoDB Query for the subscriber
  fan-out). Policy: `deploy/iam-policy.json`.
- S3 bucket `cowork-polly-tts-740353583786` — SSE-S3, public access blocked, 7-day lifecycle
  expiry on `audio/`.
- SES domain identity `mschweier.com` (DKIM-verified); all mail (owner copy + subscriber
  fan-out) sends from `aibriefing@mschweier.com`. Owner still receives at `mail@mschweier.com`
  (recipient unchanged). SES sandbox — see `deploy/subscribers/README.md` for the
  production-access follow-up.

## How to validate / change

There is no package build. Useful checks:

- Syntax: `python3 -m py_compile deploy/audio_email.py`
- Policy JSON: `python3 -m json.tool deploy/iam-policy.json`
- End-to-end smoke test: follow `deploy/validation-handoff.md` (Polly→S3→SES self-send).
- When STEP 6 changes, update **both** `deploy/audio_email.py` **and** the inline copy in
  `~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md` (keep them identical).

## Conventions

- **Credentials are never committed.** They live outside git — the sandbox reads
  `<working folder>/.aws-cowork/credentials` via `AWS_SHARED_CREDENTIALS_FILE`, or env vars via
  `~/.claude/settings.json`. Never print or commit `AWS_SECRET_ACCESS_KEY`. Account-id-in-bucket
  name is expected; the live IAM access-key **ID** in the migrated runbook is redacted.
- **Async Polly only**; **use `OutputUri`, never build the S3 key**; **SES From must be exactly
  `aibriefing@mschweier.com`**. Fail-safe: never lose the brief over an audio/email glitch.
- Confirm the active AWS account before any deploy/mutation (`/aws-account`, `aws-account-safety`).

## Active planning docs (auto-imported)

@docs/prd/_active.md
@docs/notes/_latest-checkpoint.md
