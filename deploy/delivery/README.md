# `deploy/delivery/` — decoupled AWS delivery boundary

The standalone stack that performs AWS delivery for the daily AI brief (PRD
`docs/prd/agent-system-redesign.md`, **ADR-0014** Decisions 2a/2d, **ADR-0015**). Post-cut-over
it is the **only** thing that can email a real subscriber; the content-generation side (a Claude
Platform agent / the MicroVM) holds no AWS delivery IAM (FR-1).

> **Status:** deployed + live-validated, but **production still delivers in-VM** via
> `deploy/managed-agent/`. Moving production onto this boundary is an owner-gated cut-over —
> see [`CUTOVER-production-decoupling.md`](CUTOVER-production-decoupling.md).

## Endpoints (HTTP API `https://6nbe4wsng6.execute-api.us-east-1.amazonaws.com`)
- **`POST /deliver`** — async trigger. Body = contract **v2**: `brief_markdown`, `listening_script`
  (required), `candidates`, `source_usage` (raw JSON strings, best-effort archived), and
  `metadata` (`email_subject`, `brief_date` [strict `YYYY-MM-DD`], `enable_subscriber_fanout`,
  `idempotency_key`). Returns `202 {deliveryId}`; the worker (self-invoke) derives HTML → Polly →
  SES → archive. Gated by the **delivery bearer** (`delivery_auth.py`).
- **`GET /deliver/{id}`** — poll: `pending` / `succeeded` (+summary) / `failed`. Same bearer.
- **`GET /recent-briefs?count=N`** — read recent priors from S3. Gated by a **separate**,
  short-lived **signed token** (`recent_briefs_auth.py` / `recent_briefs_token.py`) — a read-token
  holder can never reach `POST /deliver` (auth separation, ADR-0014 2d).

## Data
`brief-deliveries` DynamoDB table (PK `deliveryId`) holds two item types: real delivery rows
(random UUID id, retained as history) and idempotency-dedup rows (`idem#<brief_date>`, `expiresAt`
TTL so they self-clean). No GSI, no Scan.

## Audio-as-link (docs/prd/audio-link-delivery.md, ADR-0017)
The daily brief email carries a `🎧 Listen to this brief` **link**, not an MP3 attachment.
`delivery_core.synthesize_audio()` only starts/polls the Polly task and returns the
`OutputUri`-derived key (no more `/tmp` download); `resolve_audio_metadata()` then does a
cheap `HeadObject` on that key to confirm it's live and read its S3 lifecycle `Expiration`
header, and `audio_link.build_audio_link_result()` (hand-duplicated with
`deploy/subscribers/layers/common/python/audio_link.py`) renders the meta-box line. The
link's public host (`AUDIO_BASE_URL`, same value as `SUBSCRIBE_SITE_URL`) is served by
**`deploy/subscribers/`'s** `SubscribeSiteDistribution` `/audio/*` behavior — this stack
does not own that routing or the bucket-policy statement that authorizes it (see
`deploy/subscribers/README.md`'s ownership note). No IAM change here: `HeadObject` is
authorized by the existing `S3AudioReadWrite` `s3:GetObject` grant.

## Fail-safe / operations (ADR-0015 D7/D8)
- **Idempotency:** a duplicate `POST /deliver` for the same `idempotency_key` returns the first
  delivery (no double-send); the async worker leg also claims `pending→in_progress` so a Lambda
  re-delivery can't re-send.
- **Total send failure** (nobody, not even the owner, received the brief — e.g. a full SES outage)
  is a **hard `failed`**, never a silent `succeeded`. A **partial** failure (owner OK, some
  subscribers failed) stays `succeeded` with `failed_count>0` in the summary (re-driving it would
  double-send).
- **Re-drive:** a `failed` delivery releases its idempotency claim automatically, so re-POSTing the
  same run (same four artifacts + `brief_date`) starts fresh — safe because a `failed` delivery
  means nobody was sent. The MicroVM client (`delivery_client.py`) surfaces any non-success as a
  loud non-zero exit; the four artifacts remain in the workspace for the re-drive.

## Validate / deploy
```
cd deploy/delivery
.venv/bin/python -m pytest tests/ -q          # unit tests (moto-backed)
cdk synth                                      # or: cdk deploy BriefDeliveryStack --require-approval never
```
Both bearer secrets auto-generate a shell-safe placeholder at create time and are populated/rotated
out-of-band (never in git). See `CUTOVER-production-decoupling.md` for the production cut-over.
