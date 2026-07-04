# 0013. Evaluation-harness backbone: custom AWS-native build vs. adopting a self-hostable eval tool

- Status: **Accepted (2026-07-04) — human signed off on Option A (build custom AWS-native).**
  Per PRD `docs/prd/eval-harness.md` §6/§7 gate 0, this backbone choice was escalated to the human
  as a bigger-than-usual, hard-to-reverse decision; the human reviewed both options and the
  Architect's recommendation and approved **Option A**. Harness implementation may now proceed. The
  sub-decisions in §D (candidates-artifact emission: skill-content, ADR-0008-bound) and §E
  (reviewer gating: shared-secret bearer key) stand as resolved by the Architect and did not
  themselves require escalation.
- Date: 2026-07-04
- Deciders: architect (Claude, recommendation); **human (final sign-off on Option A)**

## Context

The daily AI brief pipeline (`deploy/managed-agent/`) costs ~$2.60–2.65/run, dominated by
cache-read tokens, with the post-research writing/delivery phase (~4.2M) costing *more* than
research (~1.2M). The owner wants to optimize that cost in a **later epic**, but has no way to
prove a cheaper pipeline didn't degrade quality. PRD `eval-harness.md` (this epic, epic 1 of 2)
builds the **measurement infrastructure only**: an evaluation harness that scores a brief-run
across nine quality axes plus a first-class phase-level cost breakdown, calibrates automated
LLM-judge scores against real `brief-feedback` reader data, gives a human an easy web review to
agree/override each per-criterion score, and emits a structured, versioned, machine-readable
record per run for a future optimization agent to consume.

The PRD is fully resolved on **every product-level question** (trigger model, replicate count,
freeze-and-replay, the full criteria set, account/region default, review-UI reviewer flow — all
"[RESOLVED — PM]"). It **deliberately defers one decision to this ADR**: the harness's
architectural backbone.

**Option A — custom AWS-native harness.** A new `deploy/eval/` CDK app modelled as a sibling of
`deploy/subscribers/` and `deploy/feedback/`: a DynamoDB table (or two) for eval + review records,
Lambda(s) for the judge orchestrator and cost/token miner, an HTTP API, and a private-bucket +
OAC CloudFront static review site — every construct pattern already proven twice in this repo
(HTTP API + locked CORS + throttled `$default` stage, `certificateArn`/domain context with a
default fallback, per-function least-privilege roles scoped by ARN, `cdk.json` context conventions,
a README runbook). Everything — judge orchestration, review UI, cost accounting, calibration — is
built and maintained in-house.

**Option B — adopt a self-hostable eval/observability tool** (Langfuse or Arize Phoenix) as the
backbone. Both are open-source, self-hostable, and provide LLM-as-judge scoring, a human
annotation UI, and datasets out of the box — potentially far less to build, at the cost of a new
self-hosted dependency with its own operational and security surface, and an impedance mismatch
with this repo's microVM/Managed-Agents run model, its serverless single-account CDK conventions,
and (critically) the **domain-specific** concepts the PRD requires that no generic eval tool
models.

Whatever is chosen must satisfy FR-1…FR-23 architecture-agnostically; fit the existing
replay/temporary-deployment run mechanism (`deploy/managed-agent/README.md` §6/§7 — deployments
are **immutable**, changed only by create-new-then-archive); capture phase-level cost/token data
(FR-14) mined from Managed Agents **session transcripts** (`span.model_request_end`-style
`usage` events — *not* from a standard LLM-calling SDK the tool would auto-instrument); calibrate
read-only against `brief-feedback` with **no de-anonymization path** (FR-15/FR-21); hold
least-privilege IAM with no SES-to-subscribers, no `brief-feedback` writes, no static keys
(FR-21); and default to single-account `740353583786` / `us-east-1` (FR-23).

### What I researched (Option B, for real — 2026-07-04, live docs)

Fetched Langfuse and Arize Phoenix documentation directly (`langfuse.com/docs`,
`arize.com/docs/phoenix`) across the five dimensions the PRD flagged:

- **(a) LLM-as-judge with custom rubrics — both YES.** Langfuse ("LLM-as-a-Judge": present input +
  output + a scoring rubric, judge returns numeric/categorical/boolean score + reasoning; custom
  criteria supported). Phoenix (LLM-as-a-judge evaluators against a rubric, client-side SDK evals
  **and** server-side UI evals). Neither is a limitation.
- **(b) Human review/annotation UI — both have one, but neither matches FR-18's exact side-by-side
  shape out of the box.** Langfuse: "Scores via UI" + Annotation Queues — open a trace/observation,
  click **Annotate**, pick score configs, set values, add a comment. Phoenix: annotation configs
  (Categorical/Continuous/Freeform rubrics) + annotate-in-the-UI on a trace. Both give
  *per-criterion score + comment on a record* — the agree/override/comment/submit core of FR-19.
  **But** both render a **trace/observation** detail, not the PRD's bespoke "brief content + its
  listening script **side by side** with judge scores/rationale/evidence **and** the edition's real
  reader `brief-feedback`." That composite view — especially surfacing external `brief-feedback`
  next to the judge — is **not** a native layout; it would require cramming brief text, script,
  evidence, and reader feedback into span attributes/metadata and accepting the tool's generic
  trace-detail rendering, or building a custom view anyway.
- **(c) Self-hosting on AWS — materially different footprints.**
  - **Phoenix**: an "all-in-one" single container (UI + OTLP collector on ports 6006/4317).
    SQLite for dev; **Postgres** for production. Documented AWS path is **Fargate via
    CloudFormation** (single service + RDS/Aurora Postgres). Free to self-host, no feature gates.
    Relatively light: one long-running container + a managed Postgres.
  - **Langfuse**: **multi-container** — Langfuse Web **and** Langfuse Worker, **plus** Postgres,
    **plus ClickHouse** (OLAP store for traces/scores), **plus Redis/Valkey**, **plus an S3/blob
    store**. Documented production AWS path is **Terraform** (not CDK), K8s/Helm, or Railway. Some
    features are gated behind an EE license key. This is a substantially heavier standing footprint
    than anything in this repo.
  - Both are **always-on** services (a trace collector + web UI that must be up to receive data and
    to review) — fundamentally unlike this repo's **serverless, pay-per-use** shape, where nothing
    runs between the weekday send and a deliberate eval trigger.
- **(d) Ingesting an EXTERNAL per-phase cost/token breakdown — technically possible in both, but
  not their happy path.** Langfuse tracks usage/cost on `generation` observations and accepts
  **arbitrary usage types** (`cached_tokens`, etc.) ingested "via API, SDKs or integrations" — so
  our mined `span.model_request_end` `usage` (including cache-read) *can* be pushed as manual
  observations. Phoenix is OpenTelemetry/OpenInference-native: cost/usage lives on spans, and we
  could emit our own OTLP spans carrying token counts. **However**, both tools' cost tracking is
  designed to auto-capture usage from an LLM SDK call *the tool itself instruments*. Our cost data
  is the opposite: **post-hoc, mined from Managed Agents Sessions API transcripts** with **no live
  SDK call inside the tool**. So in either tool we still write the entire miner ourselves and then
  **shim** its output into the tool's observation/span schema — the tool provides a viewer, not the
  capture. The hard part (the repeatable transcript miner, FR-14) is ours to build in **every**
  option.
- **(e) Domain concepts — neither models them; custom scripting required regardless.** The PRD's
  "**candidates considered vs. chosen**" content-selection contrast (FR-4/FR-6) and the
  "**freeze research and replay the writing phase**" mechanism (FR-5) are **not** first-class
  concepts in Langfuse or Phoenix. Both think in traces/spans/scores/datasets/experiments —
  generic primitives. Freeze-and-replay is a property of **how we trigger the pipeline** (the
  immutable create-new-then-archive deployment flow), which lives entirely outside either tool; the
  candidates artifact is produced by the **research phase**, archived to **S3**, and read by our
  own content-selection judge — the tool would at most display the resulting score. Adopting a tool
  does **not** remove the need to build these; it only changes where the *score* is stored.

## Decision (recommended — subject to human sign-off)

**Recommendation: Option A — build a custom AWS-native harness as a new `deploy/eval/` CDK app,
modelled as a sibling of `deploy/subscribers/` and `deploy/feedback/`.** Do **not** adopt Langfuse
or Phoenix as the backbone.

This is a recommendation the human must ratify (gate 0), not a settled decision. The reasoning:

1. **The load-bearing, hard parts are ours to build in *either* option, so a tool saves little.**
   The transcript-mined phase-level cost miner (FR-14), the candidates-vs-chosen selection judge
   (FR-4/FR-6), the accuracy and dedup judges (FR-7/FR-11 — the v1 criteria set, trimmed by the
   owner from a larger candidate list; neutrality-regression, FR-8, is deferred past v1), the
   calibration join against `brief-feedback` (FR-15), and the freeze-and-replay trigger (FR-5) are
   all
   **domain-specific and external to any generic eval tool**. Langfuse/Phoenix would give us an
   LLM-judge runner and a generic annotation UI — but we still write every judge prompt, the miner,
   the calibration, and the replay orchestration ourselves, then translate them into the tool's
   span/observation schema. The tool becomes a **viewer with a schema tax**, not a shortcut.

2. **The out-of-the-box review UI does *not* match FR-18's required layout anyway.** The one place a
   tool most plausibly saves work — the review UI — is exactly where the fit is weakest: neither
   renders "brief + listening script side by side with judge scores/rationale/evidence **and** the
   edition's real reader feedback." We'd be pushing brief text, script, evidence, and external
   `brief-feedback` into span metadata and accepting a generic trace view, or building a custom view
   regardless. Against a **single-owner-reviewer** workflow, a small static review site (the exact
   pattern `deploy/subscribers/site/` and `deploy/feedback/site/` already prove — vanilla JS, an
   HTTP API, a private bucket behind CloudFront) is *less* work than bending a trace-detail UI to
   this shape, and it renders precisely the composite the reviewer needs.

3. **Cost of the harness itself — a tool ironically adds a bigger *fixed* cost than the pipeline it
   optimizes.** The entire parent effort exists to shave a ~$2.60/run bill. Option A is
   **serverless, pay-per-use**: DynamoDB on-demand, Lambda per-invocation, CloudFront/S3 for a tiny
   static site, an occasional judge LLM call — effectively **$0 when idle**, and idle is the normal
   state (nothing runs between a weekday send and a deliberate eval trigger). Option B requires an
   **always-on** service: Phoenix ≈ one Fargate task + an RDS/Aurora Postgres running 24/7
   (order ~$30–60+/month floor); Langfuse ≈ Web + Worker containers **plus** Postgres **plus
   ClickHouse** **plus** Redis **plus** blob storage (a materially higher standing bill, and an
   operational burden — patching, upgrades, backups of multiple stores). A standing monthly floor
   that rivals or exceeds a month of pipeline runs, to measure a per-run cost, is the wrong
   trade for a personal single-owner project.

4. **Architecture fit and least-privilege.** Option A slots into the repo's proven conventions:
   per-function IAM scoped by ARN (the `sid=`-tagged `PolicyStatement` pattern already used across
   both sibling stacks), `cdk.json` context keys with default fallbacks, single-account
   `us-east-1`, no static keys, `RemovalPolicy.RETAIN` on real data. FR-21's exact grants
   (read-only on `brief-feedback` — **no** write/delete; read on brief/candidates artifacts;
   read/write only on the harness's own store; trigger an eval via the existing mechanism; **no**
   SES-to-subscribers) are expressed directly as scoped statements the security-engineer can read
   the same way they read the subscribers/feedback roles. A self-hosted tool inverts this: it wants
   broad read over whatever traces it ingests, runs long-lived compute, and adds a **new public-ish
   web surface** (its own UI + auth) whose blast radius and de-anonymization posture we'd have to
   reason about separately — a bigger security surface for a one-reviewer flow.

5. **Reversibility is *better* under Option A here.** Counter-intuitively for a "build," Option A is
   the more reversible door: the structured, versioned per-run record (FR-16) is **our schema in
   our DynamoDB/S3**, portable by design. If we ever outgrow the custom review UI, we can *later*
   push those records into Langfuse/Phoenix as an add-on viewer. Adopting a tool now bakes its
   span/observation data model into how every judge, the miner, and calibration must emit — a
   harder thing to walk back, and a standing dependency on a beta-adjacent OSS surface.

**Where Option B could still win, stated honestly (for the human's decision):** if the owner
expects the harness to grow into **continuous production tracing** of every live run (not just
deliberate evals), or a **multi-user** review team, or wants trace-waterfall/experiment-diff UIs
"for free," a tool amortizes better and Phoenix (the lighter of the two — single container +
Postgres, Fargate/CloudFormation path, no feature gates) would be the one to pick. None of those
are in this epic's scope or the PRD's stated use (single owner-reviewer, deliberate eval runs,
epic-1 measurement only) — which is precisely why the recommendation is **build (Option A)**.

### Can Option A satisfy every FR? Yes — with two honestly-noted realities (not gaps)

Confirmed FR-1…FR-23 are all satisfiable by Option A. Two things are real *work items*, not
capability gaps, and would be equally true under Option B:

- **FR-14 (cost miner) is genuine new tooling in every option.** No backbone captures the
  transcript-mined cache-read phase breakdown for us; Option A writes it as a Lambda that reads the
  Sessions API `usage`/`span.model_request_end` events and attributes them to research/writing/
  delivery. This is the epic's hardest single piece and is backbone-independent.
- **FR-4 candidates artifact requires a small skill-content addition (see §D) — lockstep-bound.**
  This is a genuine limitation *of the requirement*, not of Option A, and it is equally required if
  Option B were chosen (the tool cannot invent a candidate list the research phase never emits).

Both are surfaced as consequences below, not buried.

## Sub-decisions resolved by the Architect (independent of the backbone choice)

### D. Where the FR-4 "candidates considered" artifact is emitted — **skill-content change (ADR-0008 lockstep-bound)**, not a wrapper-only trick

**Recommendation: emit the candidates artifact via a small, explicit addition to the
`daily-ai-brief` skill's output contract — accepting that this is a skill-content change and
therefore falls under ADR-0008's three-way lockstep + confirmed live-version push.** Do **not**
pretend the pipeline wrapper alone can capture it.

Reasoning: the PRD's stated preference is to avoid lockstep churn "if you can capture the same
candidates faithfully without touching the skill." Examined honestly, you **cannot**. The
**skill** is what actually performs the research and decides what to include or exclude; today the
pipeline wrapper (`deploy/managed-agent/pipeline/`) only ever sees the skill's **final output**
(the finished brief + listening script), never the broader set of stories the skill considered and
**rejected**. A wrapper cannot reconstruct "all stories/topics considered" from a brief that, by
definition, contains only the *chosen* ones — the rejected candidates are exactly the information
that never reaches the wrapper. FR-6's whole value (catching an *important dropped* story) depends
on that rejected set, which only the skill has. So the skill's output contract must be extended
with a small instruction: **as part of its output, emit a durable, machine-readable list of every
story/topic it considered — title/summary, source, and included/excluded disposition** — which the
pipeline wrapper then archives to S3 alongside `brief.md`/`brief.html`/`listening-script.txt` under
`briefs/<date>/` (mirroring `brief_history.archive_todays_brief`, an additive artifact that does
**not** change the shipping brief). The **archival** is wrapper work (not lockstep-bound); the
**emission of the candidate list** is skill work (lockstep-bound).

Concretely this means the Developer must follow ADR-0008 exactly for the skill edit: (1) edit the
in-repo `deploy/managed-agent/skills/daily-ai-brief/{SKILL.md,sources.md}` first; (2) mirror the
same content addition into the local Desktop wrapper skill
(`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`, skill-content sections only); (3) validate;
(4) push a new Skills-API version (`skill_01H2qu83NwnJ5zqcbrqsCcJ6`, `skills-2025-10-02` header per
README §3a); (5) **confirm** `latest_version` resolves to it; (6) record the version id. The
Reviewer must confirm all three artifacts moved together **and** that adding the candidate-emission
instruction did **not** change the brief that ships (the artifact is additive). This is more churn
than a wrapper-only answer, but it is the only faithful one — flagged plainly rather than forced.

### E. Reviewer-gating posture for the review UI (FR-20) — **a shared-secret bearer key the review Lambda checks, wired as CloudFront + API-key**

**Recommendation: gate the review UI and its write API with a single shared secret** (a random
bearer token), held in Secrets Manager, required on every review-API request and to load the
review page — the simplest mechanism that satisfies "not an open, unauthenticated public write
surface" without over-engineering a one-person workflow. Concretely:

- The review **write API** (submit override/comment) requires the secret as a header (or a signed
  bearer value); the submit Lambda compares it with `hmac.compare_digest` against the
  Secrets-Manager-held value (the exact constant-time compare + ARN-scoped `GetSecretValue` pattern
  ADR-0011/0003 already establish). No secret ⇒ 401. This alone satisfies FR-20's hard requirement
  (the override-submit path is **not** open to the anonymous public).
- The static review **site** carries no data itself; it fetches eval records from a read API that is
  gated by the **same** secret (the reviewer pastes/stores it once, or it rides in a bookmarked
  `?k=` param the page keeps in `sessionStorage`). Internal eval data is therefore never served to
  an unauthenticated caller.
- Rejected as over-engineering for one reviewer: **Cognito** (a full user pool + hosted UI for a
  single human is disproportionate), **IAM/SigV4** (forces the reviewer to sign requests or use the
  CLI — fails the PRD's "easy web interface, not a CLI" bar), **CloudFront signed cookies/URLs**
  (key-pair + rotation machinery heavier than a bearer secret), and an **IP allowlist** (brittle
  for an owner on changing networks/mobile, and not a real authN). A shared secret is the same
  weight as this repo's existing HMAC-secret conventions, rotates with one `put-secret-value`, and
  is exactly proportionate to a personal single-reviewer surface. If the review team ever grows
  beyond the owner, revisit with Cognito — a contained, reversible change.

*(If the human ultimately picks Option B, FR-20 is instead satisfied by the adopted tool's own
authentication — Phoenix/Langfuse OAuth2/local accounts — behind a private ALB/CloudFront; the
shared-secret scheme above is the Option-A answer.)*

### Account/region (FR-23) — **confirm single-account `740353583786` / `us-east-1`, no deviation**

Under **Option A** there is no reason to deviate: the harness is serverless, isolated from the live
pipeline by construction (its own table/bucket/roles; it never shares mutable production state,
never gains SES-to-subscriber rights, and triggers evals only via the existing immutable
create-new-then-archive deployment mechanism, FR-22), and reads `brief-feedback` read-only. Every
sibling stack in this repo is single-account `us-east-1`; the eval harness stays there. *(Only
under Option B would account separation even be worth debating — a long-lived self-hosted tool with
a broad ingest surface is the kind of thing one might isolate — and even then the PRD's default is
"share the account unless justified." Since the recommendation is Option A, single-account stands
with no deviation.)*

## Alternatives considered

- **Option B(i): Adopt Arize Phoenix as the backbone.** The *lighter* of the two tools: single
  all-in-one container + Postgres, documented Fargate/CloudFormation AWS path, OpenTelemetry/
  OpenInference native, free with no feature gates, has LLM-judge + UI annotation. Rejected as the
  recommendation because: it is **always-on compute** (Fargate + RDS/Aurora, a standing monthly
  floor) against a serverless repo whose whole point is cost frugality; its trace-detail UI does
  **not** render FR-18's brief+script+judge+reader-feedback side-by-side layout without custom work;
  our cost data is post-hoc transcript-mined (not SDK-auto-captured), so the miner is ours to build
  regardless; and the domain concepts (candidates-vs-chosen, freeze-and-replay) are external to it.
  It buys a generic judge-runner and viewer we mostly don't need, at a fixed cost we'd rather not
  pay. **Would become the pick** only if the epic expanded to continuous production tracing or a
  multi-user team — neither is in scope.

- **Option B(ii): Adopt Langfuse as the backbone.** Richest feature set (LLM-judge, annotation
  queues, datasets, experiments, cost tracking with arbitrary usage types incl. `cached_tokens`).
  Rejected more firmly than Phoenix: its self-host footprint is **heavy** (Web + Worker + Postgres +
  ClickHouse + Redis + S3/blob store), its documented production AWS path is **Terraform**, not this
  repo's CDK, some features sit behind an **EE license key**, and the operational burden (five
  stateful/stateless components to run, patch, back up) is wildly out of proportion to a
  single-owner eval harness. The same "we still build the miner, judges, calibration, replay, and
  the real review layout ourselves" objection applies, on top of the heaviest ops surface of the
  three options.

- **Option A but nested inside `deploy/feedback/` or `deploy/subscribers/`** (share their CDK app /
  common layer / CI). Rejected for the same reason ADR-0012 kept feedback standalone: the eval
  harness is a distinct deploy lifecycle with its own IAM, its own store, and — unlike the public
  feedback form — a **gated** surface. It gets its own `deploy/eval/` app, sibling to the other two,
  sharing **no** role or resource with them (it only **reads** `brief-feedback` cross-stack, granted
  by ARN, same-account, exactly as the welcome-send role reads the `cowork-polly-tts` bucket
  cross-stack in ADR-0009).

- **Wrapper-only candidates artifact** (avoid the ADR-0008 skill edit entirely). Rejected as
  infeasible: the pipeline wrapper never sees the stories the skill *rejected*, and FR-6 depends on
  exactly that rejected set. Faithful capture requires the skill to emit its candidate list (§D).

- **Cognito / IAM-SigV4 / CloudFront signed URLs for review gating.** Rejected as over-engineering
  for a single reviewer (§E); a shared bearer secret matches this repo's existing HMAC-secret
  conventions and the "easy web interface, not a CLI" bar.

## Consequences

Positive (if the human ratifies Option A):
- The harness is a clean serverless sibling of `deploy/subscribers/` / `deploy/feedback/` — same
  CDK patterns, same per-function ARN-scoped least-privilege IAM, same `cdk.json` conventions, same
  README runbook style — so the Developer builds by mirroring, not designing anew, and the
  security-engineer reviews IAM the way they already review the sibling stacks.
- **Effectively $0 when idle**, pay-per-use when triggered — the cost posture matches the frugality
  that motivates the whole effort, with no always-on tool floor.
- The structured versioned record (FR-16) is **our portable schema**, not locked into a tool's
  span/observation model — leaving Option B available *later* as an add-on viewer if needs grow.
- Single-account `us-east-1`, no new public tool surface, read-only on `brief-feedback` with no
  de-anonymization path — a small, auditable security footprint.

Negative / follow-ups (true under either backbone unless noted):
- **We build the review UI, judge orchestration, cost miner, and calibration ourselves** — more
  in-house code than adopting a tool *appears* to promise (though, per §Decision, the tool's real
  savings are small and its review-UI fit is poor). Mitigated by heavy reuse of the two sibling
  stacks' proven static-site + HTTP-API + DynamoDB patterns.
- **FR-4 forces an ADR-0008 lockstep skill-content change** (§D) — three-artifact sync + confirmed
  live version push, with its silent-drift failure mode. Unavoidable for faithful candidate capture;
  the Developer must follow ADR-0008 exactly and the Reviewer must confirm all three moved and the
  shipping brief is unchanged.
- **The FR-14 cost miner is genuinely new tooling** against the beta Sessions API transcript shape
  (`span.model_request_end`/`usage`), which may drift; the miner should be written defensively and
  its assumptions documented, exactly as the managed-agent build documented its beta-API confirms.
- **Reversible.** A standalone `deploy/eval/` CDK app with `RETAIN` on its real data store is torn
  down or re-pointed in a contained change; nothing here is a one-way door, and the portable record
  schema keeps a future migration to Langfuse/Phoenix open.

## Verification note

Option B research was done against **live documentation** on 2026-07-04 (`langfuse.com/docs` and
`arize.com/docs/phoenix`, fetched and read directly — LLM-judge/rubric support, annotation-UI shape,
self-host footprints incl. Langfuse's Postgres+ClickHouse+Redis+S3 multi-container stack vs.
Phoenix's single-container+Postgres Fargate/CloudFormation path, arbitrary-usage-type cost
ingestion, and the absence of native "candidates-vs-chosen"/"freeze-and-replay" concepts), because
`mcp__aws-docs` was not available in this session (same situation the managed-agent build recorded).
Option A rests entirely on construct patterns **already deployed** in this repo's
`deploy/subscribers/` and `deploy/feedback/` stacks (HTTP API + locked CORS + throttled stage,
private-bucket + OAC CloudFront + `BucketDeployment`, ARN-scoped per-function IAM,
`certificateArn`/domain context), so no `aws-docs` lookup gated it. **This ADR is Proposed pending
the human's Option-A-vs-B sign-off (gate 0); implementation must not begin until that sign-off is
recorded and this ADR's Status is updated to Accepted with the human named as a decider.**
