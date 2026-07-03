# 0005. External cross-run persistence store for brief history

- Status: Accepted
- Date: 2026-07-03
- Deciders: architect (Claude)

## Context

Today the pipeline keeps cross-run state in a local folder,
`/Users/markus/Claude Working Folder/Daily AI Briefs/`, used to (a) read **yesterday's brief**
so the day's research avoids repeating stories, and (b) **archive** each day's output durably.
Managed Agents sessions **do not share filesystem state across runs** (PRD §6, FR-7/8/9,
AC-5/AC-6): each scheduled run starts in a fresh, empty sandbox. The local folder therefore has
no equivalent and must be replaced by a real external store. This is a required design change,
not a lift-and-shift.

Access pattern is deliberately simple: on each run, **read the single most recent prior brief**
(by date), and after producing today's brief, **write today's brief as a new dated object**.
There is no query-by-attribute, no relational access, no fan-out over this data — it is "read
the latest object by date, then append today's."

Constraint: reuse existing resources, recreate nothing (PRD §6, AC-12). Two candidates already
exist in the account: the **S3 bucket `cowork-polly-tts-740353583786`** and the **DynamoDB
table `brief-subscribers`** (whose purpose is subscribers, not briefs).

## Decision

**We will persist brief history in the existing S3 bucket `cowork-polly-tts-740353583786`,
under a new `briefs/` key prefix**, using date-ordered object keys. No new bucket, no new table.

### Layout

- **Prefix:** `briefs/`
- **Key per day:** `briefs/YYYY-MM-DD/brief.md` (the Markdown brief — the canonical archived
  artifact, FR-9). Alongside it, the derived artifacts the run also produces are stored under
  the **same dated prefix** so a day is a self-contained folder:
  - `briefs/YYYY-MM-DD/brief.html`
  - `briefs/YYYY-MM-DD/listening-script.txt`
  - (the narrated **MP3 stays where it already goes** — the Polly async `OutputUri` under
    `audio/`, unchanged; PRD FR-10. The `briefs/` prefix is for the *text* history the research
    step reads, not the audio.)
- **Date basis:** the run's **local calendar date** in the pipeline's timezone
  (America/Los_Angeles per ADR-0006), so keys line up with the human's notion of "today's
  brief," independent of UTC.

### "Read yesterday's brief" — resolve by most-recent-key, not by literal date arithmetic

The run does **not** compute `today - 1 day` and demand that exact key (that would break on
Mondays, holidays, and any missed run). Instead it **lists `briefs/` and reads the most recent
prior dated object strictly before today's date** — i.e. the latest brief that actually exists.
Concretely: `ListObjectsV2(Prefix="briefs/")`, take the greatest `YYYY-MM-DD` prefix `< today`,
read that `brief.md`. Because keys are zero-padded ISO dates, lexicographic order **is**
chronological order, so this is a cheap listing + max, no scanning of contents.

This directly resolves the PRD's flagged edge case (§7, AC-5): **"what counts as yesterday on a
Monday after a weekend"** → it is simply Friday's brief (the most recent one that exists), which
is exactly today's local-folder behavior. Same logic covers a **missed run** (reads the last
brief that did run) and the **very first run** (no prior object → the research step proceeds
with no "avoid-repeats" input, exactly as a first-ever local run would, and must degrade
gracefully rather than error).

### Retention

Add a **lifecycle rule on the `briefs/` prefix** to expire text objects after a bounded window
(recommend **90 days** — enough history for the owner's durable record and far more than the
one-day "yesterday" read needs), mirroring the existing 7-day lifecycle already on `audio/`.
This keeps the store bounded and cheap. (Distinct rule/prefix; does not touch the `audio/`
rule.)

### IAM impact

None beyond what already exists. The pipeline identity's S3 grant is already
`s3:PutObject`/`s3:GetObject` on `arn:aws:s3:::cowork-polly-tts-740353583786/*` (covers the new
`briefs/` prefix). The **only addition** the read-latest logic needs is **`s3:ListBucket`** on
the bucket ARN (listing is a bucket-level, not object-level, action), scoped with a prefix
condition to `briefs/*`. This is a small, least-privilege addition to the identity ADR-0004
settled on — the **microVM IAM execution role** (self-hosted Lambda MicroVM; the code reads
these credentials via IMDSv2, no static key). Call it out to the Developer and to
security-engineer. (Nothing else in this ADR depends on how credentials reach the runtime: the
access pattern is plain boto3 S3 calls, which work identically whether credentials come from a
role or a key.)

## Alternatives considered

- **A new DynamoDB table for brief history.** Rejected: it is more machinery than the access
  pattern needs. The pattern is "store a blob of Markdown per day, read the most recent one" —
  object storage, not a keyed item store. A brief's Markdown can also be large (multi-KB to
  tens of KB) and DynamoDB's 400 KB item limit plus its cost model make it a poorer fit for
  document blobs than S3. It would also require creating a *new* table (the existing
  `brief-subscribers` table is a different domain and must not be overloaded — mixing briefs and
  subscribers in one table would be a schema smell and would entangle the fan-out's GSI).
- **Reuse the `brief-subscribers` table with a different PK namespace** (e.g. `PK=BRIEF#date`).
  Rejected: overloading a subscriber table with brief documents couples two unrelated domains,
  risks the fan-out `Query` and the brief read interfering, and offers no benefit over S3.
- **A brand-new dedicated S3 bucket for briefs.** Rejected: PRD forbids recreating/adding
  downstream resources unnecessarily (AC-12) and the existing bucket already holds this
  pipeline's artifacts (the MP3s). A prefix in the existing bucket is the minimal change.
- **Literal `today - 1 day` key lookup.** Rejected: breaks on weekends/holidays/missed runs
  (the exact edge case the PRD flags). List-most-recent is strictly more robust and matches
  local-folder behavior.
- **Keep only the latest brief (overwrite a single `briefs/latest.md`).** Rejected: loses the
  durable per-day archive the PRD wants (FR-9, "the owner has a durable record") and makes the
  "yesterday after a weekend" logic ambiguous. Dated keys + lifecycle give both history and
  boundedness.

## Consequences

Positive:
- Reuses the existing bucket and existing S3 IAM grant almost entirely — minimal new surface
  (one `ListBucket` permission, one lifecycle rule).
- Lexicographic ISO-date keys make "most recent prior brief" a cheap listing operation and make
  the weekend/holiday/missed-run edge cases fall out for free (AC-5).
- A day is a self-contained `briefs/YYYY-MM-DD/` folder (Markdown + HTML + script), easy for the
  owner to inspect and for a parallel-run diff to compare against the local folder (PRD §8).
- Bounded storage via lifecycle; no unbounded growth, no new billing surface of note.

Negative / follow-ups:
- Requires adding **`s3:ListBucket`** (prefix-scoped to `briefs/*`) to the run identity's
  policy — a small least-privilege addition the Developer must make and security-engineer must
  review, and which must be reflected wherever `deploy/iam-policy.json` is mirrored for the new
  identity (ADR-0004).
- The new `briefs/` lifecycle rule must be created on the existing bucket as a deploy step
  (imperative, like the existing `audio/` rule) and documented in the runbook — it is not
  auto-applied.
- First-ever run has no "yesterday" object; the research step must **degrade gracefully** (no
  avoid-repeats input) rather than error. The Developer must ensure the read tolerates an empty
  listing (same as a first-ever local run).
