---
name: daily-ai-brief
description: Weekday AI news briefing — researched, written, validated, narrated via Polly, and emailed via SES with the MP3 attached. Runs inside a self-hosted Claude Managed Agents microVM session.
---

Generate today's Daily AI Brief, turn it into a narrated MP3, and email it (brief as the
body + MP3 attached) — fully unattended. Audio = Amazon Polly, email = Amazon SES.

## Provenance and faithfulness note

This skill is a **verbatim port** of steps 1–4 of the local Claude Desktop scheduled
task's `~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md` (STEP 1–STEP 4B), which in
turn invokes an external `daily-ai-brief` skill whose own source content was not found
on this machine as a separate file when this port was done (docs/adr/0007) — there is
no `~/Claude/Skills/daily-ai-brief/` or similar on the machine this port was built on.
**This file was therefore reconstructed from the inline STEP 1–4 description in the
local scheduled task's `SKILL.md`, which describes the workflow in enough operational
detail to port faithfully** (source tiers, gather method, validation rule, listening-
script rules) — it is not a copy of a separate skill-internal file (e.g. a `sources.md`)
that this port could not locate. No research method, source tier, validation rule, or
listening-script rule below has been changed, paraphrased away, or "improved" from that
description (PRD non-goal). If the external `daily-ai-brief` skill's own source is later
found to differ from this reconstruction, treat that discovery as a drift bug to fix via
the parallel-run diff (ADR-0007), not a reason to silently diverge further.

**The one deliberate behavior change** (per ADR-0007): "read yesterday's brief" (STEP 3)
now reads from the S3 `briefs/` store (docs/adr/0005) instead of the local
`Daily AI Briefs/` working folder, because the microVM sandbox has no filesystem shared
across runs. Everything else — the source tiers, the gather method, the brief format,
the dollar/benchmark validation step, and the listening-script rules — is unchanged.

## Environment adaptation (microVM vs. local Desktop task)

| | Local Desktop task (today) | This ported skill (microVM) |
|---|---|---|
| Brief Markdown output | `/Users/markus/Claude Working Folder/Daily AI Briefs/AI Brief - YYYY-MM-DD.md` | `/workspace/brief.md` (per `worker.mjs`'s `workdir: "/workspace"`) |
| Brief HTML | `/tmp/brief.html` | `/workspace/brief.html` |
| Listening script | `/tmp/listening-script.txt` | `/workspace/listening-script.txt` |
| MP3 output | `<working folder>/Daily AI Briefs/AI Brief <date>.mp3` | `/workspace/brief.mp3` |
| "Read yesterday's brief" | Read the local folder directly | `python3.13 /opt/pipeline/audio_email.py read-yesterday` (S3 `briefs/` read, ADR-0005) |
| AWS credentials | `AWS_SHARED_CREDENTIALS_FILE` from a mounted working-folder file | None needed — the microVM's IAM execution role resolves automatically via IMDSv2 (ADR-0004); boto3 calls with zero explicit credential setup |
| Archive today's brief | Implicit (the Markdown file already lives in the durable working folder) | Explicit: `audio_email.py`'s `__main__` archives `/workspace/brief.md` to S3 `briefs/YYYY-MM-DD/` after a successful send (ADR-0005), via `BRIEF_MARKDOWN_PATH` |

No source tier, research method, writing format, or validation rule differs between the
two columns — only where files live and how AWS is reached differ, matching ADR-0007's
"faithful port, not a rewrite" mandate.

---

STEP 1 — This SKILL.md **is** the `daily-ai-brief` skill in this runtime (there is no
separate skill file to invoke, unlike the local task's STEP 1, which invokes an external
skill by name). Follow this workflow exactly, writing to `/workspace/` as shown above
instead of a local working folder.

STEP 2 — Gather. Use web search / web fetch tools for the last ~24–48h. Prioritize
**Tier 1** (frontier labs), **Tier 2** (Hugging Face Daily Papers + arXiv cs.CL/cs.LG/
cs.AI), **Tier 4** (tech press), **Tier 7** (Hacker News, Reddit, GitHub/HF trending)
every day; scan **Tier 3** (benchmarks), **Tier 8** (policy), **Tier 9** (chips/infra);
use **Tier 6** newsletters as an end-of-pass cross-check. ~25–40 candidates before
dedup. For paywalled scoops (The Information, Bloomberg, WSJ, FT, NYT), search for free
coverage and cite both. Never fabricate URLs, numbers, or sources.

STEP 3 — Read yesterday's brief to avoid repeats (report genuine follow-ups instead).
This is the one step whose *mechanism* changed for the microVM (ADR-0007): there is no
local working folder to read, so read the most recent PRIOR brief from the S3 `briefs/`
store instead — this is optional/best-effort exactly as it was for the local task
("Optionally read yesterday's brief"), and a first-ever run or a listing/read failure
must degrade gracefully to proceeding with no avoid-repeats input, never error the run
(ADR-0005). Run in bash:

```bash
python3.13 /opt/pipeline/audio_email.py read-yesterday > /workspace/yesterdays-brief.md 2>/workspace/yesterday-read.log || true
```

If `/workspace/yesterdays-brief.md` is non-empty, read it for context before writing
today's brief. If empty (first-ever run, or nothing found strictly before today), proceed
without it — this is expected and not an error.

STEP 4A — Write the brief per this skill's output contract: `/workspace/brief.md`
(overwrite if it exists within this session — each session starts fresh, so this is
always a fresh write, not a real overwrite of a prior day). Tiered structure: one-line
tl;dr, Headlines (8–15 bullets), then deep dives (Research & Models; Industry, Deals &
Strategy; Products, Tools & Releases; Benchmarks & Evals; Policy, Safety & Society).
Omit empty sections. 5–10 deep dives. A shorter brief is fine on a quiet day.

STEP 4B — Validate. Before generating the HTML/audio, re-check every dollar figure and
benchmark score in the brief against a second independent source (prefer primary). Fix
or downgrade to "reported/unconfirmed" any that don't confirm. This is cheap insurance:
the audio and email are hard to retract once sent.

STEP 5 — Produce two derived files for delivery:

(a) **BRIEF HTML** — convert the brief Markdown to clean, inbox-readable HTML (headings,
bold, links preserved). Save to `/workspace/brief.html` (UTF-8).

(b) **LISTENING SCRIPT** — a plain-text, speech-optimized narration of the brief, NOT
the Markdown. Rules: no URLs, no emoji, no Markdown, no "Sources:" lines. ~800–1,200
words (≈5–8 min at ~150 wpm). Start with a spoken intro ("Your AI brief for {Weekday},
{Month} {D}. Top story today…"), then the headlines as a quick run-through, then the
deep dives in flowing prose. Normalize for the ear: "$2.5B" → "2.5 billion dollars";
expand or letter-read acronyms where it aids comprehension. Save to
`/workspace/listening-script.txt` (UTF-8).

STEP 6 — Synthesize + email via AWS. Sends the brief to the owner
(`mail@mschweier.com`, unchanged) and fans out to every confirmed public subscriber
(DynamoDB `brief-subscribers`) — both from `aibriefing@mschweier.com`. **No credential
file to locate** — unlike the local task's STEP 6, the microVM's IAM execution role
resolves automatically via IMDSv2 (ADR-0004); boto3 needs zero explicit credential
setup. Run this in bash:

```bash
set -e
export LISTENING_SCRIPT_PATH="/workspace/listening-script.txt"
export BRIEF_HTML_PATH="/workspace/brief.html"
export BRIEF_MARKDOWN_PATH="/workspace/brief.md"
export MP3_OUT_PATH="/workspace/brief.mp3"
export EMAIL_SUBJECT="Daily AI Brief — <DD.MM.YYYY>"   # substitute today's date, German format
export SUBSCRIBERS_TABLE_NAME="brief-subscribers"
export SUBSCRIBERS_API_BASE_URL="https://2il2bs0iq4.execute-api.us-east-1.amazonaws.com"
python3.13 /opt/pipeline/audio_email.py
```

This single invocation (`deploy/managed-agent/pipeline/audio_email.py`, the microVM
port of `deploy/audio_email.py` — see that file's own docstring for the exact
credential/persistence adaptations, ADR-0004/ADR-0005) does, in order: async Polly
synthesis (`OutputUri`, never a hand-built S3 key) with a text-only fail-safe on any
audio error; the owner's send; the subscriber fan-out (failure-isolated per recipient);
and finally archives `/workspace/brief.md` (plus the HTML and listening script) to the
S3 `briefs/YYYY-MM-DD/` store for tomorrow's STEP 3 and as the owner's durable record
(ADR-0005) — archiving is best-effort and never gates or blocks the send that already
completed.

Notes/gotchas (unchanged from the local task): region is us-east-1; bucket
`cowork-polly-tts-740353583786`; voice Matthew (en-US neural). Use the API's
`OutputUri`, never construct the S3 key (Polly inserts a dot before the TaskId). A wrong
key returns HTTP 403 (not 404) because the policy omits `s3:ListBucket` on the `audio/`
prefix (the `briefs/` prefix does grant `s3:ListBucket`, per ADR-0005 — the two prefixes
have different IAM shapes, on purpose). SES From must be exactly
`aibriefing@mschweier.com` — nothing sends from `mail@mschweier.com` anymore (it's still
the owner's recipient address). One bad subscriber address never blocks the owner's
send or anyone else's (per-recipient try/except; see `SES_SEND_FAILED`/
`SES_SENT_SUMMARY` in the output — this is also the Managed Agents run's operational
signal, PRD FR-19/AC-17, in addition to native run history). SES is still in sandbox
mode — subscriber fan-out only reaches addresses individually verified as SES test
identities until production access is requested.

STEP 7 — Fail-safe. The brief Markdown file from STEP 4 must ALWAYS be produced
regardless of audio/email outcome — it already exists at `/workspace/brief.md` by the
time STEP 6 runs, so an audio/email failure never loses the brief's content, only its
delivery. If `python3.13 /opt/pipeline/audio_email.py` prints `AUDIO_STEP_FAILED` it has
already fallen back to sending a text-only email (brief body, no attachment) — that is
acceptable; note it in the summary. If the whole STEP 6 command fails (e.g. an SES
error), do NOT lose the brief: report the failure clearly in the session's final summary
and, if possible, retry the STEP 6 command once. Never block on an audio/email error —
a fresh microVM session has no local human to notice a stuck run, so this fail-safe
matters even more here than on the local Desktop task.

STEP 8 — Finish with a one-sentence highlight and whether audio was attached. Since this
runs unattended in a scheduled Managed Agents session (no owner watching the chat), the
session's own run-history/webhook signal (PRD FR-19/AC-17) is the primary way the owner
learns the outcome — but still end the session with a clear final summary in case the
transcript is inspected.

Reader context (Markus): Applied AI Manager at Anthropic focused on Industries customers
in DACH; expert-level in Gen AI, LLMs, agentic AI, AWS Bedrock and Claude models. Dates
as DD.MM.YYYY. If a source fails to fetch, fall back to a date-scoped web search and
continue.
