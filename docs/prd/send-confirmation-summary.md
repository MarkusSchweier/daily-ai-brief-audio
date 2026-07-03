# PRD: Post-send confirmation summary email to the owner

- Status: **Implemented, reviewed, security-cleared (2026-07-03).** No ADR (architect confirmed:
  same-file, same-sender, same-recipient, same-IAM-role addition, no new AWS resource/IAM/secret).
  Implemented in commit `a0d1450` (+ `445f8c1`, a small test-coverage follow-up) against
  FR-1..FR-8/AC-1..AC-7. Independent reviewer pass: approved, traced every AC against the actual
  code (call-site blast radius, owner-exclusion math, non-fatal failure isolation end-to-end in
  `__main__`, query-failure disambiguation wired through `send_all()` to the confirmation wording)
  — one non-blocking coverage gap (singular-form wording) found and closed. Independent
  security-engineer pass: no findings — confirmed no new AWS resource/IAM/secret, no sensitive
  data or exception detail ever reaches the email body, the DynamoDB query-failure signal has no
  externally-reachable trigger path. Next: microVM image rebuild/push (Managed Agents path only,
  per the human's decision — the local Desktop task is deactivated), live validation, PR.
- Author: product-manager (Claude)  ·  Date: 2026-07-03
- Linked ADRs: none
- Source: GitHub issue
  [#11 "Implement daily ai briefing summary for mail@mschweier.com"](https://github.com/MarkusSchweier/daily-ai-brief-audio/issues/11)

## 1. Problem

Today, after the daily pipeline runs, the owner has **no direct confirmation that the run
completed and reached its subscribers**. To know whether a scheduled run actually fanned out —
and to how many people — the owner has to inspect the Managed Agents run-history/webhook signal
or the `SES_SENT_SUMMARY sent=N failed=M` log line, or infer it from their own inbox copy (which
only proves the owner's copy went out, not the subscriber fan-out). There is no at-a-glance,
inbox-level "the brief went out to N subscribers today" acknowledgment.

The owner wants a **short, separate confirmation email** to `mail@mschweier.com` after each daily
run, stating that the briefing was sent and to how many subscribers — so the owner gets a
positive delivery signal without checking logs or the run history.

### Why now
The pipeline now runs on the Managed Agents path (the local Desktop task is deactivated), which
the owner monitors remotely. A short confirmation email closes the loop with a low-effort,
inbox-native signal. The data this needs is **already computed** inside the pipeline —
`send_all()` in `deploy/managed-agent/pipeline/audio_email.py` already returns per-run send/fail
counts — so this is a pure, additive code change with **no new AWS resource, IAM permission, or
secret**.

## 2. Goals & non-goals

### Goals
- **Send a separate, short confirmation email** to `mail@mschweier.com` from
  `aibriefing@mschweier.com` after the full daily run (owner copy + subscriber fan-out)
  completes, stating that the brief was sent and to how many **subscribers**.
- **Report a subscriber-only count**, excluding the owner's own copy — e.g. "Sent to 5
  subscribers today", not "6 recipients (you + 5 subscribers)". Report subscriber send failures
  when there were any.
- **Handle the validation-only skip mode** (`SKIP_SUBSCRIBER_FANOUT`) with clear wording so a
  validation run's confirmation is never mistaken for a real fan-out ("fan-out skipped for this
  validation run").
- **Never fail the pipeline over the confirmation itself.** The confirmation send is a
  best-effort layer on top of an already-completed run; a failure to send it is logged, not
  fatal — consistent with the project's "never lose the brief over an audio/email glitch"
  philosophy (`CLAUDE.md`).
- **Require no new AWS infrastructure, IAM permission, or secret** (see §6) — this is a pure
  code addition to the existing pipeline script running inside the same microVM session.

### Non-goals (explicitly out of scope)
- **Not merged into the daily brief email.** This is a distinct, short email — the owner's daily
  brief copy is unchanged in content, recipient, sender, and behavior.
- **No change to the subscriber fan-out, the owner copy, the audio fail-safe, or the archival
  step.** `send_all()`'s send behavior and its `(sent_count, failed_count)` return contract are
  read, not altered (aside from what §7 resolves).
- **No change to `deploy/audio_email.py` or the local Desktop task's inline copy**
  (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`). The local task is deactivated and this
  change targets the Managed Agents path only; these files are **not** lockstep targets for this
  change.
- **No new AWS resources, IAM grants, or Secrets Manager secrets.** `mail@mschweier.com` is
  already the owner's live recipient and `aibriefing@mschweier.com` is already the permitted
  sender (`deploy/iam-policy.json` gates on `ses:FromAddress` only, with no recipient
  restriction).
- **Not a rich report/digest.** No per-recipient list, no delivery-status tracking, no bounce
  handling, no HTML styling requirement beyond a plain, readable short message.

## 3. Users & use cases

- **Owner (operator/recipient)** — the sole audience for this feature.
  - *US-1:* "As the owner, after each daily run I receive a short email telling me the brief went
    out and to how many subscribers, so I get a positive delivery signal without checking logs or
    the run history."
  - *US-2:* "As the owner, if some subscriber sends failed, the confirmation tells me how many, so
    I know to look closer."
  - *US-3:* "As the owner, when I do a manual validation run with the fan-out skipped, the
    confirmation clearly says so, so I'm not misled into thinking real subscribers were mailed."
  - *US-4:* "As the owner, a glitch in this confirmation email never costs me the actual brief or
    fails the run — the brief and fan-out already happened before the confirmation is attempted."

## 4. Functional requirements

Numbered; each maps to acceptance criteria in §5. "The system shall …".

1. **Sent after the run completes.** After the daily send path finishes (i.e. after `send_all()`
   returns in the send-mode `__main__` path of
   `deploy/managed-agent/pipeline/audio_email.py`), the system shall send **one** confirmation
   email to `mail@mschweier.com` from `aibriefing@mschweier.com`. This is in addition to the
   owner's daily brief copy, not a replacement for it.
2. **Subscriber-only count, computed correctly.** The confirmation shall state the number of
   **subscribers** the brief was sent to, **excluding the owner's own copy**. Because
   `send_all()`'s current `sent_count`/`failed_count` **include the owner's own send**, the
   confirmation's subscriber count shall be derived so the owner is not double-counted (i.e. the
   count reported must reflect confirmed-subscriber sends only, not owner + subscribers). *(How
   the subscriber-only counts are surfaced from `send_all()` — e.g. returning them separately vs.
   deriving them — is an implementation detail for the Developer; the requirement is that the
   reported number is subscribers-only.)*
3. **Failure count when non-zero.** When one or more **subscriber** sends failed, the
   confirmation shall state how many failed (e.g. "Sent to 5 subscribers; 1 failed"). When no
   subscriber sends failed, the confirmation need not mention failures (or states "0 failed" —
   wording is a low-risk detail).
4. **Short subject and body.** The confirmation shall have a short, scannable subject and body
   including at minimum: that today's brief was sent, the count of subscribers it was sent to,
   the failure count if any, and the date of the run (the run's local calendar date in
   `PIPELINE_TIMEZONE`, matching how the pipeline already dates its archive). It shall not
   include the full brief content.
5. **Validation-skip wording.** When the run was invoked with `SKIP_SUBSCRIBER_FANOUT` enabled
   (manual-validation-only — the scheduled deployment never sets it), the confirmation shall
   **clearly state that the subscriber fan-out was skipped for this validation run** and shall
   **not** report a subscriber-delivery count that implies real subscribers were mailed (e.g.
   "Fan-out skipped for this validation run — no subscribers were mailed"), so a future
   validation run is not confused by a real-looking confirmation.
6. **Confirmation failure is non-fatal.** A failure of the confirmation send itself (SES error,
   or any exception while building/sending it) shall be **caught and logged, never raised** — it
   shall not fail the overall pipeline run, shall not prevent the subsequent brief-archival step,
   and shall not be treated as a run error. The confirmation is attempted only **after** the
   owner copy and fan-out have already completed, so its failure cannot affect delivery of the
   actual brief.
7. **No new AWS infrastructure/IAM/secrets.** The confirmation shall be sent using the **existing**
   SES sender (`aibriefing@mschweier.com`) to the **existing** owner recipient
   (`mail@mschweier.com`), within the existing microVM session's existing SES permissions. This
   feature shall add **no** new AWS resource, **no** new IAM permission, and **no** new secret.
   *(This is a verified scope-reducing fact, not an assumption — see §6.)*
8. **Distinguish a query failure from a genuine zero.** `_query_confirmed_subscribers()` currently
   swallows a DynamoDB query failure and returns an empty list (logging
   `SUBSCRIBERS_QUERY_FAILED`), making "0 subscribers" ambiguous between "nobody has confirmed
   yet" and "the subscriber lookup itself failed." `send_all()` shall surface a query-failure
   signal (e.g. an additional return value), and when it is set, the confirmation shall say so
   explicitly (e.g. "0 subscribers (subscriber lookup failed — please check)") rather than report
   a plain, misleadingly-normal-looking "0 subscribers today." Resolved per §7 as a
   correctness/quality requirement, not a subjective preference.

## 5. Acceptance criteria

Given/When/Then, testable in AWS account `740353583786`, `us-east-1`, with SES in sandbox and the
owner's own verified addresses standing in for subscribers.

- **AC-1 (sent after a normal run, subscriber count):** Given a scheduled run that fanned out to
  **N** confirmed subscribers with **M** subscriber-send failures, When the run completes, Then
  `mail@mschweier.com` receives one confirmation email from `aibriefing@mschweier.com` stating the
  brief was sent to **N** subscribers (owner **not** counted) and, when **M > 0**, that **M**
  failed (FR-1, FR-2, FR-3, FR-4).
- **AC-2 (owner excluded from the count):** Given a run with **exactly zero** confirmed
  subscribers, When it completes (the owner copy having been sent), Then the confirmation reports
  **0 subscribers**, not 1 — confirming the owner's own send is excluded from the reported count
  (FR-2).
- **AC-3 (validation-skip wording):** Given a manual run with `SKIP_SUBSCRIBER_FANOUT` enabled,
  When it completes, Then the confirmation clearly states the fan-out was skipped for a validation
  run and does **not** report a subscriber-delivery count implying real subscribers were mailed
  (FR-5).
- **AC-4 (confirmation failure is non-fatal):** Given the confirmation send raises for any reason
  (e.g. SES error), When the run executes, Then the pipeline run does **not** fail on account of
  it, the failure is logged, and the brief-archival step still runs — the owner's brief and the
  fan-out, already completed, are unaffected (FR-6).
- **AC-5 (no new infra/IAM/secrets):** Given the change is inspected, When the diff, IAM policy,
  and infrastructure are reviewed, Then the confirmation uses the existing sender/recipient within
  existing SES permissions and introduces **no** new AWS resource, IAM permission, or secret
  (FR-7).
- **AC-6 (date and short form):** Given any run, When the confirmation is received, Then it is a
  short email (subject + brief body) that includes the run's date and does not contain the full
  brief content (FR-4).
- **AC-7 (query failure disambiguated from a genuine zero):** Given the DynamoDB subscriber query
  raises an exception (simulated in a test), When `send_all()` runs, Then it surfaces a
  query-failure signal distinct from a genuine empty result, and the confirmation states the
  lookup failed rather than reporting a plain "0 subscribers today" (FR-8).

## 6. Constraints & dependencies

- **Managed Agents path only.** The change touches **only** `deploy/managed-agent/pipeline/
  audio_email.py`. The local Desktop task is deactivated; `deploy/audio_email.py` and the
  `SKILL.md` inline copy are **out of scope** and are **not** lockstep-updated for this change.
- **No new AWS resource, IAM permission, or secret (verified).** `deploy/iam-policy.json` grants
  `ses:SendEmail`/`ses:SendRawEmail` gated solely by an `ses:FromAddress ==
  aibriefing@mschweier.com` condition, with **no recipient restriction**; `mail@mschweier.com` is
  already the owner's live daily-copy recipient. The microVM session already holds these SES
  rights. Sending an additional email from the same sender to the same recipient therefore needs
  nothing new.
- **Sender/recipient are fixed constants.** From (`SENDER = aibriefing@mschweier.com`) and
  recipient (`RECIP = mail@mschweier.com`) already exist as module constants in `audio_email.py`
  and must be reused as-is.
- **Data source already exists.** `send_all()` already computes the per-run send/fail counts and
  logs `SES_SENT_SUMMARY sent=N failed=M`; the confirmation reads that data. Note `sent_count`
  currently **includes the owner's own send** (the owner is incremented before the fan-out loop),
  so the subscriber-only count must exclude it (FR-2).
- **Date basis.** The run's date shall use the run's local calendar date in `PIPELINE_TIMEZONE`
  (default `Europe/Berlin`), consistent with `_today_local_date()` already used for archival.
- **SES sandbox.** Unchanged; the owner's recipient is already a verified SES identity, so the
  confirmation send is unaffected by sandbox status. No production-access request here.
- **AWS account** `740353583786`, `us-east-1`. Confirm the active account before any deploy.

## 7. Risks & open questions

- **[RESOLVED] Should the confirmation distinguish "0 subscribers because nobody has confirmed
  yet" from "0 subscribers because the subscriber query failed"?** Decided: **yes**, per the
  PM's recommendation, ratified without a separate human round-trip because it is a
  correctness/quality call already implied by this repo's established "fail loudly, not
  silently" convention (the same principle behind `worker.mjs`'s AC in the managed-agents epic
  and the fail-closed webhook design in ADR-0006) — not a subjective preference fork. As the code
  stands, `_query_confirmed_subscribers()` **swallows a DynamoDB query failure and returns an
  empty list** (logging `SUBSCRIBERS_QUERY_FAILED`), indistinguishable at `send_all()`'s return
  value from a genuine zero-subscriber day. `send_all()` will surface a query-failure flag, and
  the confirmation will say "0 subscribers (subscriber lookup failed — please check)" in that
  case, versus "0 subscribers today" on a genuinely empty day. This is now folded into FR-3/AC-3
  below (renumbered from the original FR set to include this explicitly).
- **Confirmation-send failure visibility (minor).** By design (FR-6) a failed confirmation is
  non-fatal and only logged. The owner therefore loses **this** signal on the rare run where the
  confirmation itself fails — but the underlying run-history/webhook and `SES_SENT_SUMMARY` log
  line remain as the authoritative signals, so no delivery information is actually lost. Noted so
  it isn't mistaken for a gap.
- **Wording is low-risk and revisable.** Exact subject/body phrasing (including the failure-count
  and skip-mode strings) is a low-stakes product detail that can be refined without structural
  change; not gated on a decision.

## 8. Rollout & metrics

- **Phasing.** Single additive change to `deploy/managed-agent/pipeline/audio_email.py`: after
  `send_all()` returns in the send-mode `__main__` path, compute the subscriber-only count (and,
  if the §7 decision is "yes", a query-failed flag), build a short email, and send it via the
  existing SES client — wrapped in its own try/except so a failure is logged and never raised, and
  the existing brief-archival step still runs afterward.
- **Ship gate.** AC-1..AC-7 pass; a validation run confirms the owner receives a correct
  confirmation with a subscriber-only count on a normal run, correct skip-mode wording under
  `SKIP_SUBSCRIBER_FANOUT`, and that a forced confirmation-send failure does not fail the run or
  block archival. The §7 open question is resolved by the owner before final wording is fixed.
- **Success metric.** After ship: every daily Managed Agents run produces exactly one confirmation
  email to `mail@mschweier.com` stating the subscriber-only send count (and failures, if any);
  **zero** runs are failed by a confirmation-send error; and no regression to the owner's brief
  copy, the subscriber fan-out, the audio fail-safe, or the archival step.
- **Handoff.** The §7 decision (disambiguate a failed subscriber query from a genuine
  zero-subscriber day) is resolved, folded into FR-8/AC-7. The Architect reviewed and confirmed
  **no ADR is needed** (2026-07-03) — the query-failure signal is a contained tweak to
  `send_all()`'s return contract in one file, not a significant/irreversible/cross-cutting decision.
  The Developer then implements the single-file change with a unit test for the subscriber-only
  count, the skip-mode wording, the query-failure disambiguation, and the non-fatal-failure
  isolation; the reviewer verifies against AC-1..AC-7.
