# PRD: Evaluation harness for the daily AI brief pipeline

- Status: **Approved, ready for build (2026-07-04).** Gate 0 cleared: ADR-0013 presented both
  backbone options and the human signed off on **Option A (custom AWS-native harness)**. The v1
  evaluation-criteria set has also been trimmed by the owner from the full nine-criterion candidate
  list down to five (§4.B) to avoid over-engineering the first build. This epic remains
  **measurement infrastructure only**; the follow-up cost-optimization epic is explicitly out of
  scope here (see §2).
- Author: product-manager (Claude)  ·  Date: 2026-07-04
- Linked ADRs: [0013 Evaluation-harness backbone: build vs. adopt](../adr/0013-eval-harness-backbone-build-vs-adopt.md)
  (**Accepted** — Option A, custom AWS-native, human-approved).
- Source: owner request following a real cost analysis of the daily pipeline (~$2.60–2.65/run in
  Claude Sonnet 5 usage, dominated by cache-read tokens — ~1.2M in research vs. ~4.2M in the
  post-research writing/delivery phase, because every subsequent turn re-sends the accumulated
  research context). This is **epic 1 of 2**: build the eval harness first, so the later
  cost-optimization epic (model-per-subtask splitting, session/context restructuring, brief-length
  trimming) can be measured against **real quality signal** rather than shipped on vibes.

## 1. Problem

The daily AI brief pipeline (`deploy/managed-agent/`) researches, writes, narrates, and emails an
AI news brief to the owner and a growing list of public subscribers every weekday, unattended.
The owner just did a real, transcript-mined cost analysis and found each run costs **~$2.60–2.65**
in Claude Sonnet 5 usage, overwhelmingly from **cache-read tokens** — and, counter-intuitively,
the **post-research writing/delivery phase costs *more* than research** (~4.2M vs. ~1.2M cache-read
tokens), because every subsequent turn in one long agentic session re-sends the entire accumulated
research context. That analysis was done **once, by hand**, by mining a session transcript's
`span.model_request_end` events — it is not repeatable tooling.

The owner wants to reduce that cost (a **separate, later epic**: model-per-subtask splitting,
session/context restructuring, brief-length trimming). But cost changes to an LLM pipeline can
silently degrade quality — a cheaper model or a trimmed context can drop an important story, skew
tone, hallucinate, or repeat yesterday's news — and there is currently **no way to measure whether
a pipeline change made the brief worse.** The only quality signal today is the just-shipped
public feedback form (`deploy/feedback/`, `feedback.mschweier.com`), which collects a handful of
real reader ratings per edition but arrives slowly, sparsely, and only *after* an edition has
shipped to real subscribers.

We need to build the **measurement infrastructure first**: an evaluation harness that scores a
brief (and its production run) across the quality axes that matter, calibrates those automated
scores against real reader feedback, lets a human quickly review and correct the automated
judgment through an easy web interface, captures the full **cost breakdown** as a first-class
criterion, and emits a **structured, versioned, machine-readable record** of every run so a future
optimization agent can read it and reason about tradeoffs. Only with this harness in place can the
next epic change the pipeline *and prove it didn't sacrifice quality to save money.*

### Why now
The cost analysis is fresh and the appetite to optimize is real, but optimizing without a quality
baseline risks shipping a cheaper, worse brief to a growing subscriber base. Building the harness
now — before any cost change — establishes the baseline and the tooling to measure every candidate
change against it. The feedback form just shipped, so real reader ground-truth is beginning to
accumulate and can be wired in as a calibration signal from the start.

## 2. Goals & non-goals

### Goals
- **Evaluate a brief-production run across a defined set of quality axes** (§4.A), producing
  per-criterion scores with judge rationale and supporting evidence — not a single opaque number.
- **Calibrate automated (LLM-judge) scores against real reader feedback.** Use the existing
  `brief-feedback` DynamoDB table (7 graded 1–5 ratings + 2 free-text fields per real submission,
  with anonymity handling — see `reader-feedback.md`) as a ground-truth signal: correlate judge
  scores against real reader scores on the **same editions**, and surface reader free-text
  suggestions into what the judge checks for.
- **Give a human an easy, intuitive web interface** to review each evaluation, see the brief and
  the judge's scores/rationale side by side, and agree with or override each per-criterion score
  with an optional comment — not a CLI, not a spreadsheet.
- **Evaluate content-selection quality** by contrasting **all** stories/topics identified during
  research against the stories actually chosen for the final brief (was something important
  dropped? was something low-value included?). This requires the research phase to emit a durable
  "candidates considered" artifact it does not produce today (§4.A, FR-4; lockstep constraint §6).
- **Make cost a first-class eval criterion with a full phase-level breakdown** (research vs. writing
  vs. delivery, by real Claude API token usage), turning the owner's one-off manual transcript
  analysis into repeatable tooling.
- **Emit a structured, versioned, machine-readable record per eval run** capturing the
  configuration evaluated, per-criterion scores + judge rationale + evidence, any human override,
  the full cost breakdown, and variance metadata — explicitly building the **input** to a future
  optimization-agent epic.
- **Handle statistical variance** with a default of **3 replicate runs** per candidate
  configuration, and support **freezing a research phase's output and replaying it** through
  multiple writing-phase configurations, so testing writing-phase changes doesn't re-pay for
  research each time.
- **Run evaluations as a separate, deliberately-triggered activity** — never folded into the live
  scheduled weekday send — so real subscriber-facing sends are never gated on, slowed by, or put at
  risk by evaluation (decided, §2 / FR-1).

### Non-goals (explicitly out of scope for this epic)
- **No actual pipeline or cost changes.** This epic builds **measurement infrastructure only.**
  Model-per-subtask splitting, session/context restructuring, brief-length trimming, and any other
  optimization are the **separate follow-up epic** the harness exists to measure. This PRD must not
  scope, design, or pre-commit to any of them.
- **No auto-remediation / optimization agent.** The harness produces the machine-readable *input*
  a future "read eval output and propose/implement pipeline changes" agent would consume; it does
  **not** build that agent, and it does **not** automatically apply any change.
- **No change to the live daily pipeline's behavior, schedule, output, or delivery** — except the
  single **additive** change of the research phase emitting a durable "candidates considered"
  artifact (§4.A, FR-4), which does not alter the produced brief or its send. The weekday send time
  (06:07, `deployment.json` cron `"7 6 * * 1-5"`) and the fan-out are untouched.
- **No change to the feedback collection surface.** The harness **reads** the `brief-feedback`
  table as a calibration signal; it does **not** change `deploy/feedback/`, its form, its schema,
  its token scheme, or its anonymity handling. Reading feedback must honor the existing anonymity
  guarantees (§6).
- **No change to SES, Polly, the subscriber fan-out, subscribe/confirm/unsubscribe, or the
  instant-welcome-brief.** Evaluations do not send email to real subscribers.
- **The build-vs-adopt architecture decision is NOT made here.** Whether the harness is a
  custom AWS-native build (mirroring `deploy/subscribers/` / `deploy/feedback/`: static review
  site + API Gateway + Lambda + DynamoDB) or an adopted self-hostable eval/observability tool
  (e.g. Langfuse, Arize Phoenix) is **deferred to the Architect's ADR** and the human's sign-off
  (§7). This PRD states *what* the harness must do, architecture-agnostically, never *how* it is
  built.
- **No re-scoring of historical/pre-harness editions is required** beyond what calibration against
  already-collected feedback needs; backfilling a full eval history is out of scope.

## 3. Users & use cases

- **Owner / optimization operator (primary)** — the reason the harness exists.
  - *US-1:* "As the owner, before I ship a cost-optimizing pipeline change, I trigger an
    evaluation of a candidate configuration and get per-criterion quality scores plus a full cost
    breakdown, so I can see exactly what quality (if any) a cheaper configuration costs me."
  - *US-2:* "As the owner, I run **3 replicates** of a candidate by default and see the variance,
    so I'm not fooled by a single lucky or unlucky run."
  - *US-3:* "As the owner testing only a **writing-phase** change, I freeze one research output and
    replay it through several writing configurations, so I'm not re-paying for expensive research
    on every replicate."
- **Human reviewer (could be the owner)** — calibrates and corrects the automated judge.
  - *US-4:* "As a reviewer, I open a simple web page listing evaluations awaiting my review, click
    into one, see the brief content next to the judge's per-criterion scores, rationale, and cited
    evidence, and quickly **agree or override** each score with an optional comment, then submit."
  - *US-5:* "As a reviewer, I can see how the automated judge's scores compare to what **real
    readers** said about the same editions, so I can tell whether the judge is well-calibrated and
    trust (or distrust) it accordingly."
- **Future optimization agent (data consumer, not built here)** — the machine reader.
  - *US-6:* "As a later automated agent, I read a structured, versioned eval record for each run —
    the configuration evaluated, per-criterion scores + rationale + evidence, any human overrides,
    the cost breakdown, and variance metadata — and reason about which pipeline change to propose,
    without re-deriving any of it from transcripts."
- **Architect / reviewer / security-engineer**
  - *US-7:* "As the Architect, I evaluate a custom AWS-native harness against adopting a
    self-hostable eval tool, write both up in an ADR with a recommendation, and get the human's
    sign-off before anyone builds."
  - *US-8:* "As a reviewer/security-engineer, I can verify the harness never sends eval email to
    real subscribers, never gates or slows the live daily send, reads the `brief-feedback` table
    without weakening its anonymity guarantee, and holds least-privilege IAM scoped to exactly
    what it needs."

## 4. Functional requirements

Numbered FR-N; each maps to acceptance criteria AC-N in §5. "The system shall …". These are
stated **architecture-agnostically** — they describe *what* the harness does, not *how* it is
built (that is the Architect's ADR, §7).

### A. What gets evaluated, and when

1. **Separate, deliberately-triggered evaluation runs (not the live send).** The harness shall
   evaluate briefs produced by **deliberately-triggered evaluation runs**, executed via the same
   replay / temporary-deployment mechanism already established for manual validation in
   `deploy/managed-agent/README.md` (§7 "Verify end-to-end" / the deployment-replacement flow) —
   **not** by folding evaluation into the live scheduled weekday production run. The live daily
   send shall never be gated on, slowed by, or made to depend on an evaluation. *(Decided, §7 —
   recommended and adopted: evaluation must never risk the real daily send, and this lets a **new**
   configuration be evaluated **before** it ever goes live.)*
2. **Candidate configuration is the unit of evaluation.** An evaluation shall target a named
   **candidate configuration** — the set of pipeline settings under test (in this epic, the current
   production configuration is the baseline; once the optimization epic exists, this includes which
   model/prompt is used per phase). The harness shall record which configuration each run
   evaluated (FR-16) so results are comparable across candidates over time.
3. **Replicates with a default of 3.** For a given candidate configuration, the harness shall
   support running **N replicate evaluation runs**, defaulting to **3**, and shall aggregate their
   per-criterion scores and cost with variance metadata (FR-14). *(Decided, §7: n=3 is the default
   because repeated-sampling confidence intervals narrow fast from n=1 to n=3 and only slowly
   after.)*
4. **Durable "candidates considered" research artifact.** The research phase shall emit a
   **durable, machine-readable artifact enumerating all stories/topics it identified and
   considered** during research — including, for each, enough to judge selection (e.g. a title/summary,
   its source, and whether it was ultimately included in or excluded from the final brief). This
   artifact shall be archived alongside the run's other outputs (analogous to how
   `brief_history.archive_todays_brief` archives `brief.md`/`brief.html`/`listening-script.txt`
   under `briefs/<date>/`), so the content-selection evaluation (FR-6) can contrast candidates
   considered vs. stories chosen without reconstructing them from a transcript. This is an
   **additive** artifact; it shall not alter the produced brief or the send. *(This touches
   `daily-ai-brief` **skill content** — see the three-way lockstep constraint in §6.)*
5. **Freeze-and-replay research.** The harness shall support **freezing a completed research
   phase's output** (including the FR-4 candidates artifact) and **replaying it through one or more
   writing-phase configurations**, so a writing-phase-only comparison does not re-run (and re-pay
   for) research. A frozen research output shall be reusable across all replicates of the
   writing-phase candidates being compared. *(Decided, §7: research is the expensive, rarely-changed
   phase; this is a real cost-saving property of the harness itself.)*

### B. Evaluation criteria (the quality axes)

**Status: trimmed to a v1 subset, decided by the owner 2026-07-04 after reviewing the full
candidate set below.** Each *kept* criterion is a distinct, independently-reported axis yielding a
score plus judge rationale plus supporting evidence/quotes (FR-15) — except where marked
**LLM-judge only**, meaning it is scored and reported but does **not** require a human
agree/override step in the review UI (FR-18/FR-19), to keep human review time proportionate.
Criteria marked **OUT OF SCOPE (v1)** are kept here, numbered, and reasoned rather than deleted, so
a later iteration can reintroduce them without renumbering.

6. **Content selection.** *(Kept — full human review.)* The harness shall judge selection quality
   by contrasting the **candidates considered** (FR-4) against the **stories actually chosen** for
   the final brief — flagging important stories that were dropped and low-value stories that were
   included.
7. **Factual accuracy / hallucination risk.** *(Kept — LLM-judge only, no human review step.)* The
   harness shall judge whether the brief's claims are **traceable to a fetched source**, flagging
   claims that appear unsupported or fabricated. Reported in the review UI for visibility but does
   **not** require a human agree/override action (FR-19) — checking source traceability by hand for
   every claim is disproportionate manual work for a human reviewer; the judge's flag is the
   actionable signal.
8. **OUT OF SCOPE (v1): Neutrality / tone drift.** Not selected for v1. The owner may reintroduce
   this later (e.g. once the cost-optimization epic starts changing prompts/models, a neutrality
   regression becomes more likely and worth catching); no artifact or mechanism from this epic
   depends on it, so it can be added without rework.
9. **Length / format compliance.** *(Kept — full human review.)* The harness shall judge the brief
   against the **skill's own stated target** for length and format (e.g. headline / deep-dive
   counts and structure as defined in the `daily-ai-brief` skill), flagging under- or over-shoot.
10. **OUT OF SCOPE (v1), replaced by a different idea: source-tier diversity.** Not selected for v1
    in its original form (judging the brief's source mix against `sources.md`'s tiering). Instead,
    the owner wants a **separate, later idea**: track which of the sources actually named in
    `sources.md` get featured in each generated brief over time, so that after a few days/weeks of
    real data, sources that are *never* featured can be identified and considered for removal from
    the skill's source list — reducing research cost (fewer sources to check) rather than judging
    diversity per se. This is **out of scope for this epic** — tracked as a separate GitHub issue
    (see §6) — and is **not** built here.
11. **Day-over-day deduplication.** *(Kept — LLM-judge only, no human review step.)* The harness
    shall verify that the brief does **not repeat stories from recent prior editions** — i.e. that
    the pipeline's existing "read recent prior briefs" dedup mechanism
    (`brief_history.read_recent_prior_briefs`) is working — flagging repeated stories. Reported for
    visibility; does **not** require a human agree/override action (FR-19) for the same
    proportionality reason as FR-7.
12. **OUT OF SCOPE (v1): Listening-script quality.** Not selected for v1. The owner's reasoning:
    this axis is really evaluating **Amazon Polly's narration**, not the LLM's writing output — a
    different concern from the rest of this epic (which is scoped to the LLM pipeline's cost and
    quality). If a future epic evaluates Polly/narration specifically, this criterion can be
    reintroduced then.
13. **OUT OF SCOPE (v1): Latency.** Not selected for v1. The owner's reasoning: wall-clock/
    active-seconds latency does not meaningfully matter for this application (an unattended
    overnight batch job with no user waiting on it in real time).
14. **Cost with full phase-level breakdown (first-class criterion, kept — the owner's original,
    firm requirement).** The harness shall capture and report the run's **cost broken down by
    phase — research vs. writing vs. delivery — from real Claude API token usage** (including the
    cache-read token burden that dominates cost), as **repeatable tooling** (not a one-off manual
    transcript mine). Cost shall be a **first-class eval criterion** reported per run and per
    candidate configuration, alongside the quality axes — not merely an appendix. Reported as
    computed data in the review UI; not a subjective judge score, so no agree/override applies.

**v1 criteria set: FR-6 (content selection), FR-7 (factual accuracy, LLM-judge only), FR-9
(length/format), FR-11 (dedup, LLM-judge only), FR-14 (cost breakdown).** FR-8, FR-10, FR-12, FR-13
are retained above as numbered, reasoned deferrals — not deleted — so later re-inclusion doesn't
require renumbering anything else in this document.

### C. Calibration against real reader feedback

15. **Reader-feedback calibration.** The harness shall use the existing `brief-feedback` DynamoDB
    table as a ground-truth signal: for editions that have **both** an automated evaluation and
    real reader submissions, it shall **correlate the LLM-judge scores against the real reader
    scores** on the corresponding axes (the 7 graded 1–5 ratings), and shall **surface reader
    free-text suggestions** (`additionalSources`, `otherFeedback`) into the review context so a
    reviewer can see what real readers asked for. Reading feedback shall honor the table's existing
    **anonymity handling** — the harness shall not attempt to de-anonymize any submission.

### D. Structured, versioned, machine-readable output

16. **Per-run structured record.** Every evaluation run shall produce a **structured, versioned,
    machine-readable record** capturing at least: (a) the **configuration evaluated** (which
    model/prompt per phase, once that dimension exists); (b) **per-criterion scores + judge
    rationale + supporting evidence/quotes** for the v1 criteria set (FR-6, FR-7, FR-9, FR-11); (c)
    any **human override** of a judge score and its optional comment (FR-19); (d) the **full cost
    breakdown** (FR-14); and (e) **variance metadata** across the replicates (FR-3/FR-17). The
    record's schema shall be **versioned** and **extensible**, so a deferred criterion (FR-8, FR-10,
    FR-12, FR-13) can be added later without breaking existing records or their consumers.
17. **Aggregation across replicates.** For a candidate configuration's replicate set, the harness
    shall produce an **aggregate record** with per-criterion central tendency and a **variance
    measure** (e.g. spread / confidence indication) across the replicates, so a consumer can judge
    result stability, not just a point estimate.

### E. Human review web interface

18. **Review UI — list and detail.** The harness shall provide an **easy, intuitive web interface**
    (not a CLI, not a spreadsheet) with, at minimum: (a) a **list view** of evaluations awaiting
    human review; and (b) a **detail view** for one evaluation showing the **brief content**
    (and its listening script) **side by side** with the judge's **per-criterion scores,
    rationale, and cited evidence**, plus the corresponding **real reader feedback** for that
    edition when available (FR-15).
19. **Review UI — agree / override / comment / submit.** From the detail view, a reviewer shall be
    able to, **per criterion**, **agree with** or **override** the judge's score and add an
    **optional comment**, then **submit** the review. A submitted override and its comment shall be
    persisted into that run's structured record (FR-16) as a human signal distinct from the
    automated judge score.
20. **Review UI — accessible, lightweight, non-public-facing posture.** The review UI shall meet
    the same lightweight/accessibility bar as the existing `deploy/subscribers/` and
    `deploy/feedback/` sites (labeled fields, keyboard-operable, readable on mobile, no heavy
    framework). Because it exposes internal eval data and accepts reviewer input, it shall **not**
    be an open, unauthenticated public write surface the way the feedback form is — its access
    posture (how the reviewer is gated) shall be specified by the Architect in the ADR (§7); this
    PRD requires only that internal eval data and the override-submit path are **not** exposed to
    the anonymous public.

### F. Least-privilege, isolation, and no impact on production

21. **Least-privilege IAM.** The harness's compute shall hold **least-privilege IAM** scoped to
    exactly what it needs: **read** access to the `brief-feedback` table and to the brief/candidates
    artifacts it evaluates, whatever **read/write** it needs on its **own** eval-record and
    review store, and the ability to **trigger an evaluation run** via the established replay /
    temporary-deployment mechanism. It shall **not** gain SES send rights to real subscribers, shall
    **not** be able to write to or delete from the `brief-feedback` table, and shall **not** hold
    static access keys.
22. **No impact on the live pipeline.** The harness shall **not** modify, gate, slow, or share
    mutable state with the live scheduled weekday production run and its fan-out. An evaluation
    (including any temporary deployment it creates) shall be isolated such that a failure or
    long-running eval never affects the real daily send, and evaluations shall **not** send email
    to real subscribers (FR-1, FR-22).
23. **Single-account, single-region (unless the ADR justifies otherwise).** The harness shall
    deploy into the same account/region as the rest of the repo (`740353583786`, `us-east-1`),
    consistent with this repo's single-account convention, **unless** the Architect's ADR
    identifies and justifies a real reason for account/environment separation from production and
    the human approves it. *(Decided default, §7: share the account; deviate only with a stated
    reason and human sign-off.)*

## 5. Acceptance criteria

Given/When/Then, testable against the harness, the pipeline's evaluation-run mechanism, the
`brief-feedback` table, and the IAM in account `740353583786`, `us-east-1`. *(Where an AC refers to
"the harness store" or "the review UI," it is satisfied by whichever backbone the Architect's ADR
selects — the criteria are architecture-agnostic.)*

### Trigger, replicates, replay
- **AC-1 (separate trigger, live send untouched):** Given the harness, When an evaluation is
  triggered, Then it runs via the deliberate replay / temporary-deployment mechanism, produces a
  brief that is **not** sent to real subscribers, and the live scheduled weekday deployment's
  schedule, output, and send are unchanged and un-gated by the evaluation (FR-1, FR-22).
- **AC-2 (candidate recorded):** Given an evaluation of a named candidate configuration, When it
  completes, Then the run record identifies which configuration was evaluated so it is comparable
  to other candidates (FR-2, FR-16).
- **AC-3 (3 replicates by default):** Given a candidate configuration with no replicate count
  specified, When an evaluation is run, Then **3** replicate runs are executed and their scores and
  cost are aggregated with variance metadata (FR-3, FR-17).
- **AC-4 (freeze-and-replay research):** Given a completed research phase whose output (including
  the candidates artifact) is frozen, When two or more writing-phase configurations are evaluated
  against it, Then research is **not** re-run for each — the frozen research output (and its cost)
  is reused across those runs (FR-5).

### Candidates artifact & criteria
- **AC-5 (candidates artifact archived):** Given an evaluation (or production) run, When research
  completes, Then a durable machine-readable "candidates considered" artifact enumerating all
  identified stories/topics (with source and included/excluded disposition) is archived alongside
  the run's brief artifacts, and its production does **not** change the brief that ships (FR-4).
- **AC-6 (content-selection judged from candidates):** Given the candidates artifact and the final
  brief, When content selection is evaluated, Then the harness reports whether important stories
  were dropped or low-value stories included, derived from the candidates-vs-chosen contrast
  (FR-6).
- **AC-7 (accuracy/hallucination, LLM-judge only):** Given a brief, When accuracy is evaluated,
  Then claims not traceable to a fetched source are flagged with rationale/evidence, reported for
  visibility with **no** human agree/override step required (FR-7, FR-15).
- **AC-8 — OUT OF SCOPE (v1).** Neutrality regression detection (FR-8) is deferred; not built or
  tested in this epic.
- **AC-9 (length/format):** Given a brief that under- or over-shoots the skill's stated
  length/format target, When evaluated, Then the deviation is flagged against that target (FR-9).
- **AC-10 — OUT OF SCOPE (v1), replaced.** Source-tier diversity scoring (FR-10) is deferred;
  replaced by a separate, later idea (per-brief source-usage tracking — see the GitHub issue
  referenced in §6) that is explicitly not part of this epic.
- **AC-11 (dedup verified, LLM-judge only):** Given a brief that repeats a story present in a
  recent prior edition, When evaluated, Then the repetition is flagged (verifying the "read recent
  prior briefs" dedup is working), reported for visibility with **no** human agree/override step
  required (FR-11).
- **AC-12 — OUT OF SCOPE (v1).** Listening-script quality as its own axis (FR-12) is deferred —
  it evaluates Polly narration, a different concern from this epic's LLM-pipeline scope.
- **AC-13 — OUT OF SCOPE (v1).** Latency capture (FR-13) is deferred; latency does not
  meaningfully matter for this unattended overnight batch application.
- **AC-14 (cost breakdown, repeatable):** Given an evaluation run, When it completes, Then a
  phase-level cost breakdown (research vs. writing vs. delivery, from real Claude API token usage
  including cache-read tokens) is recorded as a first-class criterion — produced by repeatable
  tooling, not a manual transcript mine (FR-14, FR-16).

### Calibration
- **AC-15 (feedback calibration):** Given an edition with both an automated evaluation and real
  reader submissions in `brief-feedback`, When calibration runs, Then the harness reports the
  correlation between judge scores and real reader scores on the corresponding axes and surfaces
  the reader free-text suggestions into the review context — **without** de-anonymizing any
  anonymous submission (FR-15).

### Structured output
- **AC-16 (structured, versioned record):** Given a completed evaluation run, When its record is
  inspected, Then it contains the evaluated configuration, per-criterion scores + rationale +
  evidence, any human override + comment, the full cost breakdown, and variance metadata, and
  carries a schema/format version (FR-16).
- **AC-17 (replicate aggregate + variance):** Given a candidate's replicate set, When aggregated,
  Then the aggregate record reports per-criterion central tendency **and** a variance measure
  across the replicates (FR-17).

### Review UI
- **AC-18 (list + side-by-side detail):** Given pending evaluations, When a reviewer opens the
  review UI, Then they see a list of evaluations awaiting review and can open a detail view showing
  the brief (and listening script) side by side with per-criterion judge scores, rationale, cited
  evidence, and the edition's real reader feedback when available (FR-18).
- **AC-19 (agree/override/comment/submit persists):** Given a detail view, When a reviewer agrees
  with or overrides a per-criterion score, adds an optional comment, and submits, Then the override
  and comment are persisted into that run's structured record as a human signal distinct from the
  judge score (FR-19, FR-16).
- **AC-20 (UI posture):** Given the review UI, When inspected, Then it meets the lightweight/
  accessibility bar of the existing sites and is **not** an open, unauthenticated public write
  surface exposing internal eval data (FR-20).

### IAM & isolation
- **AC-21 (least-privilege IAM):** Given the harness's execution role(s), When inspected, Then they
  grant only the reads needed (the `brief-feedback` table read; the brief/candidates artifacts) and
  the read/write on the harness's **own** store and the trigger of an evaluation run — with **no**
  SES send to real subscribers, **no** write/delete on `brief-feedback`, and **no** static access
  keys (FR-21).
- **AC-22 (production isolation):** Given an evaluation that fails or runs long, When it does so,
  Then the live scheduled weekday send is unaffected, no eval email reaches a real subscriber, and
  no mutable production state is shared or corrupted (FR-22).
- **AC-23 (account/region):** Given the deployed harness, When inspected, Then it is in account
  `740353583786`, `us-east-1` — unless the Architect's ADR justifies separation and the human
  approved it (FR-23).

## 6. Constraints & dependencies

*(Items below are settled decisions/facts for this epic — do not relitigate. The single deferred
decision is the build-vs-adopt backbone, §7.)*

- **AWS account** `740353583786`, region `us-east-1` — confirm the active account before any
  deploy. Single-account convention (FR-23) unless the ADR justifies otherwise.
- **v1 criteria scope is deliberately trimmed (§4.B).** Neutrality/tone drift (FR-8),
  listening-script quality (FR-12), and latency (FR-13) are deferred, not deleted — reintroducing
  any of them later is additive to the versioned record schema (FR-16), not a rework. Source-tier
  diversity (FR-10) is replaced by a different idea entirely (tracking which named `sources.md`
  sources actually get featured per brief over time, to identify prunable sources and cut research
  cost) — **tracked as a separate GitHub issue, not built in this epic.**
- **Architecture (build vs. adopt) is deferred to the Architect's ADR — implementation is
  BLOCKED until it lands and the human signs off.** The Architect must present **both** options —
  (a) a **custom AWS-native harness** mirroring `deploy/subscribers/` / `deploy/feedback/` (static
  review site + API Gateway + Lambda + DynamoDB) and (b) **adopting a self-hostable existing
  eval/observability tool** (e.g. Langfuse, Arize Phoenix) as the backbone — with a recommendation,
  and get the human's final sign-off. This is a **bigger-than-usual escalation** per this repo's
  standing convention that the Architect escalates major/irreversible decisions to the human. All
  FRs above are written architecture-agnostically so either backbone can satisfy them.
- **The "candidates considered" artifact touches `daily-ai-brief` SKILL content — three-way
  lockstep applies (ADR-0008).** FR-4 requires the research phase to emit a durable candidates
  artifact it does not produce today. To the extent this is achieved by changing the skill's
  research/output instructions (`deploy/managed-agent/skills/daily-ai-brief/SKILL.md` /
  `sources.md`), it is a **skill-content change** and therefore falls under
  **ADR-0008's three-way lockstep + confirmed live-version-push** procedure: the change must be
  mirrored across (1) the in-repo copy, (2) the local Desktop wrapper skill
  (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`), and (3) the live Skills API version
  (`skill_01H2qu83NwnJ5zqcbrqsCcJ6`), with the push **confirmed**, per ADR-0008 / README §3a. The
  Architect should decide whether the candidates artifact is emitted via skill-content instruction
  (lockstep-bound) or via the pipeline wrapper (e.g. `pipeline/`, not lockstep-bound) — favor
  the option that minimizes skill-content churn if it can capture the same candidates faithfully.
- **The candidates artifact must not change the shipping brief.** FR-4 is additive: it adds an
  archived artifact, not a change to the produced brief, its length, its selection, or its send.
  The Reviewer must confirm the brief a production run ships is unchanged by adding the artifact.
- **Reads the `brief-feedback` table — honors its anonymity model.** Calibration (FR-15) reads the
  feedback table defined in `reader-feedback.md` (7 graded 1–5 + 2 free-text, per-edition
  attribution, anonymity handling per ADR-0011). The harness must **read-only** that table and must
  **not** attempt to recover identity from anonymous submissions; the security review must confirm
  no de-anonymization path is introduced.
- **Uses the existing evaluation-run mechanism.** FR-1/FR-5 rely on the replay / temporary-
  deployment flow already documented in `deploy/managed-agent/README.md` (§6 "Create/update the
  scheduled deployment" — deployments are immutable, superseded by create-new-then-archive — and
  §7 "Verify end-to-end" manual-trigger flow). The harness triggers evaluation runs through that
  mechanism; it does **not** introduce a second, parallel way to run the pipeline that could drift.
  **Deployments are immutable** (README §6, confirmed live 2026-07-04) — a temporary eval deployment
  is a create-then-archive, not an in-place edit.
- **Cost data source is real Claude API token usage.** FR-14's phase-level breakdown must come from
  actual token accounting (the `span.model_request_end`-style events the owner mined by hand),
  turned into repeatable tooling — not an estimate. Whether that data is obtained from session
  transcripts, an observability integration, or an API depends on the ADR's backbone choice; the
  requirement is repeatability and phase attribution, not the mechanism.
- **Managed Agents beta.** The pipeline runs on the beta Managed Agents surface (self_hosted
  environment, microVM image, `deployment.json`, beta headers). The harness must not re-version the
  live agent or skill except as ADR-0008 requires for the FR-4 candidates artifact; it adds no
  change to the beta-pinned production deployment beyond that.
- **Credentials never committed** (repo convention). No Anthropic API key, AWS secret, or static
  access key in code, logs, or git; the harness uses ambient/role credentials as the rest of the
  repo does.
- **This is epic 1 of 2.** The follow-up cost-optimization epic depends on this harness but is
  **out of scope**. Nothing in this epic may pre-commit an optimization design.

## 7. Risks & open questions

- **[DECISION NEEDED — Architect, then human sign-off] Build a custom AWS-native harness vs. adopt
  a self-hostable eval/observability tool.** This is the single largest, most cross-cutting, hardest
  -to-reverse decision in the epic and is **deliberately not made in this PRD.** The Architect must
  produce an **ADR presenting both options with a recommendation**: (a) custom AWS-native (mirrors
  the repo's `deploy/subscribers/` / `deploy/feedback/` pattern — full control, consistent with
  existing IaC/IAM/deploy conventions, but everything is built and maintained in-house, including
  the judge orchestration, the review UI, cost accounting, and calibration); vs. (b) adopt a
  self-hostable tool such as **Langfuse** or **Arize Phoenix** as the backbone (traces, scoring,
  datasets, and often a review UI out of the box — potentially far less to build, but a new
  self-hosted dependency, its own operational/security surface, and possible impedance mismatch
  with the microVM/Managed-Agents run model and the repo's single-account CDK conventions). The
  ADR should weigh: build/maintenance cost, fit with the existing replay/temporary-deployment run
  mechanism, how cost/token data (FR-14) and calibration (FR-15) are captured in each, the review
  UI (FR-18/FR-19) each provides vs. requires building, IAM/least-privilege and data-anonymity
  posture (FR-21), and single- vs. multi-account footprint (FR-23). **Per this repo's standing
  convention, the Architect escalates this major/irreversible choice to the human for final
  sign-off before any implementation begins.**
- **[RESOLVED — PM] Live-integrated eval vs. separate triggered eval.** Decided: **separate,
  deliberately-triggered evaluation runs** (FR-1), using the existing replay/temporary-deployment
  mechanism. Rationale: evaluation adds cost and latency and can fail; folding it into the live
  weekday send would let an eval failure or slowdown jeopardize the real subscriber-facing send,
  for no benefit — and a separate trigger additionally lets a **new** candidate configuration be
  evaluated **before** it ever goes live (the whole point, since the follow-up epic will test
  configurations that must not ship unmeasured). Recorded as a settled product decision, not
  deferred.
- **[RESOLVED — PM] Account/environment separation.** Decided: **share the production account/
  region** (`740353583786`, `us-east-1`) by default (FR-23), matching this repo's single-account
  convention for everything to date. Deviate only if the Architect's ADR identifies a concrete
  reason (e.g. a self-hosted tool's blast-radius or data-isolation needs) and the human approves.
  Isolation from the *live pipeline* (FR-22) is required regardless and is achievable within one
  account.
- **[RESOLVED — PM] Review UI shape.** Decided (product-level, FR-18/FR-19): the reviewer flow is
  **list of pending evals → detail view (brief + listening script side by side with judge scores/
  rationale/evidence and real reader feedback) → per-criterion agree/override + optional comment →
  submit**. Stated as UX requirements, not visual design; the Architect/Developer choose the
  concrete implementation (and the reviewer-gating posture, FR-20) within the selected backbone.
- **[RESOLVED — PM] Default replicate count = 3, plus freeze-and-replay.** Decided (FR-3/FR-5):
  n=3 replicates by default (CIs narrow fast from n=1→3, slowly after), and the harness supports
  freezing research output and replaying it through multiple writing configurations to avoid
  re-paying for research. Both are settled per the owner's stated requirements.
- **[RESOLVED — owner, 2026-07-04] v1 eval criteria set, trimmed from the full candidate list.**
  The owner reviewed the full nine-criterion candidate set (content selection, factual
  accuracy/hallucination, neutrality/tone drift, length/format compliance, source-tier diversity,
  day-over-day dedup, listening-script quality, latency, cost) and selected a **v1 subset** to
  avoid over-engineering the first build: **content selection (FR-6, full human review), factual
  accuracy/hallucination (FR-7, LLM-judge only), length/format compliance (FR-9, full human
  review), day-over-day dedup (FR-11, LLM-judge only), and cost with a full phase-level breakdown
  (FR-14, first-class, the owner's original firm requirement)**. Neutrality/tone drift (FR-8),
  listening-script quality (FR-12), and latency (FR-13) are deferred (see §4.B for reasoning per
  criterion); source-tier diversity (FR-10) is replaced by a different, later idea (source-usage
  tracking to identify prunable sources — tracked as a separate GitHub issue, not this epic). All
  deferred FRs are retained, numbered, and reasoned in §4.B rather than deleted, so a later
  iteration can reintroduce any of them without renumbering.
- **Candidates-artifact fidelity (design risk, for the Architect/Developer).** Content-selection
  evaluation (FR-6) is only as good as the candidates artifact (FR-4). If the research phase
  under-reports what it considered (e.g. only lists what it nearly-included), "important story
  dropped" detection weakens. The Architect/Developer should ensure the artifact captures a
  genuinely broad candidate set, and the Reviewer should test it against a known-dropped-story
  fixture (AC-6). Flagged as a fidelity risk, not a blocker.
- **LLM-judge reliability / cost.** The judge is itself an LLM and can be miscalibrated or add
  non-trivial cost. Mitigations already in scope: replicates + variance (FR-3/FR-17) expose judge
  instability; human override (FR-19) corrects it; and calibration against **real** reader feedback
  (FR-15) is the ground-truth check on whether the judge can be trusted at all. The judge's own
  cost should be reported so it isn't confused with pipeline cost.
- **Feedback is sparse and slow.** Calibration (FR-15) depends on real reader submissions, which
  arrive slowly and only for shipped editions — so early on, correlation will be thin. This is
  expected; the harness must degrade gracefully (report "insufficient feedback to calibrate" rather
  than a spurious correlation) and improve as feedback accumulates. Not a blocker; a data-maturity
  note.
- **Skill-content lockstep drift (ADR-0008).** If FR-4 is implemented as a skill-content change,
  the three-way lockstep + confirmed live-version push must be honored or the live brief and the
  eval-run brief could diverge (one emits candidates, one doesn't). The Developer must follow
  ADR-0008 exactly; the Reviewer must confirm all three artifacts moved together. Prefer emitting
  the artifact in the pipeline wrapper over the skill if it avoids lockstep-bound churn (§6).
- **Scope-creep into the optimization epic.** The strong temptation is to start "fixing" cost while
  building the measurement for it. This epic must ship the **harness only**; any pipeline/cost
  change belongs to epic 2 and must not be smuggled in here (§2 non-goal).

## 8. Rollout & metrics

- **Handoff (gate 0): Architect ADR + human sign-off — BEFORE any build.** The Architect writes the
  build-vs-adopt ADR (§7) presenting both options with a recommendation; the human signs off on the
  backbone. **No harness implementation starts until this lands.** The Architect also decides where
  the FR-4 candidates artifact is emitted (skill-content, ADR-0008-bound, vs. pipeline wrapper) and
  the reviewer-gating posture (FR-20).
- **Phasing (after gate 0).**
  1. **Candidates-considered artifact (FR-4).** Add the durable candidates artifact to the research/
     archival path, archived alongside the existing brief artifacts. If skill-content, follow
     ADR-0008's three-way lockstep + confirmed live push. Backward-compatible and additive: the
     shipping brief is unchanged; runs before this simply have no candidates artifact.
  2. **Cost tooling (FR-14).** Turn the one-off transcript-mined cost analysis into repeatable
     phase-level (research/writing/delivery) token-usage tooling, per the ADR's backbone.
  3. **Judge + criteria — v1 subset (FR-6, FR-7, FR-9, FR-11).** Implement the automated scoring
     across the four kept quality axes (content selection, factual accuracy, length/format, dedup),
     each emitting score + rationale + evidence. FR-8/FR-10/FR-12/FR-13 are not implemented in this
     epic.
  4. **Structured record + replicates + freeze/replay (FR-16/FR-17/FR-3/FR-5).** Versioned per-run
     records, replicate aggregation with variance, and the freeze-research-and-replay-writing
     mechanism.
  5. **Calibration (FR-15).** Wire in read-only `brief-feedback` correlation + free-text surfacing,
     honoring anonymity.
  6. **Review UI (FR-18..FR-20).** The list → side-by-side detail → agree/override/comment → submit
     flow, gated per the ADR's reviewer-access posture, persisting overrides into the record.
  7. **Validate.** Trigger an evaluation of the **current production configuration** as the baseline;
     confirm the live daily send is untouched (AC-1/AC-22); run 3 replicates and see variance
     (AC-3); freeze research and replay two writing configs (AC-4); confirm each **v1** criterion
     produces a score+rationale+evidence (AC-6, AC-7, AC-9, AC-11, AC-14 — AC-8/AC-10/AC-12/AC-13
     are out of scope and need no validation); confirm calibration against a real edition with
     feedback (AC-15); confirm a human review override persists (AC-19); security-review the IAM and
     the anonymity no-de-anon guarantee (AC-21/AC-15).
- **Ship gate.** Gate 0 (ADR + human sign-off) passed; AC-1..AC-7, AC-9, AC-11..AC-23 pass (AC-8,
  AC-10, AC-12, AC-13 are out of scope for v1 and excluded from the ship gate); the security review
  confirms least-privilege IAM (no SES-to-subscribers, no write on `brief-feedback`, no static
  keys — AC-21), production isolation (AC-22), and that reading feedback introduces no
  de-anonymization path (AC-15); and the FR-4 candidates artifact is confirmed **not** to change
  the shipping brief.
- **Success metric.** After ship: the owner can trigger an evaluation of any candidate
  configuration and get, per run, the **v1 criteria set's** scores + rationale + evidence (content
  selection, factual accuracy, length/format, dedup — the last two LLM-judge only), a full
  phase-level cost breakdown, and (default) 3-replicate variance — all in a versioned,
  extensible machine-readable record — plus an easy web review to agree with or override the
  human-reviewed criteria; automated judge scores can be correlated against real reader feedback on
  shared editions; and there is **zero**
  regression to the live daily send, the fan-out, the feedback surface's anonymity, or the brief's
  content/audio/schedule. Concretely, this epic is a success when the **next** (cost-optimization)
  epic can measure a candidate change against a real quality baseline instead of shipping on vibes —
  which is the entire reason this harness exists.
