# Project Memory — Daily AI Brief: Audio & Mail

Loaded alongside the global operating manual (`~/.claude/CLAUDE.md`). Keep this to what's
**specific to this repo**; the workflow, roles, gates, and conventions are inherited globally.

---

## Project overview

- **Name:** daily-ai-brief-audio
- **What it does:** Turns the day's written AI brief into **narrated audio** and **emails it**,
  fully unattended. Input is a plain-text *listening script* derived from the brief; output is
  one MP3 (Amazon Polly) delivered as an email attachment (Amazon SES) alongside the brief's
  HTML body.
- **Where it fits:** the `daily-ai-brief` skill writes `AI Brief - <date>.md` + a listening
  script; a weekday scheduled task triggers **this** pipeline *after* the brief is written.
  This project does **not** generate the brief text — only audio + delivery.
- **Brief source (this machine):** `/Users/markus/Claude Working Folder/Daily AI Briefs/`
  (read-only input; configurable via `BRIEF_DIR`).
- **Repo / default branch:** local (not yet on GitHub) / `main`
- **Cloud:** AWS **personal** account, `us-east-1`. Async Polly → S3 → SES. Full spec:
  [docs/audio-mail-integration.md](docs/audio-mail-integration.md).

## Stack & build commands  (the agents read these)

- Install: `uv sync`
- Lint/format: `ruff check . && ruff format --check .`
- Types: `mypy src`
- Test: `pytest -q`

## AWS resources (personal account, us-east-1)

- IAM user `cowork-polly-tts` — least-privilege: Polly synthesis + S3 read/write on the one
  bucket + SES send from `mail@mschweier.com` only.
- S3 bucket `cowork-polly-tts-<account-id>` — SSE-S3, all public access blocked, 7-day
  lifecycle expiry on `audio/`.
- SES domain identity `mschweier.com` (DKIM-verified); sender `mail@mschweier.com`
  (account is in the SES sandbox — self-send only).

## Project-specific conventions

- **Credentials are never committed.** They live outside git — env vars from a local `.env`
  (copy `.env.example`) or `~/cowork-polly-credentials.txt` (chmod 600). CI/agents must never
  print or commit `AWS_SECRET_ACCESS_KEY`.
- **Async Polly only** (`StartSpeechSynthesisTask`) — Polly assembles one MP3 server-side; no
  ffmpeg/stitching. **Download via the API's `OutputUri`, never build the S3 key yourself.**
- **SES From must be exactly `mail@mschweier.com`** (an IAM condition rejects any other From);
  prefer boto3 `send_raw_email` over the CLI (CLI `fileb://` doesn't expand in raw-message).
- Confirm the active AWS account before any deploy/mutation (`/aws-account`, `aws-account-safety`
  skill). Never hard-code secrets or the secret key; account-id-in-bucket-name is expected.
- Listening script: plain UTF-8, no URLs/emoji/markdown, ~800–1,200 words; voice `Matthew`
  (en-US neural). Keep cost in mind (~$0.11/run).

## Active planning docs (auto-imported)

@docs/prd/_active.md
@docs/notes/_latest-checkpoint.md
