# daily-ai-brief-audio

Turns the daily written AI brief into **narrated audio** (Amazon Polly) and **emails it**
(Amazon SES), unattended. It consumes the output of the `daily-ai-brief` skill (the brief
markdown + a plain-text listening script) and is triggered by the weekday scheduled task after
the brief is written. It does **not** generate the brief text.

- **Pipeline:** listening script → Polly async task → MP3 in S3 → download via `OutputUri` →
  MIME email (HTML brief + MP3 attachment) → SES `send_raw_email`. Full spec:
  [docs/audio-mail-integration.md](docs/audio-mail-integration.md).
- **AWS:** personal account, `us-east-1` — least-privilege IAM user, encrypted S3 bucket with a
  7-day expiry, DKIM-verified SES domain. Credentials live outside git.

## Setup

```bash
uv sync
cp .env.example .env    # fill from ~/cowork-polly-credentials.txt — never commit real values
```

## Develop (Claude Code stack)

Plan first, then build:

```
/feature "synthesize the listening script to MP3 via Polly and email it via SES with the brief attached"
```

## Checks

```bash
ruff check . && ruff format --check .
mypy src
pytest -q
```
