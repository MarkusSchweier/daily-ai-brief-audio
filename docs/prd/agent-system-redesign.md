# PRD: Agent-system redesign — decouple content generation from AWS delivery

- Status: **Draft (rev. 2, 2026-07-06) — architecture decisions deferred to the Architect's ADR
  (Gate 0).** As with `eval-harness.md` (whose build-vs-adopt backbone was deferred to ADR-0013),
  this PRD states **what** the redesigned system must do and **why**, architecture-agnostically. It
  does **not** decide the two big open questions — (a) `cloud` vs. `self_hosted` vs. hybrid Managed
  Agents environment for production, and (b) the concrete shape of the decoupled delivery API and
  the git-tracked candidate layout. Those are the Architect's next task (an ADR presenting options
  with a recommendation), escalated to the human for sign-off per this repo's standing convention
  for major/irreversible decisions. **No implementation starts until that ADR lands and the human
  approves it.** One PM-level ambiguity (FR-8/§7: whether "narrated version" means listening-script
  text or synthesized audio) was preliminarily resolved by Claude in favor of listening-script text
  (2026-07-05, owner asleep) and **confirmed by the owner on 2026-07-06**: "The listening script is
  the output. No actual TTS for evals." Settled, no longer open.
- **Revision 2 (2026-07-06) — supersedes revision 1; incorporates six owner-feedback points given
  after reviewing revision 1 (before reviewing ADR-0014).** In brief: (1) **eval-harness
  re-integration is de-scoped** — the old "Section E" FR-13/FR-14 move to Non-Goals; the existing
  `deploy/eval/` harness will be adapted to *this* epic's output in a **later, separate epic**, not
  the reverse, so this epic must not shape its design around `deploy/eval/`'s current mechanics.
  What stays is the standalone *property* that a candidate's artifacts are retrievable via
  Claude-Platform-only API calls (reframed as an intrinsic property of the new system, not "for the
  eval harness"). (2) **The local Desktop fallback is declared dead/retired** — the PRD no longer
  hedges about preserving it or reconciling ADR-0008's lockstep with it. (3) **Markdown→HTML
  conversion moves to the delivery side, deterministically (no LLM)** — content generation's output
  narrows to **brief markdown + listening-script text only** (brief HTML is dropped from what
  content generation produces). (4) **Git-native versioning is preferred over a bespoke
  `registry.json`** — historical candidate state must be retrievable via git primitives (e.g.
  `git show <ref>:<path>`) **without a repo checkout/rollback**; a duplicate-of-git index is now
  discouraged, not assumed. (5) restates (1). (6) **A new per-brief source-usage output** is added
  (which `sources.md` sources were featured in a run), on **every** run, seeding GitHub issue #28's
  later source-consolidation effort. Because several of these directly contradict specific sections
  of **ADR-0014**, **ADR-0014 was written against revision 1 and is no longer valid as-is — it needs
  a fresh Architect pass against this revision** (a separate, already-planned next step, not part of
  this PM revision). Old→new FR mapping is noted inline where numbering shifted.
- Author: product-manager (Claude)  ·  Date: 2026-07-05 (rev. 2: 2026-07-06)
- Linked ADRs: **ADR-0014** (agent-system-redesign topology) exists but was written against
  **revision 1** of this PRD and is **superseded/invalidated by this revision** on the six points
  above — it requires a fresh Architect pass (the required Gate-0 next step) before any
  implementation. This PRD also supersedes the architecture premises of **ADR-0004** (self-hosted
  chosen so the agent's own `boto3` reaches AWS via the microVM IAM role), **ADR-0006** (the
  `self_hosted` environment + microVM stack shape), and **ADR-0007** (skill + delivery orchestration
  folded into one agent run), and updates **ADR-0008** (skill-content lockstep — see the revised §6
  note: the local Desktop fallback is retired, so the lockstep collapses to in-repo copy ↔ live
  Skills-API resource) — all of which the fresh redesign ADR pass must reconcile.
- Source: owner request (2026-07-05, verbatim): *"Agent system re-design — we'll completely
  de-couple the content generation on Claude Platform from the TTS/e-mail/… on AWS. The deployment
  of a new agent system (candidate) to Claude Platform should ideally be a simple API call without
  the need to bundle and deploy a container. Evals should be possible with all candidates on Claude
  Platform without even standing up the MicroVM. … Everything should be git-tracked and declarative.
  Multi-agent needs to be possible. I do not understand the point with the different environments.
  Can we find a solution that only requires one self-hosted environment?"* Live research against
  `platform.claude.com/docs` (2026-07-05) then established the facts in §6 that make this feasible;
  the owner subsequently confirmed (2026-07-05) that evaluating **cloud-for-everything, including
  production** is in scope.

## 1. Problem

The daily-AI-brief pipeline's current Managed Agents architecture (ADR-0004 / ADR-0006 / ADR-0007)
**tightly couples content generation** (the Claude Platform agent + the `daily-ai-brief` skill) to
**AWS delivery** (Polly narration, S3 archival, SES send, DynamoDB subscriber fan-out). That
coupling is not incidental — it is baked into three concrete, file-verifiable places:

1. **One container image bakes in both concerns.** `deploy/managed-agent/microvm/Dockerfile`
   copies **both** the skill content (`:71` — `COPY skills/daily-ai-brief /opt/skills/daily-ai-brief`)
   **and** the AWS delivery pipeline code (`:92` — `COPY pipeline/ /opt/pipeline/`) into the single
   microVM image. A content-generation experiment and an AWS-delivery change ship in the same
   artifact.
2. **One IAM role holds both auth surfaces.** `MicroVmExecutionRole`
   (`deploy/managed-agent/cdk/managed_agent/stack.py`) grants **both** the Anthropic
   environment-key read (`ReadEnvironmentKey`, worker auth) **and** the full AWS delivery
   permission set (Polly synth, S3 rw on `cowork-polly-tts-740353583786/*`, SES send gated by
   `ses:FromAddress: aibriefing@mschweier.com`, DynamoDB `Query` on `brief-subscribers`). The
   execution context that generates content also holds live rights to email real subscribers.
3. **Delivery is triggered by hardcoded inline prompt text.** `deployment.json`'s
   `agent.initial_prompt` is a ~3,000-character inline blob whose wrapping steps orchestrate HTML
   derivation, the `audio_email.py` invocation, and the environment-variable wiring for the
   delivery step (ADR-0007's amendment moved these delivery mechanics into `initial_prompt`
   deliberately). Changing how content generation works means editing the same string that also
   drives AWS delivery.

**The concrete costs of this coupling, both already felt:**

- **Every content-generation experiment requires a container rebuild-and-push of the same image
  that also contains the AWS delivery code.** Trying a different model, prompt, or multi-agent
  decomposition means rebuilding the microVM image (`create-microvm-image`) — a multi-step CLI
  process (`deploy/managed-agent/README.md` §5) — because the skill content is baked into the
  image at build time. The eval-harness epic hit this exact wall firsthand: a skill-content change
  required an **undocumented** microVM image rebuild **even after a successful Skills API version
  push**, because `worker.mjs` has no runtime skill-fetch logic and the agent reads the skill from
  the baked-in `/opt/skills/...` path via `cat` (now documented in `README.md` §3a and ADR-0008's
  2026-07-04 amendment). This is a symptom of **this repo's custom worker**, not a platform limit —
  the standard self-hosted worker and the `cloud` environment both download skills dynamically per
  session (§6).
- **Running an evaluation requires standing up the same AWS infrastructure as production, and the
  eval harness cannot actually vary the candidate.** `deploy/eval/functions/trigger/handler.py`
  targets a **single hardcoded** `PRODUCTION_AGENT_ID` / `PRODUCTION_ENVIRONMENT_ID` pair
  (`:252-253`), and the `candidateConfigId` it records (`:221`) is only a **label** on the
  eval record — it does **not** change what actually runs. So today the harness can only ever
  evaluate the one production configuration, dressed up under different labels; it has **no
  mechanism to run a genuinely different candidate at all.** Because that one configuration is the
  self-hosted microVM pipeline, every evaluation drags in the full AWS delivery stack (the microVM,
  the launcher Lambda) even though evaluation must never touch delivery. *(This bullet describes
  today's coupling as **motivation** only. Rev. 2, owner feedback #1/#5: **actually re-plumbing
  `deploy/eval/`** — replacing its hardcoded pair, its record schema, its poll-based S3 retrieval — is
  explicitly **out of scope** for this epic and deferred to a later, separate epic that adapts the
  harness to whatever this redesign produces. This epic delivers only the standalone property that a
  candidate is triggerable and its output retrievable via Claude-Platform-only APIs, with no AWS and
  no delivery path; it does not touch the harness's code.)*

The owner is about to start a **cost-optimization epic** (the separate, deferred
`cost-optimization-candidates.md`) that will test many candidate configurations — different models
per phase, session/context restructuring, multi-agent decompositions, brief-length trimming. Under
today's architecture, **every one of those candidates is a container rebuild plus a full AWS-stack
stand-up to evaluate**, and the eval harness literally cannot switch between them. That is
unworkable at the cadence the optimization epic needs, and it entangles pure content-generation
experiments with the live subscriber-facing AWS delivery path they should never be able to touch.

### Why now
The cost-optimization epic is queued and depends on being able to (a) deploy a new candidate agent
system cheaply and (b) evaluate it without production infrastructure. Live research (§6) confirmed
the platform primitives to fully decouple these — `cloud` vs. `self_hosted` environments are
independent, composable resources; skills can load dynamically per session; and content artifacts
are retrievable through Claude-Platform-only API calls. Redesigning **now**, before the
optimization epic produces its candidates, means those candidates land on a clean architecture
instead of forcing a redesign mid-flight. It also directly answers the owner's standing confusion
("I do not understand the point with the different environments … can we find a solution that only
requires one self-hosted environment?"): one environment can already serve any number of agents,
and the environment need not be self-hosted at all once delivery is decoupled (§6).

## 2. Goals & non-goals

### Goals
- **Fully decouple content generation from AWS delivery.** A candidate agent system's **only**
  interface to AWS delivery shall be a **stable, versioned contract** (the content it produces —
  **brief markdown + listening-script text** — handed across a well-defined boundary), **never**
  direct AWS credentials or IAM in the content-generation execution context. The thing that
  generates the brief must not be able to email a subscriber. *(Rev. 2: content generation no longer
  produces brief **HTML** — HTML is derived deterministically on the delivery side; see the
  Markdown→HTML goal below and FR-2/FR-2a.)*
- **Move Markdown→HTML derivation to the delivery side, deterministically (no LLM).** Deriving the
  inbox-readable HTML from the brief markdown shall be performed by the **delivery side**,
  **deterministically, with no LLM/agent involvement**, using the pipeline's existing
  markdown-to-HTML conversion approach as its basis — turning today's per-run, agent-improvised
  `markdown.markdown(...)` call (specified only as "convert that brief Markdown to clean,
  inbox-readable HTML" in `deployment.json`'s `initial_prompt` step 2, done ad hoc each run) into an
  explicit, reviewable, tested, single delivery-owned function. The existing standardized HTML design
  — the delivery-side chrome already applied by `audio_email.py`'s `_html_with_header()`
  (feedback-link/subscribe/AI-disclaimer banner) and `_html_with_unsubscribe_footer()`, plus the
  visual convention the agent's ad hoc conversion has produced — **must be preserved**. This is a
  cost saving (no AI needed for a deterministic syntax transform) and a consolidation of an already-
  split concern (delivery already owns the header/footer chrome today). *(Rev. 2, owner feedback #3.)*
- **Make deploying a new candidate agent system a pure API-call operation** — no container build,
  no image push, no bundling step of any kind. Standing up candidate N should be API calls plus a
  git-tracked declaration, not a `create-microvm-image` cycle.
- **Make a candidate triggerable and its output retrievable with no AWS infrastructure and no
  delivery path — an intrinsic property of the redesigned system.** Triggering any candidate and
  retrieving its produced content artifacts shall require **no** AWS infrastructure (no microVM, no
  delivery Lambda) and shall **not** trigger or touch the delivery path. A candidate's produced
  artifacts — the **brief markdown**, the **listening-script text**, the **`candidates.json`**
  stories-considered selection artifact, the **source-usage record** (below), and the run's
  **cost/token-usage data** — shall be retrievable via **Claude-Platform-only API calls**. *(Rev. 2,
  owner feedback #1: this is a property of the new content-generation system itself — it matters
  intrinsically for cheap experimentation regardless of which harness, if any, consumes the output
  later. It is deliberately **not** framed as "for the eval harness"; wiring the existing
  `deploy/eval/` harness to this is a later, separate epic — see Non-goals.)*
- **Make every candidate git-tracked and declarative, with independently-diffable dimensions.**
  Model, system prompt(s), skill(s), and parameters (e.g. effort / thinking budget) shall be
  **separate, independently-diffable** fields — not one baked prompt blob (contrast today's
  `initial_prompt`). A **multi-agent** candidate (a coordinator + sub-agents graph) shall be
  representable as cleanly as a single-agent one.
- **Persist candidate versions as real, addressable Platform-side resources** that can be created
  without deleting or superseding earlier ones, and select any past or present candidate for either
  a triggered run or (once promoted) the production schedule **without rebuilding anything.**
- **Retrieve any candidate's historical state via git's own native versioning — no repo rollback,
  and no bespoke duplicate-of-git index.** Recovering a **historical** version of a candidate's
  declaration (model, prompt(s), skill reference(s), parameters) shall be possible through git's own
  native versioning primitives — e.g. `git show <commit-or-tag>:<path>`, which reads a file's content
  at any historical commit/tag **without touching HEAD or the working tree** — so accessing an
  earlier version of one and the same agent/multi-agent candidate **never requires checking out an
  old commit or rolling back the repo.** A **bespoke side-table/index that merely duplicates what git
  commits/tags already track shall be avoided or minimized**; any minimal mapping still needed (e.g.
  "which git ref does live Platform resource X correspond to") must be justified against why git
  tags/commit messages alone are insufficient, not assumed necessary by default. *(Rev. 2, owner
  feedback #4: the owner favors out-of-the-box GitHub versioning; `git show <ref>:<path>` is the
  stated mechanism that answers "how will the eval system read a previous version of a prompt without
  rolling back the repo.")*
- **Emit a per-brief source-usage record on every run.** The content-generation pipeline shall
  additionally produce, on **every** run — production or candidate/experimental alike — a durable,
  structured record of which of the `sources.md`-listed sources were actually featured/used in that
  run's brief. This is **additive** (it does not change the shipped brief) and seeds a later,
  separate source-list-consolidation effort (identify never-featured sources to prune and cut
  research cost). *(Rev. 2, owner feedback #6; realizes GitHub issue #28; sibling of the
  `candidates.json` selection artifact — same additive, non-behavior-changing pattern.)*
- **Seriously evaluate retiring the self-hosted stack** (`deploy/managed-agent/cdk/` and
  `deploy/managed-agent/microvm/`) entirely in favor of the `cloud` environment type, for **both**
  candidate/eval use **and** production — per the owner's explicit 2026-07-05 decision to have this
  evaluated — while still producing a real recommendation (this PRD requires the evaluation, not a
  foregone outcome).

### Non-goals (explicitly out of scope for this epic)
- **No actual cost-optimization candidate is built, deployed, or run here.** Producing and
  evaluating optimization candidates (different models per phase, context restructuring,
  multi-agent decomposition, brief-length trimming) is the **separate, deferred
  `cost-optimization-candidates.md` epic**, which depends on this one. This epic delivers the
  **mechanism** to declare/deploy/evaluate candidates and (as validation) re-expresses the
  **current production configuration** as the first candidate — it does not design or ship any
  cheaper configuration.
- **The cloud-vs-self-hosted-vs-hybrid architecture decision is NOT made here.** It is the
  Architect's ADR (§7), escalated to the human. All requirements below are architecture-agnostic.
- **The concrete shape of the decoupled delivery API is NOT decided here** — whether it is a new
  standalone CDK stack (e.g. `deploy/delivery/`, mirroring the existing per-surface pattern) and how
  a content-generation agent authenticates to it are the Architect's ADR (§7).
- **No re-integration of the existing `deploy/eval/` harness — explicitly out of scope; deferred to
  a later, separate epic.** *(Rev. 2, owner feedback #1/#5 — this reverses revision 1's "Section E:
  in scope, not deferred.")* This epic must **not** modify `deploy/eval/` code, its trigger Lambda,
  its DynamoDB record schema, or its poll handler, and must **not** shape the redesigned
  content-generation system's design around `deploy/eval/`'s **current** mechanics — its hardcoded
  `PRODUCTION_AGENT_ID` / `PRODUCTION_ENVIRONMENT_ID`, its DynamoDB record schema, or its poll-based
  S3 retrieval. The owner's explicit direction is that **the existing eval harness will be adapted to
  whatever this epic produces, in a later epic — not the other way around** ("I don't want to
  influence the development of the de-coupled content gen + delivery system by the existing eval
  harness. It needs to be the other way around."). Adapting `deploy/eval/` (and any change to its v1
  criteria, its structured-record schema, or its review UI) is that later epic's problem. What
  **stays in scope** here is only the standalone *property* — see the Goals — that a candidate can be
  triggered and its artifacts retrieved via Claude-Platform-only API calls (FR-6/FR-7/FR-8), because
  that property is intrinsic to cheap experimentation on the new system regardless of any harness.
  The old revision-1 requirements "the eval harness targets a specified candidate" (former FR-13) and
  "eval-harness re-integration preserves the v1 criteria unchanged" (former FR-14) are hereby
  **retired from this epic's scope** and belong to the later adaptation epic.
- **No change to `deploy/subscribers/` or `deploy/feedback/`.** Those are unrelated public-facing
  surfaces (subscribe/confirm/unsubscribe; the reader-feedback form). This epic does not touch their
  stacks, schemas, domains, or IAM.
- **No change to the brief's content, the weekday schedule, the send cadence, or SES/Polly
  behavior** as experienced by real subscribers. Production keeps producing and delivering the same
  brief at the same time; only the internal architecture that produces and delivers it is
  restructured. (If the redesign changes the production runtime, the owner-facing output and
  schedule are held constant and validated as unchanged — §5.)
- **The local Desktop fallback is dead — this epic does not consider, preserve, or reconcile
  anything with it.** *(Rev. 2, owner feedback #2 — this is stronger than revision 1's "no retirement
  required," and stronger than the prior "deactivated, might be reactivated" framing in `CLAUDE.md` /
  ADR-0008.)* The local fallback (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`) is
  **retired/dead**; it will not run and will not be reactivated. This epic **does not** need to keep
  it in lockstep, mirror any change into it, preserve it, or reconcile the redesigned topology with
  it **in any form**. Concretely, the ADR-0008 three-way lockstep collapses to a **two-way** lockstep
  (in-repo copy ↔ live Skills-API resource); the local Desktop copy is no longer a lockstep member.
  There is no "kept in lockstep if reactivated" hedge — the owner's direction is unambiguous: dead,
  in any form.

## 3. Users & use cases

- **Owner / optimization operator (primary)** — the reason the redesign exists.
  - *US-1:* "As the owner, I declare a new candidate agent system in git (model, prompt(s),
    skill(s), parameters) and deploy it to Claude Platform with a **pure API call** — no container
    build, no image push — so I can iterate on content-generation configurations quickly."
  - *US-2:* "As the owner, I **trigger any candidate without standing up any AWS infrastructure**
    and without touching the delivery path, and retrieve its produced content artifacts (brief
    markdown, listening-script text, `candidates.json`, source-usage record, cost/token data) via
    Claude-Platform-only API calls — so experimenting with a candidate is cheap, fast, and can never
    email a real subscriber." *(Rev. 2: reframed off "the eval harness needs" — this is the new
    system's own retrieval property, usable by a manual/scripted check now and by the later eval
    adaptation epic.)*
  - *US-3:* "As the owner, I express a **multi-agent** candidate (a coordinator plus sub-agents) as
    a git-tracked declaration just as cleanly as a single-agent one, so the optimization epic can
    test agentic decompositions."
  - *US-4:* "As the owner, I keep **every** candidate version as an addressable Platform resource
    tracked in git (leaning on git's own versioning, not a bespoke index), and can point either an
    experimental/eval run or the production schedule at any of them without rebuilding anything."
  - *US-4a:* "As the owner, I recover the exact declaration of an **earlier** version of a candidate
    (its model, prompts, skill references, parameters) with a plain `git show <ref>:<path>` —
    **without** checking out an old commit or rolling back my working tree — so I (or a later tool)
    can inspect or re-run any historical candidate state on demand." *(Rev. 2, owner feedback #4.)*
- **Operator / scripted check running an experimental candidate (not the live daily send)** — must
  be able to actually vary the candidate.
  - *US-5:* "As an operator (via a manual or scripted Claude-Platform API call — **not** the existing
    `deploy/eval/` harness, whose adaptation is a later epic), I trigger a run **against a specified
    candidate** and retrieve that candidate's produced content artifacts through Claude-Platform-only
    API calls, so I can inspect what a given candidate under test actually produces." *(Rev. 2, owner
    feedback #1/#5: reframed from "the eval harness" as the actor — this epic delivers the
    triggerable-and-retrievable property; wiring `deploy/eval/` to it is deferred.)*
- **Content-generation agent (runtime actor)** — produces content, must not touch AWS.
  - *US-6:* "As the content-generation agent, I produce the **brief markdown and the
    listening-script text** (no HTML — that is derived deterministically on the delivery side) and
    hand them across a **stable delivery contract**; I hold **no** AWS credentials and cannot email a
    subscriber — the delivery side authenticates, derives the HTML, and performs the AWS work."
    *(Rev. 2, owner feedback #3.)*
- **Architect / reviewer / security-engineer**
  - *US-7:* "As the Architect, I evaluate `cloud` vs. `self_hosted` vs. hybrid for production, the
    decoupled-delivery-API shape, and the git candidate layout; I write them up in an ADR with a
    recommendation and get the human's sign-off before anyone builds."
  - *US-8:* "As a reviewer/security-engineer, I can verify the content-generation execution context
    holds **no** AWS delivery credentials, evaluations never send email to real subscribers and never
    require production infrastructure, the delivery API's auth is least-privilege, and the redesign
    does not regress the live daily send."

## 4. Functional requirements

Numbered FR-N; each maps to acceptance criteria AC-N in §5. "The system shall …". Stated
**architecture-agnostically** — they describe *what* the redesigned system does, not *how* it is
built (the Architect's ADR, §7). "Delivery boundary" / "delivery contract" refers to whatever
decoupled interface the ADR selects between content generation and AWS delivery; "candidate
declaration" refers to whatever git-tracked declarative representation the ADR selects. Criteria are
satisfied by whichever backbone the ADR chooses.

*(Rev. 2 numbering note: former FR-13/FR-14 — "eval harness targets a specified candidate" and
"re-integration preserves the v1 criteria" — are **removed** from this epic (moved to Non-goals /
the later eval-adaptation epic). Former FR-15 → **FR-13** and former FR-16 → **FR-14** to close the
gap. Two new requirements are added: **FR-2a** (delivery-side deterministic HTML derivation) and
**FR-8a** (per-brief source-usage record). Acceptance criteria are renumbered to match in §5.)*

### A. Decoupling content generation from AWS delivery

1. **Content generation holds no AWS delivery credentials.** The content-generation execution
   context (the agent[s] that research/write/derive the brief) shall hold **no** direct AWS
   credentials or IAM permissions for delivery (Polly, S3, SES, DynamoDB). Its **only** path to
   delivery shall be handing produced content across the delivery boundary. Concretely, the coupling
   that exists today — `MicroVmExecutionRole` holding both the environment-key read and the full
   delivery permission set — shall be eliminated: whatever runs content generation shall not be able
   to email a real subscriber.
2. **Delivery is a stable, versioned contract taking content as input.** AWS delivery (narration,
   HTML derivation + audio/HTML archival, subscriber fan-out, feedback-link embedding — the work
   `deploy/managed-agent/pipeline/audio_email.py` does today) shall be reachable only through a
   **stable, versioned contract** whose input is the brief content — **the brief markdown and the
   listening-script text**, plus whatever minimal metadata delivery needs — not a shared execution
   context. *(Rev. 2, owner feedback #3: the contract input **no longer includes brief HTML**;
   delivery derives HTML itself per FR-2a.)* The contract's version shall be recorded so a change to
   it is explicit and reviewable, not an invisible inline prompt edit.
2a. **Delivery derives the brief HTML deterministically, with no LLM, from one fixed template.** The
   delivery side shall derive the inbox-readable brief HTML from the brief markdown
   **deterministically, with no LLM/agent involvement**, via an explicit, tested, delivery-owned
   function that applies **one fixed, well-designed HTML email template**, chosen once and applied
   **identically on every run**. The content-generation side shall **not** produce brief HTML.
   The stable, code-defined delivery-side chrome — `audio_email.py`'s `_html_with_header()`
   (`:309`) and `_html_with_unsubscribe_footer()` (`:344`) — stays **exactly as-is** (unchanged,
   still delivery-owned, still wrapping whatever the body-conversion function produces). The
   acceptance bar for the body conversion has **two** parts: (a) the **content conversion itself**
   — headings, paragraphs, lists, bold/italic, links, horizontal rules, i.e. whatever
   `markdown.markdown(...)` does with the source Markdown — is verified **correct and faithful
   against multiple real markdown fixtures**, not just one; and (b) the chosen template is
   **professional, internally consistent, and broadly in the visual spirit** (clean, modern styling
   fit for a daily newsletter) of recent production output — **without** being pinned to any single
   day's exact CSS values, wrapper markup, or incidental structural choices.
   *(Corrected 2026-07-06 — supersedes the rev.-2 requirement to "preserve THE existing standardized
   design" and "reproduce it byte-for-byte against a real recent production brief (singular)." That
   premise was **factually wrong**: there is no single standardized design to preserve. Three real
   production `brief.html` files pulled from `s3://cowork-polly-tts-740353583786/briefs/<date>/` —
   2026-07-03, 2026-07-04, 2026-07-06 — are **three genuinely different HTML documents**, not
   variations of one template. 07-03 uses a single `<div>` wrapper (no `<table>`), a `.footer` CSS
   class, link colour `#0645ad`, background `#f2f2f4`; 07-04 uses `.email-wrapper`/`.email-card`
   divs with named classes in a `<head>`-level `<style>` block, a `.tldr` callout the others lack,
   link colour `#2b6cb0`, and an `h1` colour-bordered the others aren't; 07-06 uses a nested
   `<table role="presentation">` layout with an inline `<style>` inside the body's inner `<td>`, an
   uppercase "Daily AI Brief" eyebrow label the others lack, link colour `#2563eb`, and its own ad
   hoc footer text ("You're receiving this because you subscribed to the Daily AI Brief.", no
   unsubscribe link — which does **not** match `_html_with_unsubscribe_footer()`'s actual output,
   confirming it is the agent's own improvised body footer, not delivery code). The agent
   re-improvises the whole document — wrapper approach, CSS class system, colour palette, presence
   of a `.tldr` box or eyebrow label — fresh every run: today's output is **non-deterministic by
   construction** (an LLM writing free-form HTML), so there is nothing stable to reverse-engineer.
   Fixing one template is therefore a genuine improvement — subscribers currently receive a
   visually-different email every day; determinism removes that as a side effect. Build-time note:
   this remains a **regression risk** in that the developer must validate the content conversion
   against multiple real fixtures and confirm the chrome is untouched — but the target is a chosen
   fixed template in the same visual spirit as recent output, **not** a byte-for-byte match to any
   one historical brief. Rev. 2, owner feedback #3, still applies otherwise.)*
3. **Delivery authenticates the caller; content generation does not carry AWS identity.** The
   delivery boundary shall authenticate its caller by a mechanism that does **not** require the
   content-generation side to hold AWS credentials or IAM (e.g. a bearer token / API key
   presented to an HTTP endpoint, consistent with `deploy/eval/`'s existing reviewer-bearer-secret
   pattern — the exact mechanism is the ADR's to specify, FR-open in §7). Delivery credentials to
   AWS services shall live **only** on the delivery side.

### B. Candidate deployment without container builds

4. **Deploying a candidate is a pure API-call operation.** Deploying a new candidate agent system to
   Claude Platform shall be achievable through **API calls plus a git-tracked declaration only** —
   with **no** container build, **no** image push, and **no** bundling step. The
   `create-microvm-image` build cycle shall **not** be on the critical path of standing up a new
   candidate. *(If the ADR selects a topology in which a container is still built for production
   delivery-side reasons, that container shall not be part of deploying a **content-generation
   candidate** — the two must be independently deployable, per FR-1/FR-2.)*
5. **Skill content reaches a candidate without an image rebuild.** A change to a candidate's
   skill content shall take effect for that candidate through the platform's own skill mechanism
   (e.g. a Skills-API version the agent references), **without** requiring a microVM image rebuild —
   eliminating the ADR-0008 §amendment failure mode where a Skills-API push alone did not reach the
   running session because the skill was baked into the image. *(This is the direct fix for the
   "custom worker never wired up dynamic skill loading" root cause in §1.)*

### C. Infrastructure-free, delivery-free candidate runs and retrieval

*(Rev. 2, owner feedback #1/#5: this section describes an intrinsic property of the **redesigned
content-generation system itself** — any candidate can be triggered and its output retrieved with no
AWS and no delivery path — which matters for cheap experimentation regardless of which harness, if
any, consumes it later. It is deliberately **not** written as "for the eval harness." Wiring the
existing `deploy/eval/` harness to this is a later, separate epic; see Non-goals.)*

6. **Triggering a candidate requires no AWS infrastructure.** Triggering any candidate for a run
   shall **not** stand up or require AWS delivery infrastructure — **no** microVM and **no** delivery
   Lambda shall be necessary. Such a run shall be executable via Claude-Platform API calls alone.
7. **A non-production candidate run never triggers or touches delivery.** A candidate run performed
   for experimentation/evaluation (i.e. any run **not** on the live production schedule) shall
   **not** trigger, invoke, or otherwise reach the AWS delivery path, and shall **never** send email
   to a real subscriber. In the redesigned architecture such a run has **no delivery path at all** —
   so this is guaranteed by construction, **not** by any subscriber-fan-out feature gate. *(The
   `deploy/eval/` harness's existing fail-closed `ENABLE_SUBSCRIBER_FANOUT` gate remains correct for
   the **production** delivery path; how it is affected when that harness is later adapted to this
   system is the later epic's concern, not this one's.)*
8. **A candidate's produced artifacts are retrievable via Claude-Platform-only API calls.** A
   candidate run shall produce, and a caller shall be able to retrieve **without any AWS
   involvement**, these content artifacts: the **brief markdown**, the **listening-script text**, the
   **`candidates.json`** stories-considered selection artifact, the **source-usage record** (FR-8a),
   and the **cost/token-usage data** for the run. Retrieval shall be via Claude-Platform APIs only
   (e.g. the Files API for generated files; the sessions/events API for token usage) — not via S3 or
   any other AWS service. **Brief HTML is not among the retrievable artifacts** — it is no longer a
   content-generation output; HTML is derived deterministically on the delivery side (FR-2a) and is
   not produced on non-production runs. *(See the §7 open question on the exact link between an agent
   writing an output path and it becoming a downloadable file — a build-time verification item, not a
   settled fact.)* **[CONFIRMED — owner, 2026-07-06]** "the narrated version and other artefacts"
   (owner's original phrasing) is the **pre-narration listening-script *text***, **not** synthesized
   audio: "The listening script is the output. No actual TTS for evals." Settled — a candidate run
   never synthesizes or retrieves audio.
8a. **Per-brief source-usage record, on every run.** The content-generation pipeline shall emit, on
   **every** run — production or candidate/experimental alike — a durable, structured record of which
   of the `sources.md`-listed sources were actually featured/used in that run's brief. This record is
   **additive**: it shall **not** change the shipped brief's content (matching the same
   additive-artifact pattern the eval-harness epic established for `candidates.json`). It is a
   foundation for a later, separate source-list-consolidation effort — identifying never-featured
   sources to prune from `sources.md` and cut research cost — and does **not** itself perform that
   consolidation. On a candidate run it is retrievable via Claude-Platform-only APIs (FR-8); on a
   production run it is archived alongside the run's other outputs. *(Rev. 2, owner feedback #6;
   realizes **GitHub issue #28**; compare `eval-harness.md` §4.B item 10.)*

### D. Git-tracked, declarative, versioned candidates

9. **Independently-diffable candidate dimensions.** Each candidate shall be represented in git as a
   **declarative** definition whose **model**, **system prompt(s)**, **skill reference(s)**, and
   **parameters** (e.g. effort / thinking budget) are **separate, independently-diffable** fields —
   not a single opaque prompt blob (contrast `deployment.json`'s current inline `initial_prompt`). A
   diff of a candidate must make clear *which dimension changed*.
10. **Multi-agent candidates representable as cleanly as single-agent.** The candidate representation
    shall express a **multi-agent** candidate — a coordinator plus one or more sub-agents (a graph,
    with each agent carrying its own model/prompt/skill/parameters) — as cleanly as a single-agent
    candidate. The representation shall not privilege the single-agent case such that adding a
    sub-agent requires a fundamentally different structure.
11. **Candidate versions persist as addressable Platform resources without superseding earlier
    ones.** Each candidate (and its referenced skill version[s]) shall persist as a **real,
    addressable Claude Platform resource** created **without** deleting or archiving earlier
    candidates. Keeping every version registered is acceptable because Managed Agents bills per
    **active session**, not per idle agent/environment definition (§6) — so "keep every candidate
    forever" carries no ongoing compute cost. Any past or present candidate shall be **selectable**
    for either an experimental/eval run or (once promoted) the production schedule **without
    rebuilding anything.**
12. **Historical candidate state is retrievable via git-native versioning, without a repo rollback;
    a bespoke duplicate-of-git index is avoided or minimized.** *(Rev. 2, owner feedback #4 — this
    replaces revision 1's "git-tracked `registry.json` mapping slug → IDs" as the presumed default.)*
    The declaration of any candidate — including any **earlier/historical** version of one and the
    same agent/multi-agent candidate — shall be retrievable through **git's own native versioning
    primitives** (e.g. `git show <commit-or-tag>:<path>`, which reads a file's content at any
    historical commit/tag **without touching HEAD or the working tree**), so accessing a previous
    version **never requires checking out an old commit or rolling back the repo.** This is a **hard
    requirement** regardless of mechanism. A **bespoke side-table/index that merely duplicates what
    git commits/tags already track shall be avoided or minimized** (a stated preference): if the
    Architect's ADR determines some **minimal** mapping is still genuinely needed — e.g. "which git
    ref does live Platform resource X correspond to," since a candidate's live Platform IDs are
    generated at sync time and are not knowable from the declaration alone — that mapping must be
    **justified against why git tags/commit messages alone are insufficient** (for example, a
    git-native approach such as a **tag per candidate-sync event whose annotation/message records the
    resulting Platform resource id(s)**), not assumed necessary by default. This PRD does **not**
    prescribe the exact mechanism — that is the Architect's fresh ADR pass — but the two constraints
    are firm: (a) retrieve historical declaration state **without repo rollback**, and (b) do **not**
    stand up a hand-rolled index that re-tracks what git already versions. Whatever the ADR selects,
    turning a declaration into live Platform resources shall be **idempotent** (re-running the sync
    does not duplicate a resource) and the resulting live resource id(s) shall be recoverable for any
    candidate (present or historical) without reconstructing them from memory or the Anthropic
    console.

### E. Eval-harness re-integration — REMOVED from this epic (deferred to a later, separate epic)

*(Rev. 2, owner feedback #1/#5. Revision 1's Section E was titled "in scope, not deferred"; that is
reversed. Its two requirements — former **FR-13** "the eval harness targets a specified candidate,
not one hardcoded pair" and former **FR-14** "re-integration preserves the v1 criteria unchanged" —
are **removed from this epic** and moved to Non-goals (§2).)* Owner direction: *"eval harness
re-design will follow this epic and is not in scope for this one … I don't want to influence the
development of the de-coupled content gen + delivery system by the existing eval harness. It needs
to be the other way around."* Accordingly, this epic delivers only the **standalone property** that
a candidate is triggerable and its artifacts retrievable via Claude-Platform-only API calls
(FR-6/FR-7/FR-8) — it does **not** modify `deploy/eval/`, its trigger Lambda, its DynamoDB schema, or
its poll handler, and does **not** shape the new system around that harness's current mechanics
(hardcoded `PRODUCTION_AGENT_ID`/`PRODUCTION_ENVIRONMENT_ID`, its record schema, poll-based S3
retrieval). Adapting `deploy/eval/` to consume this epic's output is the **later** epic's work.

### F. Serious evaluation of retiring the self-hosted stack

13. **The ADR must seriously evaluate cloud-for-everything, including production.** *(Rev. 2:
    renumbered from former FR-15.)* The Architect's
    ADR shall **seriously evaluate retiring** `deploy/managed-agent/cdk/` and
    `deploy/managed-agent/microvm/` (the self-hosted stack) entirely in favor of the `cloud`
    environment type — for candidate/eval use **and** for **production** — per the owner's explicit
    2026-07-05 decision. The ADR shall present this as a genuine option with a **recommendation**,
    weighing at minimum: the exact output-retrieval mechanics on `cloud` (§7 open item); whether any
    self-hosted-only capability the production pipeline actually relies on would be lost (e.g. the
    docs indicate self-hosted's `Memory` feature is **not** available on `cloud` — the ADR must
    check whether this pipeline uses it); data-residency considerations (a non-concern for this
    specific public-news application, but the ADR should state that explicitly rather than assume
    it); operational cost/complexity of each; and how each satisfies FR-1..FR-12 (and the added
    FR-2a/FR-8a). The outcome is **not preordained** — `cloud`-for-everything, `self_hosted`-retained,
    or a hybrid could each win on the merits; this FR requires the evaluation and a recommendation,
    not a specific answer.

### G. No regression to production, subscribers, or feedback

14. **No regression to the live daily send, subscribers, or feedback surfaces.** *(Rev. 2:
    renumbered from former FR-16.)* The redesign shall
    **not** regress the live weekday brief's content, schedule, send cadence, or subscriber
    experience, and shall **not** modify `deploy/subscribers/` or `deploy/feedback/`. If the redesign
    changes the production runtime (e.g. moving production to `cloud`), the owner-facing brief output
    and the weekday delivery shall be validated as **unchanged** before the redesigned production
    path supersedes the current one. Real subscribers shall not receive an evaluation brief or a
    duplicated/dropped daily brief as a result of the redesign.

## 5. Acceptance criteria

Given/When/Then, testable against the redesigned system, the delivery boundary, and account
`740353583786`, `us-east-1`. *(Where an AC refers to "the delivery boundary" or "candidate
declaration," it is satisfied by whichever concrete shapes the Architect's ADR selects — the
criteria are architecture-agnostic.)* *(Rev. 2 numbering: former AC-13/AC-14 — eval-harness
re-integration — are **removed** (out of scope); former AC-15 → **AC-13**, former AC-16 → **AC-14**;
new **AC-2a** and **AC-8a** added.)*

### Decoupling
- **AC-1 (content generation holds no AWS delivery rights):** Given the redesigned content-generation
  execution context, When its identity/permissions are inspected, Then it holds **no** AWS delivery
  credentials or IAM (no Polly/S3/SES/DynamoDB delivery grants) — the current
  `MicroVmExecutionRole`-style combined grant is gone — and it cannot email a real subscriber
  (FR-1).
- **AC-2 (delivery is a versioned content contract):** Given a produced brief, When it is delivered,
  Then delivery happens **only** by handing the content (**brief markdown + listening-script text** +
  minimal metadata — **not** brief HTML) across a stable, versioned contract whose version is
  recorded — not via a shared execution context or an inline prompt blob (FR-2).
- **AC-2a (delivery derives HTML deterministically, no LLM, one fixed template):** Given the brief
  markdown handed across the contract, When delivery runs, Then the inbox-readable brief HTML is
  derived on the **delivery side** by an explicit, tested function with **no** LLM/agent involvement,
  the content-generation side produces **no** brief HTML, and the output applies **one fixed HTML
  email template identically on every run** with the `_html_with_header()`/
  `_html_with_unsubscribe_footer()` chrome unchanged; And When the body conversion is checked, Then
  (a) the content conversion (headings, paragraphs, lists, bold/italic, links, horizontal rules) is
  verified correct and faithful against **multiple** real markdown fixtures, and (b) the chosen
  template is professional, internally consistent, and broadly in the visual spirit of recent
  production output — **without** being pinned to any single day's exact CSS, wrapper markup, or
  incidental structural choices (FR-2a). *(Corrected 2026-07-06 — the former "preserves the existing
  standardized design, confirmed byte-for-byte against a real recent production brief" bar was
  factually unmeetable: no single standardized design exists — three real production briefs
  (2026-07-03/04/06) are three structurally different documents; see FR-2a's correction note for the
  evidence.)*
- **AC-3 (delivery authenticates its caller; no AWS identity on the content side):** Given the
  delivery boundary, When a caller invokes it, Then the caller is authenticated by a mechanism that
  does **not** require the content-generation side to hold AWS credentials/IAM, and the AWS delivery
  credentials live only on the delivery side (FR-3).

### Candidate deployment without builds
- **AC-4 (pure-API candidate deploy):** Given a git-tracked candidate declaration, When a new
  candidate is deployed to Claude Platform, Then it is created via **API calls only** — with **no**
  container build, **no** image push, and **no** bundling step on the critical path (FR-4).
- **AC-5 (skill change without image rebuild):** Given a change to a candidate's skill content, When
  it is applied, Then it takes effect for that candidate through the platform's skill mechanism
  **without** a microVM image rebuild — i.e. the ADR-0008 §amendment "push didn't reach the session
  because the skill was baked into the image" failure mode does not occur (FR-5).

### Infrastructure-free, delivery-free candidate runs and retrieval
- **AC-6 (no AWS infra to trigger a candidate):** Given a candidate, When it is triggered for a run,
  Then the run needs **no** microVM and **no** delivery Lambda — executable via Claude-Platform API
  calls alone (FR-6).
- **AC-7 (a non-production candidate run never reaches delivery):** Given an experimental/eval
  candidate run (not on the production schedule), When it executes, Then it does **not** trigger or
  invoke the AWS delivery path and **no** email reaches a real subscriber — guaranteed by
  construction (it has **no** delivery path at all), **not** by any subscriber-fan-out feature gate
  (FR-7).
- **AC-8 (content artifacts retrieved via Claude-Platform APIs only):** Given a completed candidate
  run, When a caller retrieves its artifacts, Then it obtains the brief markdown, listening-script
  text, `candidates.json`, source-usage record, and cost/token-usage data **via Claude-Platform APIs
  only** (no S3 or other AWS read) — with **brief HTML not among them** (HTML is derived on the
  delivery side, FR-2a, and not produced on non-production runs) and audio **not** synthesized or
  retrieved (confirmed by the owner, 2026-07-06: "the listening script is the output," no TTS for
  evals) (FR-8).
- **AC-8a (per-brief source-usage record emitted every run):** Given any run — production or
  candidate/experimental — When it completes, Then a durable, structured record of which
  `sources.md`-listed sources were featured/used in that run's brief is produced (retrievable via
  Claude-Platform-only APIs on a candidate run; archived alongside the run's outputs on a production
  run), and its production does **not** change the shipped brief's content (FR-8a).

### Git-tracked declarative candidates
- **AC-9 (independently-diffable dimensions):** Given a candidate declaration, When a single
  dimension changes (model, a system prompt, a skill reference, or a parameter), Then the git diff
  isolates that dimension — model/prompt(s)/skill(s)/parameters are separate fields, not one opaque
  blob (FR-9).
- **AC-10 (multi-agent representable):** Given a multi-agent candidate (a coordinator + one or more
  sub-agents, each with its own model/prompt/skill/parameters), When it is declared, Then it is
  expressed in the same declarative structure as a single-agent candidate, with no fundamentally
  different structure required to add a sub-agent (FR-10).
- **AC-11 (versions persist without superseding; any is selectable):** Given several candidates
  created over time, When a new candidate is added, Then earlier candidates are **not** deleted or
  archived, each persists as an addressable Platform resource, and any past or present candidate can
  be selected for an experimental/eval run or the production schedule **without rebuilding anything**
  (FR-11).
- **AC-12 (historical state retrievable via git-native versioning, no rollback; no duplicate-of-git
  index):** Given several revisions of a candidate committed over time, When an **earlier** version's
  declaration is retrieved via `git show <commit-or-tag>:<path>`, Then its content is obtained
  **without** checking out an old commit or rolling back the working tree; and When the candidate
  layout is inspected, Then there is **no** bespoke side-table/index that merely duplicates what git
  commits/tags already track (any minimal ref→live-resource-id mapping the ADR keeps is justified,
  not a redundant registry); and When a declaration is synced, Then the sync is idempotent (no
  duplicate resource) and the resulting live resource id(s) are recoverable for any candidate,
  present or historical (FR-12).

### Serious cloud-vs-self-hosted evaluation
- **AC-13 (ADR evaluates cloud-for-everything with a recommendation):** *(Rev. 2: renumbered from
  former AC-15.)* Given the Architect's ADR,
  When it is reviewed, Then it seriously evaluates retiring the self-hosted stack (`cdk/`,
  `microvm/`) in favor of `cloud` for **both** eval **and** production, weighs at minimum output-
  retrieval mechanics, any lost self-hosted-only capability (e.g. `Memory`), data-residency (stated,
  even if a non-concern here), and operational cost — and makes a **recommendation** the human signs
  off on, with the outcome not preordained (FR-13).

### No regression
- **AC-14 (no production/subscriber/feedback regression):** *(Rev. 2: renumbered from former
  AC-16.)* Given the redesigned system, When the live weekday brief runs, Then its content, schedule,
  send cadence, and subscriber experience are unchanged; `deploy/subscribers/` and `deploy/feedback/`
  are untouched; and no real subscriber receives an experimental/eval brief or a duplicated/dropped
  daily brief. If production moves runtime (e.g. to `cloud`), the owner-facing output and weekday
  delivery are validated unchanged **before** the new path supersedes the old (FR-14).

## 6. Constraints & dependencies

*(Items below are settled decisions/facts/research for this epic — do not relitigate. The deferred
decisions are the two in §7.)*

- **AWS account** `740353583786`, region `us-east-1` — confirm the active account before any deploy
  or mutation. The redesign stays single-account/single-region consistent with every other stack in
  this repo, unless the ADR justifies otherwise and the human approves.
- **Confirmed platform research (2026-07-05, live against `platform.claude.com/docs`) — treat as
  ground truth for the requirements, but re-verify the flagged build-time items in §7:**
  - **Two environment types exist.** `self_hosted` (the customer's own AWS Lambda MicroVMs — what
    this repo uses today) and `cloud` (a fully Anthropic-managed sandbox: Ubuntu 22.04,
    pre-installed languages/tools, `curl`/`wget` available, network disabled by default but
    toggleable in environment config, **no customer infrastructure at all**).
  - **Agent and Environment are independent, composable resources.** A session references an
    `agent_id` and an `environment_id` **separately**, with **no** 1:1 binding constraint — **one**
    environment (of either type) can already serve **any number** of different agents. This directly
    answers the owner's "can we find a solution that only requires one self-hosted environment?":
    yes — and in fact the constraint the owner was worried about never existed at the environment
    level in the first place. (Whether that one environment should even *be* self-hosted is the §7
    decision.)
  - **Skills download dynamically per session in the standard worker.** In the standard self-hosted
    worker pattern (the SDK's `EnvironmentWorker` / `ant beta:worker poll`), skills download
    **dynamically per-session** from the Skills API to `<workdir>/skills/<name>/`. **This repo's
    actual worker (`deploy/managed-agent/microvm/worker/worker.mjs`) is a fully custom port of AWS's
    reference sample that never wired this up** — it bakes the skill into the image at build time
    (`Dockerfile:71`) and has the agent `cat` it. That is precisely **why** a Skills-API version
    push alone never took effect for this repo (ADR-0008 §amendment) — a symptom of this repo's
    custom worker, **not** a platform limitation. A `cloud`-environment agent (or a self-hosted one
    using the standard worker) would not have this problem — the basis for FR-5.
  - **Content files are retrievable via Claude-Platform-only APIs.** The Files API
    (`GET /v1/files/{file_id}/content`) lets a caller download files created by skills / code
    execution purely via the Claude API — free, no AWS involvement. This is the strong candidate
    mechanism for FR-8's artifact retrieval. **Not fully confirmed live:** the exact linkage between
    "an agent writes an output path in the sandbox" and "that becomes a downloadable `file_id`" — a
    **build-time verification item** (§7), not a settled fact.
  - **Billing is per active session, not per idle definition.** Managed Agents bills per **active
    session**, not per idle agent/environment definition — so "keep every candidate version
    registered forever" (FR-11) carries **no** ongoing compute cost, unlike continuously-running
    compute would.
  - **Why self-hosted was originally chosen no longer holds once delivery is decoupled.** ADR-0004
    chose `self_hosted` specifically so the agent's own `boto3` could reach AWS via the microVM's
    IMDSv2-derived IAM role. Once delivery is decoupled into its own standalone HTTP boundary
    (FR-2), that reason **goes away** — a `cloud` sandbox with network enabled and `curl`/`wget` (or
    an MCP tool) could call the decoupled delivery endpoint, authenticated by a bearer token / API
    key rather than IAM. This is what makes FR-13's "evaluate cloud-for-everything" a live option
    rather than a non-starter.
- **The existing `deploy/eval/` harness is NOT touched by this epic (rev. 2, owner feedback #1/#5).**
  This epic does **not** re-integrate, modify, or adapt `deploy/eval/` — not its trigger Lambda, its
  DynamoDB record schema, its poll handler, its v1 criteria, its structured record, or its review UI.
  The new content-generation system is designed on its own terms; **the existing harness will be
  adapted to consume this system's output in a later, separate epic — not the reverse** (that later
  epic is where `deploy/eval/`'s hardcoded `PRODUCTION_AGENT_ID`/`PRODUCTION_ENVIRONMENT_ID`, its
  record schema, and its poll-based S3 retrieval get reworked). This epic must **not** let
  `deploy/eval/`'s current mechanics shape the redesign. `deploy/eval/`'s fail-closed
  `ENABLE_SUBSCRIBER_FANOUT` gate remains correct for the **production** delivery path; how it
  interacts with a later-adapted harness is out of scope here. What this epic guarantees is only the
  standalone property (FR-6/FR-7/FR-8) that a candidate is triggerable and its artifacts retrievable
  via Claude-Platform-only APIs, verifiable now with a manual/scripted API check.
- **Markdown→HTML derivation is a delivery-side, deterministic, tested function — a regression risk,
  not a mere refactor (rev. 2, owner feedback #3, FR-2a).** Today the brief HTML is produced ad hoc
  by the content-generation agent (a `markdown.markdown(...)` call driven by `deployment.json`'s
  `initial_prompt` step 2, which only says "convert that brief Markdown to clean, inbox-readable
  HTML"), while delivery already owns the surrounding chrome (`audio_email.py`'s `_html_with_header()`
  and `_html_with_unsubscribe_footer()`). Moving body conversion to delivery both **consolidates** an
  already-split concern **and fixes a real defect**: because the agent writes free-form HTML each run,
  the output is **non-deterministic** — subscribers receive a visually-different email every day.
  `derive_html()` must therefore establish **one fixed, well-designed template applied identically on
  every run**. The acceptance bar is: (a) the **content conversion** (headings, paragraphs, lists,
  bold/italic, links, rules — whatever `markdown.markdown(...)` does with the source Markdown) is
  verified correct against **multiple** real markdown fixtures; and (b) the chosen template is
  professional, internally consistent, and in the same visual spirit (clean, modern newsletter
  styling) as recent output — **not** pinned to any one day's CSS/wrapper/structure. The Reviewer must
  treat (a) as a regression check; the code-defined chrome (`_html_with_header()`,
  `_html_with_unsubscribe_footer()`) stays untouched. *(Corrected 2026-07-06 — supersedes the earlier
  "reverse-engineer THE standardized design and confirm byte-for-byte against a real recent production
  brief" wording, which assumed a single stable design that does not exist. Three real production
  `brief.html` files (2026-07-03/04/06, from `s3://cowork-polly-tts-740353583786/briefs/<date>/`) are
  three structurally different documents — differing wrapper approach (`<div>` vs.
  `.email-wrapper`/`.email-card` vs. nested `<table>`), CSS class systems, colour palettes
  (`#0645ad`/`#2b6cb0`/`#2563eb`), and structural elements (a `.tldr` box on 07-04 only, an uppercase
  eyebrow label on 07-06 only) — proving the agent re-improvises the whole document each run. The full
  evidence is in FR-2a's correction note.)*
- **Per-brief source-usage record is additive and must not change the brief (rev. 2, owner feedback
  #6, FR-8a).** The new source-usage output (which `sources.md` sources were featured in a run) is
  produced on **every** run and follows the same additive, non-behavior-changing pattern the
  eval-harness epic established for `candidates.json` — the Reviewer must confirm adding it does not
  change the shipped brief. It **realizes GitHub issue #28** and seeds a later, separate
  source-consolidation effort; it does **not** itself prune any source. If the Architect chooses to
  emit it via skill-content instruction, the reduced (two-way) ADR-0008 lockstep below applies; if via
  the pipeline wrapper, it does not — favor whichever captures source usage faithfully with the least
  skill-content churn.
- **Candidate version history is git-native; no bespoke duplicate-of-git index (rev. 2, owner
  feedback #4, FR-12).** Historical candidate declarations must be retrievable via git's own
  primitives (`git show <ref>:<path>`) **without** a repo checkout/rollback, and a hand-rolled
  index that merely re-tracks what git commits/tags already version is to be avoided or minimized.
  Any minimal ref→live-Platform-resource-id mapping the ADR still keeps (because live IDs are
  generated at sync time and are not derivable from the declaration) must be justified against why a
  git-native approach — e.g. a tag per sync event recording the resulting ids in its annotation — is
  insufficient. The exact mechanism is the Architect's fresh ADR pass; the two firm constraints are
  "retrieve historical state without rollback" and "don't duplicate git."
- **ADR-0008 lockstep collapses to two-way — the local Desktop fallback is dead (rev. 2, owner
  feedback #2).** ADR-0008's original **three-way** skill-content lockstep (in-repo copy ↔ local
  Desktop copy ↔ live Skills-API resource) and its 2026-07-04 image-rebuild amendment (rebuild
  required because this repo's custom worker bakes the skill into the microVM image) are premised on
  the **current** self-hosted, image-baked topology **and** on the local Desktop fallback still being
  a live participant. Under this revision, **the local Desktop fallback is retired/dead and is no
  longer a lockstep member — the lockstep is now two-way (in-repo copy ↔ live Skills-API resource).**
  Separately, if FR-5 / FR-13 move to dynamic skill loading and/or `cloud`, the **image-rebuild half**
  of the amendment stops applying (there is no image to rebuild; a Skills-API push suffices). The
  fresh Architect ADR pass must state plainly how ADR-0008 is updated/superseded by the redesigned
  topology — dropping the Desktop member and (if the topology moves off image-baked skills) the
  image-rebuild step — and the Reviewer must confirm no stale note tells a future maintainer to
  keep the dead Desktop copy in lockstep or rebuild an image that no longer exists.
- **Anthropic Managed Agents beta.** The pipeline runs on the beta Managed Agents surface
  (`managed-agents-2026-04-01` and related beta headers). The redesign continues to operate against
  that beta and must **fail loudly, not silently skip**, if the platform contract changes — the same
  discipline the migration and eval epics already require. The Deployments API's confirmed
  immutability (create-new-then-archive; no in-place update — `README.md` §6), the Skills API's
  confirmed version-push shape (`POST /v1/skills/{id}/versions`, `skills-2025-10-02` header —
  `README.md` §3a), and the Agents API's **confirmed native update-in-place versioning**
  (`POST /v1/agents/{id}` with a required `version` field updates the **same** `agent_id` and
  increments its version; a stale `version` returns `409`; `GET /v1/agents/{id}/versions` lists the
  full history — confirmed live 2026-07-06, correcting an earlier PATCH/PUT-only probe that had
  concluded agents were immutable, §7) are the known-good primitives to build on.
- **Credentials never committed** (repo convention). No Anthropic API key, AWS secret, delivery-API
  bearer token, or static access key in code, logs, or git; the redesign uses ambient/role
  credentials and Secrets-Manager-by-reference as the rest of the repo does. The decoupled delivery
  boundary's auth secret follows the same pattern as `deploy/eval/`'s reviewer bearer secret
  (created empty, populated out-of-band).
- **This epic is the dependency of, but strictly separate from, the cost-optimization epic.** The
  follow-up `cost-optimization-candidates.md` epic depends on this redesign but is **out of scope**;
  nothing here may design, pre-commit to, or smuggle in an actual optimization candidate (§2
  non-goal). The owner's stated ordering is: build the cost-optimization *candidate list* first
  (that epic's opening deliverable), then do **this** redesign, then use both together.

## 7. Risks & open questions

- **[DECISION NEEDED — Architect, then human sign-off] `cloud` vs. `self_hosted` vs. hybrid for
  production.** The single largest, most cross-cutting, hardest-to-reverse decision in the epic and
  **deliberately not made in this PRD** (mirroring how `eval-harness.md` deferred build-vs-adopt to
  ADR-0013). The owner has explicitly asked that **cloud-for-everything (including production)** be
  **seriously evaluated** (FR-13) and leans toward it, but the final call is the Architect's ADR
  with a recommendation, escalated to the human per this repo's standing convention for
  major/irreversible decisions. The ADR must weigh: the exact `cloud` output-retrieval mechanics
  (the Files-API linkage below); any self-hosted-only capability the production pipeline relies on
  (the docs indicate `Memory` is **not** available on `cloud` — check whether this pipeline uses
  it); data-residency (explicitly a non-concern for this public-news app, but state it); the fate of
  `deploy/managed-agent/cdk/` + `microvm/` (retired, retained, or partially reused for the delivery
  side); operational cost/complexity; and how each satisfies FR-1..FR-12 (and the added FR-2a/FR-8a).
  **Outcome not preordained.**
- **[DECISION NEEDED — Architect, then human sign-off] The concrete shape of the decoupled delivery
  boundary and the git candidate layout.** *(A first ADR pass — ADR-0014 — already proposed answers
  here, but it was written against revision 1 and is invalidated on the six rev.-2 points; the fresh
  pass must redo the git-versioning and HTML/artifact aspects in particular.)* The Architect's ADR
  shall specify: (a) the **delivery API's shape** — most likely a new standalone CDK stack (e.g.
  `deploy/delivery/`) mirroring the existing `deploy/subscribers/` / `deploy/feedback/` / `deploy/eval/`
  per-surface pattern (static site not needed; API Gateway + Lambda(s) + the existing pipeline logic,
  which now includes the **delivery-side deterministic Markdown→HTML function**, FR-2a) — and **how a
  content-generation agent authenticates to it** (bearer token most likely, matching `deploy/eval/`'s
  reviewer-secret pattern); and (b) the **git layout for a versioned candidate** — e.g. one directory
  per candidate under `agents/candidates/<slug>/` (no `agents/` directory exists yet — confirmed) with
  **separate** files/fields for model, prompt(s), skill reference(s), and parameters; **multi-agent
  candidates need multiple agent definitions plus a coordinator definition**; the **git-native version
  history mechanism** (retrieving a historical declaration via `git show <ref>:<path>` with **no** repo
  rollback, and **avoiding a bespoke duplicate-of-git index** — FR-12; if any minimal
  ref→live-resource-id mapping is kept, justify it against a git-native alternative such as a
  per-sync tag whose annotation records the resulting ids); and the **sync mechanism** that turns a
  declaration into live Platform resources (idempotent create; the resulting live ids recoverable per
  candidate, FR-12). This is design detail the PRD deliberately leaves to the fresh ADR pass.
- **[VERIFY AT BUILD TIME — not a settled fact] Exact Files-API retrieval linkage.** FR-8 assumes
  the Files API (`GET /v1/files/{file_id}/content`) is how a caller retrieves brief.md /
  listening-script.txt / `candidates.json` / the source-usage record from a `cloud`-sandbox session
  (brief HTML is **not** retrieved — it is delivery-derived, FR-2a), but the exact link between "the
  agent writes an output path in the sandbox" and "it becomes a downloadable `file_id`" was **not**
  independently confirmed live. The Architect/Developer must confirm this end-to-end before committing
  to a `cloud`-based retrieval path; if the linkage is not as assumed, an alternative retrieval
  mechanism (e.g. the Sessions events API) may be needed. Flagged as a verification item, not a
  blocker.
- **[CONFIRMED — Claude, 2026-07-06, live] The Agents API supports native update-in-place
  versioning — this corrects the ADR-0014 (first revision)'s "agents are immutable" finding.** That
  finding tested only `PATCH`/`PUT` (405) and `DELETE` (404) against `/v1/agents/{id}` and concluded
  no update/version primitive exists. Live re-verification (prompted by the owner questioning the
  premise) found the real mechanism: `POST /v1/agents/{id}` with a required `version` field in the
  body updates the agent **in place under the same `agent_id`**, incrementing `version` (confirmed
  1→2 on a real probe agent); a stale `version` correctly returns `409` (optimistic concurrency,
  confirmed); `GET /v1/agents/{id}/versions` lists the full history (confirmed, both versions
  returned with full content). This means FR-11/FR-12 no longer need "one agent resource per
  candidate revision" as the assumed worst case — **a candidate can map to one stable `agent_id` for
  its entire life**, updated in place on each sync, with Platform tracking version history natively.
  One nuance for multi-agent candidates: a coordinator does **not** automatically pick up a new
  version of a sub-agent it references — the coordinator itself must be explicitly updated to
  re-pin its roster to the new sub-agent version. The Architect's fresh ADR pass (already
  in progress) reworks Decision 2c around this corrected primitive.
- **[CONFIRMED — owner, 2026-07-06] "narrated version" = listening-script text, not synthesized
  audio.** FR-8 / AC-8 interpret the owner's "the narrated version and other artefacts" as the
  **pre-narration listening-script *text***, not Polly-synthesized audio. Preliminarily resolved by
  Claude on 2026-07-05 (the only reading consistent with the owner's zero-AWS-eval requirement), and
  **explicitly confirmed by the owner on 2026-07-06**: "The listening script is the output. No
  actual TTS for evals." Settled — no longer open.
- **ADR-0008 lockstep reconciliation risk (now simpler — the Desktop fallback is dead).** *(Rev. 2,
  owner feedback #2.)* Two reductions apply and both must be made explicit or a half-reconciled,
  silent-drift state (the exact failure ADR-0008 exists to prevent) results: (1) the local Desktop
  fallback is **retired/dead**, so it is **no longer a lockstep member** — the lockstep is now
  **two-way** (in-repo copy ↔ live Skills-API resource), not three-way, unconditionally (independent
  of the topology choice); and (2) **if** the topology moves off image-baked skills (FR-5 / a `cloud`
  choice), the **image-rebuild half** of ADR-0008's 2026-07-04 amendment also stops applying (no
  image to rebuild; a Skills-API push suffices). The fresh Architect ADR pass must state plainly how
  ADR-0008 is updated/superseded — dropping the Desktop member outright, and dropping the
  image-rebuild step if the topology moves — and the Reviewer must confirm no stale note tells a
  future maintainer to keep the dead Desktop copy in lockstep or rebuild an image that no longer
  exists.
- **Regression risk to a live, subscriber-facing pipeline.** This redesign touches the runtime that
  currently emails real subscribers every weekday. The chief risks are that moving production runtime
  (e.g. to `cloud`) silently changes the brief's content or breaks a send, **and** that moving
  Markdown→HTML derivation to the delivery side (FR-2a) changes the brief's rendered appearance.
  *(Corrected 2026-07-06: this appearance change is now understood as **intended** — today's HTML is
  non-deterministic (a different-looking email daily; three real briefs, 2026-07-03/04/06, are
  structurally different documents — see FR-2a's correction note), and FR-2a fixes one template, so
  the "risk" is a template regression in the **content conversion**, not a deviation from a
  nonexistent single design.)* Mitigation (in scope, FR-14 / §8): the current production configuration
  is re-expressed as the first candidate and validated to produce an **unchanged** brief (content,
  structure, listening script) before any redesigned production path supersedes the live one; the
  delivery-derived HTML's **content conversion** is validated against **multiple** real markdown
  fixtures and its **fixed template** is confirmed professional and in the same visual spirit as
  recent output (FR-2a) — **not** matched byte-for-byte to any one historical brief; the cut-over is
  staged, not a hard swap; and the security review confirms the content-generation side genuinely
  cannot email a subscriber (AC-1/AC-7).
- **Delivery-boundary security surface (for the security-engineer).** Introducing a decoupled
  delivery API creates a new authenticated surface that *can* email real subscribers if misused —
  the very capability FR-1 strips from content generation. The security review must confirm: its
  auth is least-privilege and fail-closed (a missing/invalid token cannot fall open to an
  unauthenticated send — echoing the `deploy/eval/` reviewer-auth and the launcher's fail-closed
  signature check); its AWS delivery credentials remain scoped exactly as
  `MicroVmExecutionRole`/`deploy/iam-policy.json` are today (Polly synth; S3 rw on the one bucket;
  SES send gated by `ses:FromAddress: aibriefing@mschweier.com`; DynamoDB `Query` on the
  `status-index` GSI) and no broader; and no new static access key is minted.
- **Beta-surface churn.** The redesign leans harder on the Files API, dynamic skill loading, and
  possibly the `cloud` environment — all beta surfaces that may drift. The same "fail loudly, not
  silently skip" discipline the prior epics require applies; the ADR/README must record the beta
  headers/versions built against and the redesign must not silently degrade if a contract changes.
- **Scope-creep into the optimization epic.** The temptation to start building a cheaper candidate
  while building the mechanism to declare candidates is strong. This epic ships the **mechanism and
  the re-expressed current configuration only**; any actual optimization candidate belongs to
  `cost-optimization-candidates.md` and must not be smuggled in (§2 non-goal).

## 8. Rollout & metrics

- **Handoff (Gate 0): a FRESH Architect ADR pass + human sign-off — BEFORE any build.** *(Rev. 2:
  ADR-0014 already exists but was written against revision 1 and is invalidated on the six rev.-2
  points — it must be revised, not merely re-read, before Gate 0 is met.)* The Architect writes the
  revised redesign ADR resolving the two §7 decisions: (1) `cloud` vs. `self_hosted` vs. hybrid for
  production (with the FR-13 serious cloud-for-everything evaluation and a recommendation), and (2)
  the decoupled-delivery-boundary shape (now including the **delivery-side deterministic Markdown→HTML
  function**, FR-2a) + the **git-native** candidate version-history layout (retrieval via
  `git show <ref>:<path>` with **no** repo rollback, and **no bespoke duplicate-of-git index** —
  FR-12) + the sync mechanism. The ADR also **reconciles ADR-0008** with the chosen topology **and
  with the now-dead local Desktop fallback** (the lockstep collapses to two-way, in-repo ↔ live
  Skills-API — feedback #2), accounts for the new per-brief **source-usage output** (FR-8a), and
  confirms (or defers with a stated build-time check) the two verification items (Files-API retrieval
  linkage; Agents-API versioning). **The ADR must NOT re-scope eval-harness re-integration back in —
  that is deferred (feedback #1/#5).** The human signs off on the backbone. **No implementation starts
  until this lands.** The owner has confirmed the FR-8 "narrated version = listening-script text"
  interpretation (2026-07-06).
- **Phasing (after Gate 0). *(Rev. 2: eval-harness re-integration is removed from the phasing;
  Markdown→HTML derivation and per-brief source-usage are added; the rollout ends with the redesigned
  system validated on its own terms, not with `deploy/eval/` wired up.)***
  1. **Decouple delivery + move Markdown→HTML to delivery (FR-1/FR-2/FR-2a/FR-3).** Stand up the
     decoupled delivery boundary (per the ADR) so delivery is reachable only by handing content
     (**brief markdown + listening-script text**, no HTML) across a versioned, authenticated contract;
     move the AWS delivery credentials to the delivery side only; and replace today's ad-hoc
     `markdown.markdown(...)` body conversion with an explicit, tested, delivery-owned deterministic
     function applying **one fixed template** — its content conversion validated against **multiple**
     real markdown fixtures and its template confirmed professional and in the visual spirit of recent
     output (**not** byte-for-byte against any single historical brief — no single standardized design
     exists; see FR-2a's correction note), with the
     `_html_with_header()`/`_html_with_unsubscribe_footer()` chrome unchanged. Content generation loses
     all AWS delivery IAM and stops producing brief HTML.
  2. **Candidate declaration + git-native versioning + sync (FR-9/FR-10/FR-11/FR-12).** Add the
     git-tracked, declarative candidate representation (independently-diffable
     model/prompt(s)/skill(s)/params; multi-agent-capable), rely on **git's own versioning** for
     history (retrieval via `git show <ref>:<path>`, no repo rollback; **no bespoke duplicate-of-git
     index** — any minimal ref→live-id mapping justified per FR-12), and the idempotent sync that
     creates candidates as addressable Platform resources without superseding earlier ones and makes
     each candidate's live id(s) recoverable.
  3. **Pure-API candidate deploy + dynamic skills (FR-4/FR-5).** Deploy a candidate via API calls
     only — no container build — with skill content reaching the candidate via the platform skill
     mechanism, no image rebuild.
  4. **Per-brief source-usage output (FR-8a).** Add the durable, structured per-run source-usage
     record (which `sources.md` sources were featured), emitted on **every** run (production and
     candidate alike), additively — confirmed not to change the shipped brief. Realizes GitHub issue
     #28.
  5. **Re-express the current production configuration as the first candidate (validation baseline,
     FR-14).** Express today's live configuration as candidate #1 in the new representation; confirm
     it produces an **unchanged** brief (content, structure, listening script) versus the current
     production path, with the delivery-derived HTML confirmed equivalent (FR-2a).
  6. **Validate the redesigned system on its own terms (no eval-harness wiring).** Deploy a candidate
     via API only, no container build (AC-4); change its skill and confirm it takes effect with no
     image rebuild (AC-5); trigger a candidate run with **no** microVM / delivery Lambda and confirm
     no email reaches a subscriber and no AWS delivery is touched (AC-6/AC-7); retrieve all content
     artifacts — brief markdown, listening-script text, `candidates.json`, source-usage record, cost
     data — via Claude-Platform APIs only, **no HTML**, audio not synthesized (AC-8), **using a
     manual/scripted API check, not `deploy/eval/`** (its adaptation is a later epic); confirm the
     source-usage record is emitted every run without changing the brief (AC-8a); confirm the
     delivery-derived HTML applies one fixed template with its content conversion validated against
     multiple real markdown fixtures (AC-2a — **not** a byte-for-byte match to any single historical
     brief; no single standardized design exists, see FR-2a's correction note); confirm a
     single-dimension
     candidate diff isolates that dimension (AC-9) and a multi-agent candidate is representable
     (AC-10); confirm every candidate persists selectably without rebuild (AC-11) and a historical
     candidate declaration is retrievable via `git show <ref>:<path>` with no repo rollback, with no
     duplicate-of-git index present (AC-12); confirm the content-generation context holds no AWS
     delivery rights (AC-1) and the delivery boundary authenticates its caller with no AWS identity on
     the content side (AC-2/AC-3); and, **before** any production cut-over, confirm the re-expressed
     current configuration produces an unchanged brief and the weekday delivery is unaffected (AC-14).
  7. **(Conditional) Production cut-over.** If (and only if) the ADR + human sign-off chose to move
     production runtime (e.g. to `cloud`), stage the cut-over: run the redesigned production path in
     parallel / behind validation, confirm the owner-facing brief and weekday send are unchanged
     (AC-14), then supersede the old path — never a hard swap. If the ADR keeps production on
     `self_hosted`, this phase is a no-op for production and the redesign applies only to
     candidate/experimental use.
- **Ship gate.** Gate 0 (the **revised** ADR + human sign-off + owner's FR-8 confirmation) passed;
  **AC-1..AC-14 pass** (including the new AC-2a and AC-8a; the former eval-re-integration AC-13/AC-14
  are out of scope); the security review confirms the content-generation context holds no AWS delivery
  credentials (AC-1), a non-production candidate run never reaches delivery or emails a subscriber
  (AC-7), the delivery boundary's auth is least-privilege and fail-closed with AWS delivery
  credentials scoped no broader than today and no new static key (§7 security note), and ADR-0008 is
  reconciled with no half-applied lockstep remaining **and no stale reference to the now-dead local
  Desktop fallback**; the delivery-derived HTML is confirmed to apply one fixed template with its
  content conversion validated against multiple real markdown fixtures (AC-2a — no byte-for-byte
  match to any single historical brief; no single standardized design exists) and the per-brief
  source-usage record is confirmed additive (AC-8a); and the re-expressed
  current configuration is confirmed to produce an **unchanged** brief with no regression to
  `deploy/subscribers/` or `deploy/feedback/` (AC-14).
- **Success metric.** After ship: the owner can **declare a new candidate agent system in git,
  deploy it to Claude Platform with a pure API call (no container build), and trigger + retrieve it
  with zero AWS infrastructure and zero risk to the live send** — single- or multi-agent, every
  version retained and selectable, and any historical version recoverable via `git show <ref>:<path>`
  **without a repo rollback** — with a manual/scripted API check genuinely running the **specified**
  candidate rather than one hardcoded configuration. Delivery derives the brief HTML deterministically
  (no AI cost) from **one fixed template applied identically every run** — a genuine improvement over
  today's day-to-day-varying, agent-improvised HTML — and every run emits a per-brief source-usage
  record seeding later source consolidation (issue #28). Concretely, this epic is a success when the
  **next** (cost-optimization) epic can spin up and retrieve an arbitrary candidate configuration's
  output in **minutes via API calls**, instead of a container rebuild plus a full AWS-stack stand-up
  per candidate — which is the entire reason this redesign exists. *(Wiring the existing `deploy/eval/`
  harness to consume these candidates is a later, separate epic — deliberately out of scope here per
  owner feedback #1/#5.)*
