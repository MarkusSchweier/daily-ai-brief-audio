# 0014. Agent-system redesign: environment topology, decoupled delivery boundary, and git-native candidate versioning

- Status: **Proposed — pending human sign-off.** This is the Gate-0 decision for the
  agent-system-redesign epic (`docs/prd/agent-system-redesign.md` §7/§8). It is a
  major, cross-cutting, hard-to-reverse decision (it changes the runtime the live
  subscriber-facing pipeline runs on) and therefore, per this repo's standing
  convention, is escalated to the human for final sign-off before **any**
  implementation begins. Two options are presented for the environment topology with a
  clear recommendation; the delivery-boundary shape and candidate versioning layout are
  recommended concretely. Nothing here is built, deployed, or merged yet.
- Date: 2026-07-05 (revised 2026-07-06 against PRD **revision 2**; revised again 2026-07-06 —
  **third pass** — to correct the agent-versioning premise, see the third-pass note below)
- Deciders: architect (Claude, recommendation); **human (final sign-off — pending)**

> **Revision note (2026-07-06 — THIRD pass — corrects the agent-versioning premise, prompted by the
> owner directly questioning it).** The prior (second) pass's Decision 2c, and the "What I verified
> live" bullet it rested on, stated that **"agents are IMMUTABLE (create-then-archive), with no
> agent-version primitive"** and therefore built candidate versioning around **a new `agent_id` per
> revision plus a git tag per sync event** recording that revision's new ids. **That finding was
> wrong.** It came from an incomplete live probe (only `PATCH`/`PUT`/`DELETE` on `/v1/agents/{id}`
> were tried — all rejected — and the *actual* update mechanism was never tested). The owner caught
> this by questioning the premise directly ("Have you considered that Claude Platform has native
> versioning for agents and skills baked in?"), and the orchestrating session then re-verified the
> correction live (real probe against `api.anthropic.com`; see the corrected "What I verified live"
> agent bullet and the anchor `CONFIRMED — Claude, 2026-07-06, live` in PRD §7). The real mechanism:
> **`POST /v1/agents/{id}` with a required `version` field updates the agent *in place under the same
> `agent_id`* and increments its version** (confirmed 1→2); a stale `version` returns **`409`**
> (optimistic concurrency); `GET /v1/agents/{id}/versions` lists the **full version history** — the
> same shape Skills versioning already has. **Consequence for this ADR:** a candidate maps to **one
> stable `agent_id` for its entire lifetime**, *updated* on each sync rather than re-created — so the
> per-sync **git tag mechanism from the second pass is dropped entirely** and replaced by a single
> plain `agent_id` field committed once in the candidate's own `candidate.json`. Decision 2c is
> reworked accordingly, along with everything downstream of it (the layout example, the migration
> sketch's sync step, the "Alternatives considered" entries, Consequences, "What was and wasn't
> confirmed," and the sign-off items). **Not touched by this third pass:** Decision 1, Decision 2a,
> Decision 2b, the "Reconciling ADR-0008" section, and the source-usage-record subsection — except a
> single cross-reference each that pointed at the now-dropped tag mechanism. The second-pass note
> below is retained for history; its bullet #4 (tags replace `registry.json`) is itself now
> **superseded by this third pass** (a plain `agent_id` field replaces both).
>
> **Revision note (2026-07-06 — this ADR was rewritten against PRD `agent-system-redesign.md`
> revision 2, which incorporated six owner-feedback points given after reviewing PRD rev. 1 but
> before reviewing this ADR).** The original (2026-07-05) pass was written against PRD rev. 1 and
> several of its decisions were invalidated. Changed in this revision, briefly:
> 1. **All eval-harness re-integration content removed.** Rev. 2 reversed the old "Section E"
>    (eval re-integration, in scope) to an explicit non-goal — the existing `deploy/eval/` harness
>    will be adapted to *this* system in a **later, separate epic**, not the reverse. Former Phase 5
>    ("re-integrate the eval harness") is deleted; the decisions/consequences no longer reference
>    serving `deploy/eval/` specifically. The live-verified retrieval finding (Sessions events API,
>    not the Files API) is retained but reframed as "how a candidate's artifacts are retrieved by any
>    future caller — a manual/scripted check now, an adapted harness later," not "how the eval
>    harness retrieves them."
> 2. **The local Desktop fallback is declared dead** (stronger than the old "dormant, kept in
>    lockstep only if reactivated"). The ADR-0008 three-way lockstep collapses to **two-way** (in-repo
>    ↔ live Skills-API) **unconditionally** — no reactivation hedge remains.
> 3. **New Decision 2a rework: delivery derives brief HTML deterministically (no LLM).** PRD FR-2a
>    moves Markdown→HTML from the content-generation agent to the delivery side. The `POST /deliver`
>    contract input **no longer includes `brief_html`**; delivery derives it itself.
> 4. **Decision 2c reworked: git-native candidate versioning replaces the bespoke `registry.json`.**
>    A **git tag per candidate-sync event** (annotation recording the resulting live Platform IDs) was
>    made the recommended mechanism here; the standalone `registry.json` and its "Alternatives" entry
>    were removed. *(SUPERSEDED by the third pass above: because a candidate now keeps one stable
>    `agent_id` for life, the per-sync tag is no longer needed either — a single plain `agent_id` field
>    in `candidate.json`, committed once at first sync, records the one not-derivable-from-git fact.)*
> 5. (Same as #1 — no separate change.)
> 6. **New: a per-brief source-usage record (FR-8a)** — a sibling additive artifact to
>    `candidates.json`, folded into Decisions 2a/2c.
>
> **Kept as-is** (not invalidated by rev. 2, and not re-verified): **Decision 1** (environment
> topology recommendation + all its live-verified evidence), **Decision 2b** (bearer-token delivery
> auth), and the "Alternatives considered" entries for retain-self-hosted, hybrid-the-other-way,
> delivery nested in an existing stack, and delivery auth via IAM/SigV4.
- Supersedes the architecture premises of **ADR-0004** (self-hosted chosen so the
  agent's own `boto3` reaches AWS via the microVM IAM role), **ADR-0006** (the
  `self_hosted` environment + microVM stack shape), and **ADR-0007** (skill + delivery
  orchestration folded into one agent run via `initial_prompt`), and updates **ADR-0008**
  (skill-content lockstep) — see "Reconciling ADR-0008" below. If accepted, this ADR
  becomes the new source-of-truth for the topology those four describe.

## Context

The daily-AI-brief pipeline (`deploy/managed-agent/`) currently **tightly couples**
content generation (the Claude Platform agent + the `daily-ai-brief` skill) to AWS
delivery (Polly narration, S3 archival, SES send, DynamoDB subscriber fan-out). Per PRD
§1, that coupling is baked into three file-verifiable places: one container image bakes
in **both** the skill content (`microvm/Dockerfile:71`) and the delivery pipeline code
(`:92`); one IAM role (`MicroVmExecutionRole`) holds **both** the Anthropic
environment-key read **and** the full AWS delivery permission set; and delivery is
triggered by a ~3,000-character inline `initial_prompt` blob in `deployment.json` that
also drives content generation.

The concrete costs (both already felt): **every content-generation experiment requires
a container rebuild-and-push** of the same image that also contains delivery code (the
eval-harness epic hit this exact wall — a skill change required an undocumented microVM
image rebuild even after a successful Skills-API version push, because this repo's
custom `worker.mjs` never wired up dynamic skill loading and the agent reads the skill
from a baked-in `/opt/skills/...` path, ADR-0008's 2026-07-04 amendment); and **running
an evaluation drags in the full AWS delivery stack** while the harness cannot actually
vary the candidate (`deploy/eval/functions/trigger/handler.py:252-253` targets a single
hardcoded `PRODUCTION_AGENT_ID`/`PRODUCTION_ENVIRONMENT_ID`, and `candidateConfigId` is a
label only). The queued cost-optimization epic will test many candidates — this is
unworkable at that cadence.

The PRD (revision 2) states, architecture-agnostically, **what** the redesign must do
(FR-1…FR-14, plus the rev.-2 additions **FR-2a** delivery-side HTML derivation and
**FR-8a** per-brief source-usage record) and defers **two** decisions to this ADR (PRD §7):

1. **`cloud` vs. `self_hosted` vs. hybrid Managed Agents environment**, for **both**
   candidate/eval use **and** production. The owner explicitly asked (2026-07-05) that
   **cloud-for-everything** be seriously evaluated and leans toward it — but FR-13
   (former FR-15) requires a genuine evaluation with a recommendation, not a foregone
   outcome.
2. **The concrete shape of the decoupled delivery boundary** (now including the
   delivery-side **deterministic Markdown→HTML derivation**, FR-2a) and the
   **git-native, versioned candidate layout** — independently-diffable candidate
   dimensions, a sync mechanism, and a way to recover a candidate's live Platform
   resource id(s) that leans on git's own versioning rather than a bespoke
   duplicate-of-git index (FR-12, rev. 2).

**Two rev.-2 scope facts this ADR must respect throughout** (both settled by the owner, not
relitigated here):

- **The existing `deploy/eval/` harness is out of scope.** This epic does **not** re-integrate,
  modify, or shape itself around `deploy/eval/` (its hardcoded `PRODUCTION_AGENT_ID` /
  `PRODUCTION_ENVIRONMENT_ID`, its DynamoDB record schema, or its poll-based S3 retrieval).
  Adapting that harness to consume this system's output is a **later, separate epic**. What stays in
  scope is only the standalone *property* that a candidate is triggerable and its artifacts
  retrievable via Claude-Platform-only API calls (FR-6/FR-7/FR-8) — verifiable now with a
  manual/scripted API check.
- **The local Desktop fallback (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`) is
  retired/dead** — not dormant, not "reactivatable." It is no longer a lockstep member (see
  "Reconciling ADR-0008").

### What I verified live (2026-07-05) — real curl calls against `api.anthropic.com`, not doc-reading

Following this repo's established discipline (real API probes found and fixed the bugs
that the eval-harness and managed-agent epics could not have caught from docs alone —
see `deploy/eval/README.md` "Judgment calls" and ADR-0006's verification note), I ran
live probes with the beta headers `managed-agents-2026-04-01` / `anthropic-version:
2023-06-01` (and `skills-2025-10-02` / `files-api-2025-04-14` where relevant). **Every
probe resource was archived afterward and production resources confirmed untouched.**
The findings below are the ground truth this ADR rests on; where they contradict prior
doc-based assumptions (including the PRD's §6 research and the task brief handed to me),
I trust the live evidence and say so explicitly.

- **Agent + Environment are independent, composable resources — confirmed.** A session
  (or a deployment) references an `agent` id and an `environment_id` **separately**, with
  **no** 1:1 binding. I ran the live self-hosted production agent's clone against a
  brand-new `cloud` environment in the same session flow. This settles the owner's
  standing confusion ("can we find a solution that only requires one … environment?"):
  one environment already serves any number of agents; the constraint never existed at
  the environment level.

- **A `cloud` environment runs, executes tools, and returns cost data in the SAME shape
  as self-hosted — confirmed.** I created a `cloud` environment (`config: {type:
  "cloud"}`), a minimal agent, and drove a real session via the repo's own proven
  trigger (`POST /v1/deployments` then `POST /v1/deployments/{id}/run`). The cloud
  sandbox executed the `write` and `bash` tools, wrote files, and reached terminal
  status `idle` (the same terminal state the eval poller already recognizes). The session
  `usage` object and the per-event `span.model_request_end` `model_usage` fields
  (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
  `cache_creation_input_tokens`) are **byte-for-byte the same field names**
  `eval_core/cost_miner.py` already parses, and the tool-use event is
  `{"type": "agent.tool_use", "name": "..."}` — exactly the shape the cost miner's
  `_is_web_search_tool_use()` was fixed to expect. **The existing cost miner works
  unchanged against a `cloud` session.** (FR-14 is satisfied on `cloud`.)

- **`cloud` sandbox outbound internet WORKS with `networking: {type: "unrestricted"}` —
  confirmed, and this CORRECTS the PRD.** A `bash` step doing
  `curl -s -o /dev/null -w "EGRESS_HTTP_%{http_code}" https://example.com` returned
  **`EGRESS_HTTP_200`** from inside the cloud sandbox. (An earlier probe against
  `api.anthropic.com` returned HTTP **401**, which also proves egress — a 401 is an
  authenticated-server response, i.e. the packet reached the internet; a blocked network
  gives "could not resolve"/"connection refused".) **Correction to PRD §6 / the task
  brief:** creating a cloud env with just `{type: "cloud"}` produced
  `networking: {"type": "unrestricted"}` **by default** on this account — network is
  **not** disabled by default here, and `unrestricted` is the accepted config shape
  (allow-list variants I tried were silently dropped to `null`). Working egress is what
  makes cloud-for-everything viable at all: the research skill's web-fetching and a
  `curl` to the decoupled delivery endpoint both need it.

- **The `cloud` environment config is far richer than the PRD summarized — confirmed.**
  It carries structured `packages` (`pip`/`npm`/`apt`/`cargo`/`gem`/`go`), `networking`,
  an `init_script`, and an `environment` (env-vars) block — all declarative, all
  settable at environment-create time. This means a cloud environment can declaratively
  pre-install `boto3`/the pipeline's Python deps and set env vars without any Docker
  image. Python in the sandbox is **3.11** (not the 3.12 the docs implied) — a minor
  fact the pipeline must not hard-code around.

- **Files-API auto-`file_id` for agent-written files is REFUTED — important.** The task
  brief's central assumption (an agent writes an output path → it becomes a downloadable
  `file_id`) **did not hold**. After the agent wrote `/probe.txt` and `/out/brief.md`
  via the `write` tool, `GET /v1/files` stayed **empty**, and there is **no**
  `/v1/sessions/{id}/files` or `/v1/environments/{id}/files` sub-resource (both 404).
  **However, the content is fully recoverable from the session event stream:** the
  `write` tool_use event echoes the full content in `input.content`, and a
  `cat /out/brief.md` `bash` tool_result returns the exact file body
  (`'LINE-A alpha\nLINE-B beta\nLINE-C gamma\n'`). So FR-8's "retrieve artifacts via
  Claude-Platform-only APIs" **is achievable on `cloud`** — but via the **Sessions
  events API** (have the wrapper `cat` each artifact so its content lands in a
  tool_result the harness reads via `GET /v1/sessions/{id}/events`), **not** via an
  automatic Files-API `file_id`. This is the single biggest correction to the plan and
  it changes the eval retrieval mechanism, not the feasibility.

- **Agents have a NATIVE update-in-place version primitive — confirmed, decisive for candidate
  versioning. (This CORRECTS a wrong finding from this ADR's second pass.)** The second pass
  concluded "agents are immutable (create-then-archive), no agent-version primitive," based on an
  **incomplete probe** — only `PATCH`/`PUT` on `/v1/agents/{id}` (both **405 Method Not Allowed**)
  and `DELETE` (**404**) were tried, and the actual update mechanism was never tested; that
  conclusion was wrong. The corrected, re-verified finding (real probe against `api.anthropic.com`,
  `managed-agents-2026-04-01`, prompted by the owner questioning the premise; probe agent archived
  afterward): **`POST /v1/agents/{id}` — the same URL as create, POST to the *item* rather than
  PATCH/PUT — with a required `version` field in the body updates the agent *in place under the same
  `agent_id`* and generates a new version.** On the probe, `POST /v1/agents/{id}` with
  `{"version": 1, "system": "probe v2 - updated"}` returned the **same `agent_id`** and
  `"version": 2`. Retrying with the now-stale `"version": 1` returned **`409`** — the `version` field
  is a required precondition, i.e. optimistic concurrency (update from a known state, like a compare-
  and-swap). `GET /v1/agents/{id}/versions` returned **both** versions with their full content and
  `updated_at` timestamps — a genuine, complete history, **structurally identical to how Skills
  versioning already works** (`GET /v1/skills/{id}/versions`). The official docs
  (`platform.claude.com/docs/en/managed-agents/agent-setup`) confirm this is documented standard
  behavior, including **no-op detection** (an update producing no change relative to the current
  version creates no new version) and one nuance that becomes a real design constraint below:
  **coordinators are not updated automatically** — a coordinator that references an agent in its
  `multiagent.agents` roster keeps the version pinned when the coordinator was created/last updated,
  even if the reference omits `version`; to delegate to a new sub-agent version, the coordinator
  itself must be updated to re-pin its roster. Therefore candidate versioning (FR-11/FR-12) maps
  **one stable `agent_id` to a candidate for its entire lifetime** — each sync *updates* that same
  agent when its declaration changed — and Claude Platform tracks the version history natively (a
  second historical-retrieval source alongside git). This is a significant simplification over the
  second pass's "one new agent resource per revision, recorded in a per-sync git tag": there is only
  ever **one** `agent_id` per candidate to know about, so the tag mechanism is dropped (Decision 2c).
  *(Note this is now the **opposite** of the Deployments API, which remains genuinely immutable /
  create-then-archive per README §6 — agents and deployments differ here, and the second pass wrongly
  assumed they matched.)*

- **Skills DO have a version primitive — confirmed.** `GET /v1/skills/{id}` returns
  `latest_version` (a numeric id), and `GET /v1/skills/{id}/versions` lists concrete
  versions (the production skill has three, created 07-03…07-04, matching ADR-0008's push
  history). A candidate's `skills.json` should record a **concrete numeric skill version**, not
  the moving `"latest"` label, for a reproducible candidate.

- **Multi-agent is a first-class field ON a single agent resource — confirmed.** The
  agent object has a top-level `multiagent` key (`null` for single-agent).
  Agent-create accepts `multiagent: {type: "coordinator", agents: [{entry: {...}}]}`
  (my probe reached field-level validation: `multiagent.agents[0].entry.type: Field
  required`). So a coordinator + sub-agents graph is **one** agent resource with a
  nested `agents[].entry` array — not N separate agents the candidate layout must stitch
  together. (The exact `entry` schema is a build-time detail flagged below; the
  structural fact — multi-agent lives inside one agent definition — is what the
  candidate layout needs.)

## Decision 1 (recommended — pending sign-off): Hybrid — `cloud` for candidate/eval, `cloud` for production, and **retire** the self-hosted stack, staged behind validation

**Recommendation: adopt the `cloud` environment type for BOTH candidate/eval and
production, and retire `deploy/managed-agent/cdk/` + `deploy/managed-agent/microvm/`
(the self-hosted launcher Lambda, webhook, WAF, microVM image, and build/execution
roles) — but stage the production cut-over behind validation, not as a hard swap
(FR-14 / §8 Phase 7).** In effect this is "cloud-for-everything," reached by the
hybrid door: eval moves to `cloud` first (zero production risk), the current
configuration is re-expressed as candidate #1 and validated to produce an unchanged
brief on `cloud`, and only then does production cut over.

The evidence makes this the strongest option:

1. **The original reason for self-hosted (ADR-0004) evaporates once delivery is
   decoupled.** ADR-0004 chose `self_hosted` *specifically* so the pipeline's own
   `boto3` calls could reach AWS via the microVM's IMDSv2-derived IAM role. This
   redesign's whole premise (FR-1/FR-2/FR-3) is that content generation **holds no AWS
   credentials** and reaches delivery only across an authenticated HTTP boundary. Once
   that is true, the single justification for running our own microVM infrastructure is
   gone. A `cloud` sandbox with `unrestricted` networking (confirmed working) can `curl`
   the decoupled delivery endpoint with a bearer token — it never needs an AWS identity.

2. **`cloud` satisfies every FR I could test, with strictly less infrastructure.** It
   executes tools (FR-6), returns the exact cost/token shape the miner already parses
   (FR-14), reaches the internet for research and for delivery calls, downloads skills
   dynamically per session (FR-5 — the direct fix for the ADR-0008 image-rebuild failure
   mode), and needs **no** launcher Lambda, **no** webhook + signing secret, **no** WAF,
   **no** microVM image, **no** `create-microvm-image` build cycle, and **no**
   Docker/`--platform`-pinning packaging saga (README §Prerequisites documents the real
   pain there). Deploying a candidate becomes a pure API call (FR-4) with nothing to
   build. This is a large, permanent reduction in operational surface for the exact
   personal-project frugality posture the repo values.

3. **A candidate run becomes genuinely infrastructure-free and delivery-free (FR-6/FR-7).**
   On `cloud`, running a candidate is: create a candidate deployment against the
   candidate's agent + a `cloud` env, `/run` it, read artifacts from the Sessions events
   API. **No microVM, no launcher, no delivery Lambda, no S3 read** — and because the
   content-generation side has no AWS delivery path at all, "a non-production candidate
   run must not email a subscriber" is guaranteed **by construction** (there is no
   delivery path at all), not by any subscriber-fan-out feature gate. *(The
   `ENABLE_SUBSCRIBER_FANOUT` gate in `audio_email.py` remains correct for the
   **production** delivery side only; this epic does not touch it, and does not depend on
   it for the delivery-free guarantee.)* This property is intrinsic to the redesigned
   content-generation system — useful for a manual/scripted check now, and useful to
   whatever later epic adapts the existing eval harness to this system; **this ADR/epic
   does not touch `deploy/eval/` code.**

4. **Data-residency is a genuine non-concern here — stated explicitly, not assumed.**
   The pipeline researches **public AI news** from public sources and produces a
   **public** newsletter sent to self-service subscribers. There is no PII in the
   content-generation path (subscriber emails live only on the AWS delivery side, in the
   `brief-subscribers` DynamoDB table, which content generation will no longer touch),
   no proprietary corpus, no regulated data. Anthropic already runs the model and sees
   the full transcript regardless of environment type. So moving tool-execution into
   Anthropic's managed `cloud` sandbox exposes nothing that self-hosting protected. (If
   this were a private-data or regulated workload, this calculus would flip and
   self-hosted or a hybrid would be warranted — it is not.)

5. **No self-hosted-only capability this pipeline depends on is lost.** The PRD flagged
   the docs' note that the `Memory` feature is "not available on `cloud`." **This
   pipeline does not use Managed Agents Memory** — cross-run "yesterday's brief"
   persistence is done via S3 (`brief_history.py`, ADR-0005), read at session start by
   the wrapper, entirely outside any platform Memory feature. So the one asymmetric
   capability the PRD worried about is irrelevant here. (The reverse — a `cloud`-only
   capability self-hosted lacks — does not bind us either; nothing here needs it.)

**The one real cost of `cloud`, stated honestly:** the `cloud` sandbox's outbound
network is Anthropic-managed and (per the config) `unrestricted` rather than governed by
*our* IAM/VPC. Today, egress hardening is a documented "available future lever" that
ADR-0006 deliberately did **not** pull (the research step needs broad public web access
anyway, and least privilege is enforced at the IAM layer, which for content generation
will now be **empty** of AWS rights). So we are not losing a control we were using. If
future hardening of *what the sandbox can reach* is ever wanted, that is a `cloud`
environment `networking` config concern, not a reason to keep a whole microVM fleet.

**Why staged, not a hard swap (FR-14).** Production emails real subscribers every
weekday. The cut-over re-expresses today's exact configuration as candidate #1 on
`cloud`, validates it produces an unchanged brief (content, structure, listening
script), runs it in parallel/behind validation, and only then supersedes the live
`self_hosted` deployment — mirroring the parallel-run discipline ADR-0006/0007 already
established for the original migration. Retiring `cdk/` + `microvm/` happens **after**
that validation, never before.

**What is NOT retired:** the `deploy/managed-agent/` directory does not vanish — its
`skills/daily-ai-brief/`, its `deployment.json`/`agent.json` source-of-truth payloads,
and its README runbook remain relevant (updated for the `cloud` topology). Only the
self-hosted *infrastructure* subtrees (`cdk/` and `microvm/`) are retired once
production has cut over.

## Decision 2a (recommended): the decoupled delivery boundary — a new standalone `deploy/delivery/` CDK stack, bearer-token authenticated, that derives the brief HTML deterministically

**Recommendation: build the decoupled delivery boundary as a NEW, standalone
`deploy/delivery/` CDK app** — a sibling of `deploy/subscribers/`, `deploy/feedback/`,
and `deploy/eval/`, matching this repo's proven one-CDK-app-per-surface convention
(ADR-0012 established exactly this reasoning for keeping `deploy/feedback/` standalone).
No static review site is needed; the shape is **API Gateway (HTTP API) + a delivery
Lambda that wraps today's `audio_email.py` logic and takes on the Markdown→HTML
derivation that today's content-generation agent does ad hoc**, plus the delivery-side
IAM and a bearer secret.

Concretely:

- **`POST /deliver`** on an HTTP API, taking the brief content as its JSON body: the
  **brief markdown**, the **listening-script text**, and the minimal metadata delivery
  needs (email subject, the run's local date / `PIPELINE_TIMEZONE`, and the
  fan-out/feedback config toggles). **The request body does NOT include `brief_html`**
  (rev. 2, FR-2a) — content generation no longer produces brief HTML; delivery derives it
  itself (next bullet). This is the **stable, versioned contract** (FR-2): the request
  body schema carries an explicit `contractVersion` field so a change to it is a reviewable
  code change, not an invisible `initial_prompt` edit. The response reports back what was
  **derived** (the HTML), synthesized, sent, and archived. *(This is a change from this
  ADR's original — rev. 1 — pass, where `brief_html` was a contract **input**; it is now a
  delivery-derived value, never an input.)*

- **Delivery derives the brief HTML deterministically, with no LLM (rev. 2, FR-2a) — the
  delivery Lambda's new, explicit responsibility.** Today the inbox-readable HTML is
  produced by the **content-generation agent** as an undocumented, per-run wrapping step:
  `deployment.json`'s `initial_prompt` step 2 says only *"convert that brief Markdown to
  clean, inbox-readable HTML and save to /workspace/brief.html,"* and `audio_email.py`
  currently **reads** that file as an input (`brief_html = open(os.environ["BRIEF_HTML_PATH"])`,
  `audio_email.py:159`). Under the redesign the delivery Lambda derives the HTML itself,
  via a **tested, delivery-owned function** that:
  1. converts the brief markdown body to HTML using **Python's `markdown` library**
     (`markdown.markdown(...)`) — the same library the agent's ad hoc conversion already
     implicitly uses. **`markdown` is NOT currently a listed dependency anywhere in this
     repo** (no `requirements.txt`/`pyproject.toml` lists it, and `audio_email.py` does not
     import it today — the agent's call runs inside the sandbox where the package is
     ambient), so it must be **newly added to the delivery Lambda's `requirements.txt`** (a
     new `deploy/delivery/` requirements file); and
  2. wraps that body with the **existing, unchanged** delivery-side chrome already in
     `audio_email.py`: `_html_with_header(...)` (`audio_email.py:309` — the top banner:
     per-recipient feedback prompt when available + "subscribe here" forward prompt +
     AI-curation disclaimer, in one styled box) and `_html_with_unsubscribe_footer(...)`
     (`audio_email.py:344` — the `<hr>` + "Unsubscribe" footer on subscriber copies). These
     two functions are **already delivery-side today** and stay **exactly as-is** — only the
     *body* conversion moves from the agent into delivery.

  **This is a flagged regression risk, not a mere refactor (rev. 2 / PRD §6).** Because
  today's body conversion is agent-improvised and undocumented, faithfully reproducing "the
  standardized design" means **reverse-engineering the exact current output** (the precise
  `markdown` extensions/options the produced HTML implies — e.g. whether tables, fenced
  code, or nl2br are in play), **not guessing**. Concretely, this is a **build-time task for
  the Developer**: pull a **real recent production `brief.html`** (`aws s3 cp
  s3://cowork-polly-tts-740353583786/briefs/<date>/brief.html -` — note the current
  `cowork-polly-tts` credentials in this environment lack `s3:ListBucket`/`GetObject` on
  that bucket, so this is done with delivery-side or owner credentials at build time, not
  during this ADR pass) and **diff the new delivery-side conversion's output against it**,
  confirming byte-for-byte (or visually-equivalent) parity **before this ships**. The
  Reviewer must treat this as a regression check on the rendered brief, not just a code
  move (AC-2a). *(The content-generation side stops producing `brief.html` entirely, so
  `deployment.json` step 2 and `audio_email.py`'s `BRIEF_HTML_PATH` input are removed as
  part of decoupling delivery — see the migration sketch, Phase 1.)*

- **The delivery Lambda is otherwise a thin wrapper around the EXISTING
  `deploy/managed-agent/pipeline/` code**, not a rewrite. `audio_email.py` already does
  Polly→S3→SES + subscriber fan-out + feedback-link embedding + S3 archival;
  `brief_history.py` and `feedback_token.py` are its siblings. The wrapper accepts the HTTP
  body, **derives the HTML (above)**, materializes the content, and calls the same
  `send_all(...)` / archival path — with `send_all(...)`'s `brief_html` argument now fed the
  **delivery-derived** HTML rather than a file read from `BRIEF_HTML_PATH`. **Reuse, not
  reinvention** — this preserves the "byte-for-byte in intent" delivery behavior (async
  Polly via `OutputUri`, SES From exactly `aibriefing@mschweier.com`, the fail-safe that
  never loses the brief over an audio glitch) that `CLAUDE.md` and ADR-0007 require.
  *(Consideration weighed and rejected: reusing an existing Lambda from another stack. There
  is no delivery Lambda
  today — delivery runs inside the microVM as a tool call. The welcome-send Lambda in
  `deploy/subscribers/` is a different, narrower concern (one-recipient welcome), so
  folding daily delivery into it would overload a public-subscribe-stack Lambda with the
  main daily-send path. A genuinely new, dedicated delivery stack is cleaner and keeps
  IAM blast radius contained.)*

- **The delivery-side IAM holds EXACTLY today's grants, no broader.** The delivery
  Lambda's execution role gets precisely what `MicroVmExecutionRole` /
  `deploy/iam-policy.json` grant today: Polly synth; S3 rw on
  `cowork-polly-tts-740353583786/*` (+ the `s3:ListBucket` prefix per ADR-0005); SES
  `SendEmail`/`SendRawEmail` gated by `ses:FromAddress: aibriefing@mschweier.com`;
  DynamoDB `Query` on `brief-subscribers`'s `status-index` GSI; read the feedback-token
  signing secret. **These grants move — they are not duplicated.** After the redesign,
  this delivery role is the *only* thing holding SES-to-subscriber rights; the
  content-generation execution context holds none (FR-1/AC-1). No new static access key
  is minted (FR-3/AC-3).

### The two additive content-generation artifacts: `candidates.json` and the new per-brief source-usage record (FR-8a)

Two durable, structured artifacts are emitted **by the content-generation side** on every run,
**additively** — neither changes the shipped brief:

- **`candidates.json`** (the stories-considered selection artifact) already exists: the
  `daily-ai-brief` skill writes it to `WORKING_FOLDER`, and on a production run
  `brief_history.archive_candidates_file()` (`brief_history.py:189`) archives it to
  `briefs/<date>/candidates.json` as a best-effort, non-gating step (established by the
  eval-harness epic — `docs/prd/eval-harness.md` FR-4/AC-5, ADR-0013 §D).

- **NEW: a per-brief source-usage record (rev. 2, FR-8a — realizes GitHub issue #28).** On
  **every** run (production or candidate), the pipeline emits a durable, structured record of
  which `sources.md`-listed sources were actually featured/used in that run's brief. This is a
  **direct sibling of `candidates.json`** — same additive, non-behavior-changing pattern — and
  should be built **by mirroring that precedent exactly, not by inventing a new pattern**:
  emitted as a file in `WORKING_FOLDER` and, on a production run, archived alongside the other
  outputs by a new best-effort `brief_history.archive_source_usage_file()` twin of
  `archive_candidates_file()` (a fixed filename constant next to `CANDIDATES_FILENAME`, the same
  "missing file is the expected case, never raise, never gate the send" fail-safe). On a
  **candidate** run it is retrieved via the same Claude-Platform-only mechanism as the other
  artifacts (below) — no AWS. Its production must **not** alter the shipped brief (the Reviewer
  confirms this the same way as for `candidates.json`).

  **Skill-content-bound vs. pipeline-wrapper-bound — the call.** `candidates.json` is produced by
  a **skill-content instruction** (the skill enumerates what it considered), because only the
  research agent knows the full candidate set. The source-usage record is the same kind of
  knowledge — *which of the named `sources.md` sources the agent actually drew on* is known only
  to the agent as it writes, not reconstructable faithfully from outside its reasoning. So
  **emitting it is skill-content-bound**, exactly like `candidates.json`, and is therefore subject
  to the (now **two-way**, see "Reconciling ADR-0008") lockstep: in-repo copy ↔ live Skills-API
  resource. Per the PRD's steer (favor the option that captures the signal faithfully with the
  least skill-content churn), a pipeline-wrapper-only approach was considered and rejected: the
  wrapper cannot see which sources the agent leaned on without re-deriving it (brittle
  string-matching over the brief text against `sources.md`), so it would under-report — the same
  fidelity risk the eval-harness PRD flags for `candidates.json`. The archival/retrieval half
  (the `brief_history` twin) is pipeline-wrapper code and is **not** lockstep-bound.

**How a candidate's artifacts are retrieved (any future caller — a manual/scripted check now, an
adapted harness later).** On `cloud`, the retrievable artifacts are the **brief markdown**, the
**listening-script text**, **`candidates.json`**, the **source-usage record**, and the run's
**cost/token data** — **not** brief HTML (delivery-derived, FR-2a) and **not** audio (no TTS for a
candidate run — owner-confirmed 2026-07-06). Retrieval is via the **Sessions events API**, not the
Files API: as I verified live (below), an agent writing an output path does **not** produce a
downloadable Files-API `file_id`, but a `cat <path>` `bash` tool_result returns the exact file body
in the session event stream (`GET /v1/sessions/{id}/events`). So the wrapper `cat`s each artifact
and a caller reads it out of the events. **This is stated as an intrinsic property of the new
system** — this ADR/epic wires up no consumer of it; the existing `deploy/eval/` harness's
adaptation to read artifacts this way (replacing its poll-based S3 read) is a later, separate epic.

## Decision 2b (recommended): how content generation authenticates to delivery — a shared bearer secret, fail-closed

**Recommendation: the content-generation side authenticates to `POST /deliver` with a
single shared bearer token**, held in Secrets Manager, checked by the delivery Lambda
with a constant-time compare — the exact pattern `deploy/eval/`'s reviewer secret
(ADR-0013 §E) and the feedback-token scheme (ADR-0011/0003) already establish.

- The bearer token is created **empty** in the delivery stack and populated out-of-band
  (repo convention: no secret in git/CDK). The `cloud` sandbox receives it as an
  environment variable via the environment's declarative `environment` config block
  (confirmed present on the cloud env config) or injected into the delivery-step prompt
  from a Secrets-Manager reference — **never** committed. The wrapper `curl`s
  `POST /deliver` with `Authorization: Bearer <token>`.
- **Fail-closed** (the security-engineer's standing bar, echoing the launcher's
  fail-closed signature check and `deploy/eval/`'s reviewer auth): a missing or invalid
  token yields **401**, never a fall-open to an unauthenticated send. The delivery
  endpoint is the *only* new surface that can email real subscribers — the very
  capability FR-1 strips from content generation — so its auth must be the tightest
  thing in the redesign.
- **Rejected alternatives (over-engineering for this shape):** IAM/SigV4 (would
  re-introduce an AWS identity on the content side — the exact thing FR-1 forbids —
  and defeats the whole point of moving to `cloud`); mTLS / signed requests (heavier
  key machinery than a bearer secret buys us for a single trusted caller); Cognito /
  a user pool (there is no human user here, just one service-to-service call). A shared
  bearer secret is the same weight as this repo's existing secret conventions and
  rotates with one `put-secret-value`.

## Decision 2c (recommended): git-native candidate versioning — one directory per candidate, one stable `agent_id` recorded as a plain `candidate.json` field, updated in place per sync (no git tag, no `registry.json`)

*(Reworked twice. **Rev. 1** proposed a single git-tracked `registry.json` mapping slug → live
resource IDs + commit. **Rev. 2** dropped that for an annotated **git tag per candidate-sync event**
recording the resulting live IDs, on the then-current — and now **corrected** — belief that an agent
was immutable and every candidate revision produced a **new `agent_id`** that had to be recorded
somewhere per revision. **This third pass** corrects that premise: agents support native
update-in-place versioning (`POST /v1/agents/{id}` with a required `version`; confirmed live, §"What
I verified live"), so a candidate keeps **one stable `agent_id` for its whole life** and is *updated*
on each sync. That collapses "one new id per revision" to "one id, ever" — so the only
not-derivable-from-git fact left is that single id (generated by Claude at the candidate's **first**
sync), which is best recorded as a **plain `agent_id` field in the candidate's own `candidate.json`**,
committed once as an ordinary git change. **No per-sync tag, no `registry.json`.**)*

**Recommendation: one directory per candidate under `deploy/candidates/<slug>/`, with separate,
independently-diffable files per dimension; the candidate's single stable `agent_id` (and its skill
version reference[s]) live as plain fields in ordinary tracked files, populated once at first sync;
each subsequent sync *updates* that same agent in place; historical *declaration* state is read via
git's own `git show <ref>:<path>`, and historical *live* state is read from Claude Platform's own
`GET /v1/agents/{id}/versions`. No git tag, no `registry.json`.** (`agents/` was the PRD's sketch
name; `deploy/candidates/` keeps it under the repo's existing `deploy/` per-surface tree, alongside
`subscribers/`, `feedback/`, `eval/`, `delivery/` — the exact top-level name is a minor call the
human may re-pick.)

Layout for a **single-agent** candidate (e.g. re-expressing today's production config as
candidate #1) — note there is **no** top-level `registry.json` and **no** git tag:

```
deploy/candidates/
  production-baseline/
    candidate.json                  # slug, description, the one-agent composition, schedule intent,
                                    #   AND "agent_id": "agent_..." — the one stable live id,
                                    #   written once at first sync, an ordinary committed field
    agent.json                      # the agent definition: name, description, tools, mcp_servers
    model.txt                       # the model id (e.g. claude-sonnet-5)      -- diffable alone
    system-prompt.md                # the agent system prompt                  -- diffable alone
    task-prompt.md                  # the deployment initial_prompt (the run task) -- diffable alone
    skills.json                     # [{skill_id, version}] concrete versions  -- diffable alone
    parameters.json                 # effort / thinking budget / other tunables -- diffable alone
    skill/                          # OPTIONAL: candidate-owned skill source, if this
      SKILL.md                      #   candidate ships its own skill content (see below)
      sources.md
```

- **Independently-diffable dimensions (FR-9/AC-9).** Model, system prompt(s), skill
  reference(s), and parameters are **separate files**, so a candidate diff shows exactly
  which dimension changed — the direct antidote to today's opaque ~3KB inline
  `initial_prompt`. `agent.json` holds only the non-prose structure (tools,
  `mcp_servers`); the prose lives in `.md` files that diff cleanly. The `agent_id` field in
  `candidate.json` is written **once** at first sync and then is stable — it does not change on
  subsequent syncs, so it never pollutes a declaration diff (a model/prompt change shows only in
  `model.txt`/`system-prompt.md`, never in the id).

- **Multi-agent candidates use the SAME structure (FR-10/AC-10) — with the coordinator's roster
  re-pinned on sub-agent updates (see the sync ordering below).** Because `multiagent`
  is a field **on one agent resource** (confirmed live), a coordinator + sub-agents
  candidate is expressed by adding a `multiagent.json` (or an `agents/` sub-array in
  `agent.json`) enumerating each sub-agent's own `entry` with its own
  model/system-prompt/skills/parameters — the coordinator is the top-level agent, the
  sub-agents are its `agents[].entry` list. No fundamentally different directory shape:
  a single-agent candidate simply has no `multiagent` block. `candidate.json` records the
  **coordinator's** stable `agent_id`; each **sub-agent** likewise keeps its own stable `agent_id`
  (recorded in the `multiagent.json`/roster entry). The important operational nuance the corrected
  primitive introduces: because **a coordinator does not automatically pick up a new version of a
  sub-agent it references** (docs-confirmed — it keeps the sub-agent version pinned when the
  coordinator was last updated, even if the roster reference omits `version`), updating a sub-agent
  requires a **follow-up update of the coordinator** to re-pin its roster to the new version. The
  sync script encodes this as an ordered two-step (below); the *directory layout* is unchanged by it.

- **Historical *declaration* state is read via git alone, no rollback (FR-12(a)/AC-12).** Because a
  candidate's declaration is just per-dimension files in `deploy/candidates/<slug>/`, any historical
  version is read with `git show <commit-or-tag>:deploy/candidates/<slug>/system-prompt.md` (etc.) —
  which reads the file's content at that ref **without touching HEAD or the working tree.** This is
  why the layout **must not** require reconstructing a whole candidate directory by checking out an
  old commit: reading individual files at a historical ref suffices, so nothing here forces a repo
  rollback. This answers the owner's exact question — "how will the eval system read a previous
  version of a prompt without rolling back the repo": `git show <ref>:<path>`, natively. (Git is the
  source of truth for the **declared** state you edit, review, and diff.)

- **Historical *live* state is read from Claude Platform natively (FR-12(b)/AC-12) — a second,
  complementary source the corrected primitive unlocks.** Because a candidate now keeps **one stable
  `agent_id`** and each sync *updates* it in place, Claude Platform holds the candidate's full
  operational history natively: `GET /v1/agents/{id}/versions` lists **every version the candidate
  has actually run as**, with full content and `updated_at` timestamps (confirmed live). So "what has
  this candidate actually run as, and when" is answered by a **single Platform API call against the
  one `agent_id`** — no git archaeology needed at all. The two sources are complementary and both
  legitimate: **git** is authoritative for the *declared source* at any point in git history (for
  editing, review, diffing, and reconstructing intent); **Platform's version list** is the
  operational source of truth for *what was actually live* (which the sync could, in principle, lag
  or a manual console edit could diverge from — so reading it directly is strictly more truthful for
  the "what ran" question than re-deriving it from git). This is a strict improvement over the
  second pass, where "what actually ran" could only be reconstructed from the per-sync tags.

- **The one not-derivable-from-git fact is a plain `agent_id` field, not an index (FR-12/AC-12).**
  A candidate's live `agent_id` is generated by Claude at its **first** sync and is **not derivable
  from git content alone** — so, exactly as the PRD's FR-12 permits, one **minimal** mapping from
  "this candidate" → "its live id" is genuinely necessary. The corrected primitive makes that mapping
  as small as it can possibly be: **one stable id per candidate, for life.** Recording it as a plain
  `"agent_id"` field in the candidate's own `candidate.json` (populated once at first sync, committed
  as a normal git change) satisfies FR-12 without standing up "a bespoke duplicate-of-git index that
  competes with git as the source of truth for *content*":
  - It is **not an index in the sense the owner pushed back on.** It does not grow (one field, not a
    slug→id table that accretes a row per candidate), it is **not rewritten on every sync** (it is
    written once and then stable — an unchanged agent id means no diff), and it does not duplicate any
    *content* git already versions (the model/prompts/skills/params — the things you actually edit —
    live in their own tracked files; the id is just "this candidate's one address"). It is the single
    stable identifier for the candidate, nothing more.
  - The concrete numeric **skill version(s)** the candidate pins are the same kind of
    occasionally-changing fact and live the same way — as plain fields in the tracked `skills.json`
    (e.g. `[{skill_id, version}]`), updated via a normal git commit when a new skill version is
    pushed. **No tag is needed there either**, by the same reasoning: a skill version is a fact that
    changes rarely and is small enough to be an ordinary committed field. *(Skills remain create-only
    for new versions — `POST /v1/skills/{id}/versions` — which is unaffected by this agent-versioning
    correction; only the way the chosen version is **recorded** matters here, and a plain field
    suffices.)*
  - **Why a plain field rather than the second pass's per-sync tag:** the tag existed to record a
    **new** `agent_id` **per revision** — a real need only under the (now-refuted) belief that every
    revision minted a new id. With one stable id for life, there is nothing per-revision to record:
    the id is written once and never changes, so a plain committed field carries it with zero
    ceremony, no tags to push to the remote, and no per-sync annotation to maintain. The tag
    mechanism is therefore **dropped entirely** (its "Alternatives considered" entry is revised
    below to reflect that it is no longer needed, not merely no longer preferred). The `environment_id`
    is not a per-candidate fact at all — the shared `cloud` environment is referenced at run/deploy
    time (Decision 1) and can live in the runbook or a single shared config, not per candidate.

- **Keeping every candidate registered forever costs nothing.** Managed Agents bills per **active
  session**, not per idle agent/skill definition (a settled fact the PRD relies on), so every
  candidate's agent stays registered indefinitely at no ongoing compute cost — earlier candidates are
  never deleted or archived to make room. Any past or present candidate is selectable for an
  experimental run or the production schedule by reading its stable `agent_id` straight from
  `candidate.json`, with **nothing to rebuild** (FR-11/AC-11).

- **The `sync` script is idempotent, git-native, and update-in-place (FR-12/AC-12).** Given a
  candidate directory, it:
  1. **Reads the candidate's stable ids from its tracked files** — `agent_id` from `candidate.json`
     (and each sub-agent's id from the roster, for a multi-agent candidate), and the pinned skill
     version(s) from `skills.json`. If `candidate.json` has **no** `agent_id` yet, this is a
     **first sync** (step 2a); otherwise it is an **update** (step 2b).
  2a. **First sync (create):** first, for any candidate-owned skill, create its skill version
      (`POST /v1/skills/{id}/versions`) and record the concrete numeric version in `skills.json` (so
      the agent can reference a pinned version). Then `POST /v1/agents` to create the agent (with its
      `multiagent` config inlined if multi-agent), capture the returned `agent_id` (and each
      sub-agent's id), and **write them into `candidate.json`/the roster as an ordinary commit.**
  2b. **Update (in place):** for each agent whose declaration changed since it was last synced, call
      `POST /v1/agents/{id}` with the current `version` (read from `GET /v1/agents/{id}`) and the new
      configuration. The platform's **own no-op detection** means an unchanged declaration creates no
      new version — but the script should still only call update for a *changed* declaration to avoid
      needless requests, and must pass the **current** `version` (a stale one returns **`409`**;
      on a `409`, re-read the current version and retry, never blindly overwrite). Skill-content
      changes push a new skill version and update `skills.json` as before.
  3. **Multi-agent ordering (the corrected nuance):** when a **sub-agent** was updated (new version,
     same id), the coordinator does **not** see the new version automatically — so the script must,
     **after** updating the sub-agent(s), perform a **follow-up update of the coordinator**
     (`POST /v1/agents/{coordinator_id}`) so its `multiagent.agents` roster re-pins to the new
     sub-agent version(s). This is an **ordered two-step per multi-agent sync**: (i) update the
     changed sub-agent(s); (ii) update the coordinator to reference the new version(s). A single-agent
     candidate has no step (ii).
  Re-running against an **unchanged** declaration is a full **no-op**: the ids are already in
  `candidate.json`, no declaration changed, so no create, no update call (and the platform would
  no-op any update anyway). There is **no tag to write or push** and **no JSON side-file to
  reconcile** — the sync's inputs (declaration) and its one persisted output (the `agent_id` field,
  written only at first sync) are both ordinary tracked files. No Anthropic key is committed; the
  script reads it from the environment, per repo convention. *(Compared to the second pass's tag
  mechanism, this removes the "must `git push` the tag to the remote or a fresh clone won't see the
  ids" operational cost entirely: the ids are committed in `candidate.json`, so they travel with a
  normal `git pull` like any other tracked content.)*

## Reconciling ADR-0008 (skill-content lockstep): the local Desktop fallback is dead, and (if the topology moves off image-baked skills) the image-rebuild step goes away

ADR-0008's original **three-way** lockstep (in-repo copy ↔ local Desktop copy ↔ live Skills-API
resource) plus its **2026-07-04 amendment** (a Skills-API push alone did **not** reach a running
session, because this repo's custom `worker.mjs` bakes the skill into the microVM image, so an image
rebuild was *also* required) are premised on the **current self-hosted, image-baked** topology
**and** on the local Desktop fallback still being a live participant. Two independent reconciliations
apply, and both must be written into ADR-0008 explicitly or a half-reconciled, silent-drift state —
the exact failure ADR-0008 exists to prevent — results:

- **The local Desktop fallback is retired/dead — the lockstep collapses to two-way,
  unconditionally (rev. 2, owner feedback #2).** The owner has stated plainly that the local Desktop
  fallback (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`) is **dead**: it will not run and
  will not be reactivated, and it must not influence this epic in any form. This is stronger than the
  prior "deactivated / snapshot / kept in lockstep only if reactivated" framing (in `CLAUDE.md` and
  this ADR's own rev.-1 pass). Accordingly, **ADR-0008's three-way lockstep collapses to a
  *two-way* lockstep: in-repo copy (`deploy/managed-agent/skills/daily-ai-brief/`) ↔ live Skills-API
  resource** (`skill_01H2qu83NwnJ5zqcbrqsCcJ6`). The local Desktop copy is **no longer a lockstep
  member — full stop, not "for now," not "unless reactivated."** There is **no reactivation hedge and
  no human-confirmation gate on this point** — the owner has already given that direction via this
  feedback. The **live Skills-API version stays authoritative** and must still be pushed + **confirmed**
  (ADR-0008 steps 4–5 stand); the **in-repo copy remains source-of-truth**. The Developer must update
  ADR-0008 to drop the Desktop member from the lockstep, and the Reviewer must confirm no stale note
  anywhere tells a future maintainer to keep the dead Desktop copy in lockstep.

- **The image-rebuild half of the amendment stops applying *if* the topology moves off image-baked
  skills (Decision 1 / `cloud` or a standard worker).** This reconciliation is **orthogonal** to the
  Desktop-fallback question above and turns on the topology choice, not on the fallback. On `cloud`
  (and with the standard self-hosted worker), skills download **dynamically per session** from the
  Skills API — the whole reason the amendment existed (a bespoke worker that never fetched skills at
  runtime) disappears with the microVM image itself. A Skills-API version push **is sufficient** to
  reach the session; there is no image to rebuild. Under the recommended Decision 1 (`cloud`), **this
  ADR supersedes ADR-0008's 2026-07-04 image-rebuild amendment**: the Developer must mark that
  amendment superseded-by-ADR-0014 for the cloud path and update the README §3a correction, so no
  stale note tells a future maintainer to rebuild an image that no longer exists. *(If the human were
  to keep production on `self_hosted` with the current custom worker, the image-rebuild step would
  still apply for the production skill — the two-way-lockstep reconciliation above is unconditional,
  but this image-rebuild reconciliation is contingent on the topology moving.)*

If a candidate ships its **own** `skill/` content (per-candidate skills, above), that candidate's
skill is a distinct Skills-API resource with its own version — it is **not** bound into the
production skill's lockstep at all, which is cleaner: candidate skill experiments cannot accidentally
disturb the live brief's skill. The new **source-usage record (FR-8a)**, being skill-content-driven
(Decision 2a), rides the same **two-way** production-skill lockstep as the rest of the skill content.

Net: **the redesign strictly simplifies the ADR-0008 burden** — the dead Desktop fallback drops out
of the lockstep unconditionally, and (on the recommended `cloud` topology) the image-rebuild step
disappears — but the Developer must make both reconciliations explicit in ADR-0008 and the README
rather than leave them implied.

## Migration / rollout sketch (consistent with PRD rev. 2 §8 phasing)

*(Rev. 2: the former "re-integrate the eval harness" phase is **removed** — that is a later, separate
epic; extracting Markdown→HTML into delivery is folded into Phase 1; the per-brief source-usage
record and the git-native versioning are added; the sketch ends with the redesigned system validated
**on its own terms**, not with `deploy/eval/` wired up.)*

1. **Decouple delivery + move Markdown→HTML into it (FR-1/FR-2/FR-2a/FR-3).** Stand up
   `deploy/delivery/` (HTTP API + delivery Lambda wrapping `pipeline/audio_email.py`; delivery-side
   IAM = today's grants, moved not duplicated; empty bearer secret populated out-of-band). **As part
   of the same decoupling, extract the ad hoc `markdown.markdown(...)` body conversion out of the
   content-generation agent and into the delivery Lambda** as a tested, deterministic,
   delivery-owned function (add `markdown` to `deploy/delivery/`'s `requirements.txt`; reuse
   `_html_with_header()`/`_html_with_unsubscribe_footer()` unchanged), and confirm its output
   byte-for-byte (or visually-equivalent) against a real recent production `brief.html`. Content
   generation stops producing brief HTML; `deployment.json` step 2 and the `BRIEF_HTML_PATH` input
   are removed. Nothing points at the new boundary yet.
2. **Candidate declaration + git-native versioning + sync (FR-9/FR-10/FR-11/FR-12).** Add
   `deploy/candidates/` (per-dimension files; **no `registry.json`, no git tag**), record each
   candidate's **one stable `agent_id` as a plain field in its `candidate.json`** (written once at
   first sync, an ordinary committed change) and its pinned skill version(s) in `skills.json`; rely on
   `git show <ref>:<path>` for historical **declaration** state (no repo rollback) and on
   `GET /v1/agents/{id}/versions` for historical **live** state; and add the idempotent `sync` script
   that **creates each candidate's agent once and thereafter *updates it in place*** (`POST
   /v1/agents/{id}` with the current `version`; `409` on a stale version → re-read and retry) without
   superseding earlier candidates. For a **multi-agent** candidate the sync is an ordered two-step —
   update the changed sub-agent(s), **then** update the coordinator to re-pin its `multiagent.agents`
   roster to the new sub-agent version(s), since a coordinator does not pick up a sub-agent's new
   version automatically.
3. **Pure-API candidate deploy + dynamic skills (FR-4/FR-5).** A candidate deploys via API
   calls only — no container build — against a shared `cloud` environment; a skill change
   reaches it via a Skills-API version push, no image rebuild (the ADR-0008 amendment's
   failure mode is structurally gone).
4. **Per-brief source-usage record (FR-8a).** Add the durable, structured per-run source-usage
   record (which `sources.md` sources were featured) — emitted by the skill (two-way-lockstep-bound,
   Decision 2a) and archived on a production run by a new `archive_source_usage_file()` twin of
   `archive_candidates_file()`; on a candidate run it is among the Sessions-events-retrieved
   artifacts. Additive; confirmed not to change the shipped brief. Realizes GitHub issue #28.
5. **Re-express the current production configuration as candidate #1 (validation baseline, FR-14).**
   Express today's live model/prompt/skill/params as `production-baseline/` and confirm a run on
   `cloud` produces an **unchanged** brief (content, structure, listening script), with the
   delivery-derived HTML confirmed equivalent (FR-2a) — the safety baseline before any production
   cut-over. It calls the new `deploy/delivery/` boundary for the AWS work.
6. **Validate the redesigned system on its own terms — no eval-harness wiring (AC-1…AC-14).** Deploy
   a candidate via API only (AC-4); change its skill with no image rebuild (AC-5); trigger a
   candidate run with no microVM/delivery Lambda and confirm no subscriber email and no delivery path
   touched (AC-6/AC-7); retrieve all content artifacts (brief markdown, listening-script text,
   `candidates.json`, source-usage record, cost data) via Claude-Platform APIs only — **no HTML, no
   audio** — **using a manual/scripted API check, not `deploy/eval/`** (AC-8/AC-8a); confirm the
   delivery-derived HTML matches the standardized design (AC-2a); confirm single-dimension diffs
   isolate their dimension (AC-9) and a multi-agent candidate is representable (AC-10); confirm every
   candidate persists selectably with nothing to rebuild (AC-11) and a historical declaration is
   retrievable via `git show <ref>:<path>` with no rollback and **no duplicate-of-git index present**
   (AC-12); confirm content generation holds no AWS delivery rights (AC-1) and delivery authenticates
   with no AWS identity on the content side (AC-2/AC-3).
7. **(Conditional, staged) production cut-over.** If (and only if) the ADR + human sign-off chose to
   move production runtime (e.g. to `cloud`): run the `cloud` production path in parallel/behind
   validation; confirm the owner-facing brief and the 06:07 weekday send are unchanged (AC-14);
   supersede the `self_hosted` deployment; **then** retire `deploy/managed-agent/cdk/` + `microvm/`.
   Never a hard swap. If production stays on `self_hosted`, this phase is a no-op for production.

## Alternatives considered

- **Option: retain `self_hosted` for everything (status quo topology).** Rejected as the
  recommendation. Its sole original justification (ADR-0004: `boto3` reaching AWS via the
  microVM IAM role) is eliminated by decoupling delivery. Keeping it means permanently
  carrying a launcher Lambda, a public webhook + WAF + signing secret, a microVM image,
  the `create-microvm-image` build cycle, and the `--platform`-pinning packaging fragility
  (README §Prerequisites) — real, ongoing operational and security surface — to run a
  once-a-day public-news job that needs none of it. It also **fails FR-4** (a candidate
  still needs an image rebuild) and **fails FR-6** (a candidate run still drags in AWS
  infrastructure) unless that path is separately re-plumbed. Would only win if this
  pipeline had a hard requirement `cloud` cannot meet (private
  data, a self-hosted-only capability it uses, or an egress-control need that must be
  IAM/VPC-governed) — **none of which is true here** (verified: no Memory dependency,
  public data, egress hardening was never in use).

- **Option: hybrid — `cloud` for candidate/eval, retain `self_hosted` for production.**
  A legitimate, more conservative middle. Rejected as the recommendation but **the natural
  fallback if the human is uneasy about moving production runtime.** It gets the big win
  (cheap, infra-free candidate eval) immediately, while leaving the live send on the
  battle-tested self-hosted path. Downsides: it keeps the entire microVM stack alive
  purely for production (so none of the operational-surface reduction lands), and it
  leaves **two** runtimes to reason about (a `cloud` candidate and a `self_hosted`
  production could subtly diverge — the exact drift the "re-express current config as
  candidate #1" validation is meant to catch). Because the validation in Phase 5 *already*
  proves the `cloud` path reproduces the brief before any cut-over, the staged
  cloud-for-everything recommendation captures the hybrid's safety **and** the full
  simplification — so full `cloud` is preferred, with this hybrid as the explicit
  de-risking fallback the human can choose.

- **Option: hybrid the other way — `cloud` for production, retain `self_hosted` for
  candidate/experimental runs.** Rejected outright: backwards. Candidate/experimental
  runs are exactly what most needs to be infra-free and delivery-free (FR-6/7); keeping
  *them* on the heavy microVM path defeats the epic's primary purpose while moving the
  riskier surface (production) to the new runtime. No rationale supports this direction.

- **Delivery boundary: nest inside an existing stack** (`deploy/subscribers/` or
  `deploy/managed-agent/`) instead of a new `deploy/delivery/`. Rejected for the same
  reason ADR-0012 kept feedback standalone: delivery is a distinct deploy lifecycle with
  its own IAM (the only holder of SES-to-subscriber rights post-redesign) and its own
  auth surface; a dedicated stack keeps that blast radius contained and reviewable the
  way the sibling stacks already are.

- **Delivery auth via IAM/SigV4 instead of a bearer token.** Rejected: it would put an
  AWS identity back on the content-generation side — the precise coupling FR-1 exists to
  remove — and is incompatible with a `cloud` sandbox that deliberately holds no AWS
  credentials. A bearer secret is the correct weight (Decision 2b).

- **Candidate-artifact retrieval via the Files API `file_id`** (the original task brief's
  assumption). Rejected because **live-refuted**: agent-written files do not become Files-API
  objects, and there is no session file sub-resource. The Sessions events API (`cat` → tool_result)
  is the confirmed working substitute and is how a candidate's artifacts are retrieved by any future
  caller — a manual/scripted API check now, or an adapted harness later (this ADR/epic wires up no
  such consumer; adapting `deploy/eval/` is a later, separate epic).

- **A standalone git-tracked `registry.json` mapping slug → live IDs + commit** (this ADR's own
  rev.-1 recommendation). Rejected: it is a **bespoke side-table that duplicates what git already
  tracks** and competes with git as the source of truth. It would be a shared, growing file (a row
  per candidate) that every sync rewrites (merge-prone), and reading a *historical* mapping would
  still require a git operation against that file's history — so it buys nothing over recording the
  one not-in-content bit (the live id) as a plain field on the candidate itself. Superseded by the
  **plain `agent_id` field in `candidate.json`** (Decision 2c): one stable id per candidate, written
  once, not a growing shared table.

- **A git tag per candidate-sync event recording the live IDs** (this ADR's own rev.-2
  recommendation). Rejected — **no longer needed once the agent-versioning premise was corrected.**
  The tag existed to record a **new `agent_id` per candidate revision**, which was a real need *only*
  under the rev.-2 belief that agents were immutable and every revision minted a new id. That belief
  was **live-refuted** in the third pass (`POST /v1/agents/{id}` updates in place under one stable id;
  see "What I verified live"), so a candidate keeps **one `agent_id` for life** and there is nothing
  per-revision to record — the one id is written once as a plain `candidate.json` field. A per-sync
  tag would now be pure ceremony (an annotation to write and `git push` to the remote on every sync,
  carrying an id that never changes). It is therefore dropped, not merely deprecated. *(The tag was a
  sound design for the primitive it was built on — the mistake was the primitive, corrected here — so
  this is not "the tag was wrong," it is "the tag became unnecessary once agents turned out to be
  updatable in place.")*

- **Candidate versioning keyed on the agent's native version primitive (the recommended mechanism,
  Decision 2c).** Adopted. `POST /v1/agents/{id}` with a required `version` updates the agent in
  place under a **stable `agent_id`**, incrementing a Platform-tracked version; `GET
  /v1/agents/{id}/versions` returns the full history (confirmed live). This gives historical *live*
  retrieval for free from the Platform side, alongside git's historical *declaration* retrieval
  (`git show <ref>:<path>`) — two complementary sources — and reduces the not-derivable-from-git
  record to a single stable field, no growing index and no per-sync tag. *(This entry was, in the
  second pass, listed as **rejected** "because agents are immutable with no version primitive (405 on
  PATCH/PUT)." That rejection rested on an **incomplete probe** — only PATCH/PUT/DELETE were tried,
  never the real `POST`-to-item update — and is corrected here: the premise was false, so the
  mechanism is now the recommendation, not a rejected alternative. The genuinely rejected alternatives
  are instead the `registry.json` and the per-sync tag above.)*

## Consequences

Positive (if the human ratifies):
- **Deploying a candidate becomes a pure API call** (FR-4) — no container build, no image
  push, no `create-microvm-image` cycle, no `--platform` packaging fragility. Candidate
  iteration for the cost-optimization epic drops from "rebuild + full AWS-stack stand-up
  per candidate" to "API calls in minutes" — the entire reason this redesign exists.
- **Content generation genuinely cannot email a subscriber** (FR-1): SES/Polly/S3/DynamoDB
  rights live only on the `deploy/delivery/` side; the `cloud` content-generation context
  holds no AWS credentials at all. A cleaner security posture than today's combined
  `MicroVmExecutionRole`.
- **A large, permanent reduction in operational surface**: retiring the launcher Lambda,
  webhook, WAF, signing secret, microVM image, and build/execution roles removes real
  standing infrastructure and its beta-churn/patching burden — matching the repo's
  serverless-frugality bias.
- **A candidate run is infra-free and delivery-free by construction** (FR-6/7), retrievable via
  Claude-Platform-only APIs, and skill changes reach candidates with no image rebuild (FR-5) — the
  ADR-0008 amendment's failure mode is structurally eliminated. This is an intrinsic property of the
  new system (useful to a manual/scripted check now and to a later eval-adaptation epic), not a
  feature of any harness this epic builds.
- **Candidate versioning is fully git-native, and gets a second native history source for free**
  (FR-9…FR-12): per-dimension declaration files diff cleanly; historical **declaration** state is read
  with `git show <ref>:<path>` (no repo rollback); a candidate keeps **one stable `agent_id` for
  life** (recorded as a plain field in `candidate.json`, written once) and is **updated in place**
  each sync, so Claude Platform natively holds the candidate's full **live** history via
  `GET /v1/agents/{id}/versions` — "what has this candidate actually run as" needs no git archaeology.
  **No bespoke `registry.json` and no per-sync git tag** — the corrected agent-versioning primitive
  removes the need for either. Single- and multi-agent alike, every version cheaply retained (billing
  is per-session). *(This is a simplification over the second pass, which — believing agents immutable
  — needed a per-sync tag to record a new id per revision; that is gone.)*
- **Delivery derives the brief HTML deterministically, with no AI cost** (FR-2a): a tested
  delivery-owned function replaces the agent's per-run `markdown.markdown(...)` improvisation, and the
  `_html_with_header()`/`_html_with_unsubscribe_footer()` chrome is unchanged — one consolidated,
  reviewable place for the standardized design, and one less thing the content agent does.
- **Every run emits a per-brief source-usage record** (FR-8a, realizing issue #28) as an additive
  sibling to `candidates.json`, seeding a later source-list-consolidation effort — with no change to
  the shipped brief.

Negative / follow-ups (named plainly):
- **This moves the live, subscriber-facing production runtime** — the single biggest risk.
  Mitigated by Phase 5's "re-express current config as candidate #1 and validate an
  unchanged brief" gate and a **staged, parallel** cut-over (never a hard swap, FR-14);
  the security review must confirm content generation holds no delivery rights (AC-1/AC-7)
  before cut-over. **The human should specifically weigh whether to take full
  cloud-for-everything now or stop at the hybrid (cloud eval, self-hosted production) as a
  more conservative first step** — both are supported by this ADR; the recommendation is
  full cloud, staged.
- **Moving Markdown→HTML derivation to delivery is a regression risk, not a free refactor (FR-2a).**
  Today's body conversion is agent-improvised and undocumented, so faithfully reproducing "the
  standardized design" requires reverse-engineering the exact current `markdown` output — the
  Developer must diff the new delivery-side conversion against a real recent production `brief.html`
  and confirm parity before ship (AC-2a), and `markdown` must be newly added to `deploy/delivery/`'s
  `requirements.txt` (it is not a listed dependency anywhere today). The Reviewer treats this as a
  rendered-brief regression check.
- **Candidate-artifact retrieval is via the Sessions events API, not the Files API — a build-time
  verification item.** Content recovery via `cat` → tool_result is confirmed for small files; the
  full daily brief (markdown + listening script + `candidates.json` + source-usage record) is
  multi-KB, and the Developer must confirm a full-size artifact survives the tool_result/event path
  intact (chunking/truncation limits) when standing up the manual/scripted retrieval check. **This
  is a property of the new system, not `deploy/eval/` re-plumbing** — wiring the existing harness to
  read artifacts this way (replacing its poll-based S3 read) is explicitly a later, separate epic and
  is **not** in this epic's scope (rev. 2, owner feedback #1/#5).
- **The `cloud` sandbox's outbound network is Anthropic-managed (`unrestricted`), not
  IAM/VPC-governed by us.** We were not using egress hardening (ADR-0006 left it as a
  future lever, and content generation now holds no AWS rights to abuse), so nothing in
  use is lost — but it is a control that lives in the `cloud` environment `networking`
  config rather than our account, and the ADR/README should record that.
- **A new authenticated delivery surface exists** that *can* email subscribers if misused
  (the capability FR-1 removes from content generation). The security review must confirm
  its bearer auth is fail-closed, its AWS grants are scoped no broader than today, and no
  new static key is minted (PRD §7 security note).
- **The redesign leans harder on beta surfaces** (`cloud` environments, dynamic skill
  loading, the Sessions events API for retrieval). The same "fail loudly, not silently
  skip" discipline applies; the README must record the beta headers/versions built
  against, and the pipeline must not silently degrade if a contract drifts.
- **Reversibility.** The candidate declarations (each with its stable `agent_id` recorded as a plain
  `candidate.json` field) and the delivery contract are all git-tracked and portable; every
  candidate's full live history is additionally recoverable from the Platform
  (`GET /v1/agents/{id}/versions`); the `self_hosted` stack (if retained during the staged cut-over,
  or reconstructable from git history if retired) is the fallback if `cloud` ever proves unsuitable in
  production. Nothing here is a one-way door before the Phase-5 validation gate.

## What was and wasn't confirmed (verification note)

**Confirmed live (2026-07-05, real curl against `api.anthropic.com`, probe resources
archived, production untouched):** agent+environment independence; a `cloud` environment
executing tools and returning terminal `idle` via the deployment `/run` trigger; the
`cloud` session `usage` + `span.model_request_end` `model_usage` shape being identical to
what `cost_miner.py` already parses (FR-14 works on cloud); **working `cloud` egress**
(`EGRESS_HTTP_200` to example.com; HTTP 401 from `api.anthropic.com`) with
`networking: {type: "unrestricted"}` **defaulted on this account** (correcting the PRD's
"disabled by default"); the rich `cloud` env config (`packages`/`networking`/`init_script`/
`environment`); **Files-API auto-`file_id` REFUTED** (empty `/v1/files`, no session file
sub-resource) with **content recoverable via the Sessions events API** (`cat` →
tool_result, and `write` tool_use `input.content`); **skills DO version**
(`latest_version` + a listable versions collection, three on the production skill);
**multi-agent as a first-class `multiagent` field on one agent resource**. *(One
finding from this batch — "agents immutable, no agent-version primitive" — was **wrong**
and is corrected below; see "Correction (third pass, 2026-07-06).")*

**Correction (third pass, 2026-07-06 — a prior finding was wrong).** The second pass
recorded, in this very section, "**agents immutable** (405 on PATCH/PUT, archive-not-delete)
with **no agent-version primitive**." **That was incorrect** — it rested on an incomplete
probe that tried only `PATCH`/`PUT`/`DELETE` on `/v1/agents/{id}` and never the actual update
call. Re-verified live (real curl against `api.anthropic.com`, `managed-agents-2026-04-01`,
prompted by the owner questioning the premise; probe agent created and archived afterward,
production untouched): **agents have a native update-in-place version primitive.**
`POST /v1/agents/{id}` (POST to the *item*, same URL as create) with a required `version` field
in the body updates the agent **under the same `agent_id`** and generates a new version —
confirmed `version` 1→2 on a probe with the **same** id returned; retrying with a stale
`"version": 1` returned **`409`** (optimistic concurrency); `GET /v1/agents/{id}/versions`
returned **both** versions with full content and `updated_at` (a genuine, complete history, the
same shape Skills versioning has). The docs
(`platform.claude.com/docs/en/managed-agents/agent-setup`) confirm this as standard behavior,
including **no-op detection** (no new version when an update changes nothing) and that
**coordinators are not updated automatically** (a coordinator keeps the sub-agent version pinned
at its own last update; to delegate to a new sub-agent version, the coordinator must itself be
updated). This correction is what Decision 2c (third pass) is built on: **one stable `agent_id`
per candidate for life, updated in place**, with the per-sync git tag from the second pass
dropped. (The **Deployments** API remains genuinely immutable / create-then-archive per README
§6 — the second pass wrongly assumed agents matched deployments; they do not.)

**Not fully confirmed (build-time verification items — flag for the Developer, do not
treat as settled):**
1. **Large-artifact retrieval via the events API.** Content recovery via `cat` →
   tool_result is confirmed for small files. The full set of retrievable artifacts (brief
   markdown + listening-script text + `candidates.json` + the source-usage record — **not**
   HTML, which is delivery-derived, and **not** audio) is multi-KB; the Developer must confirm
   a full-size artifact survives the tool_result/event path intact (chunking/truncation limits),
   and design a chunked-read fallback if a single `cat` result is capped, when standing up the
   manual/scripted retrieval check. If it does not hold, the Files API *may* still be usable via
   an **explicit** upload from within the session (not the automatic path I refuted) — untested.
2. **The exact `multiagent.agents[].entry` schema.** Confirmed the field exists and is
   validated; the precise `entry.type` values and sub-shape were not fully enumerated
   (my two guesses hit field-level validation). The Developer must pin the real schema
   before declaring a multi-agent candidate — not needed for single-agent candidate #1.
3. **`cloud` session terminal-status vocabulary under load.** The probes settled at
   `idle`. A long research+write session should be observed end-to-end to confirm it does not
   surface a different terminal state; a tolerant terminal-status recognizer (as the repo's
   existing session pollers already use) should hedge this.
4. **Whether the production research skill's web-fetching tooling works identically on
   `cloud`.** Egress is confirmed working; the specific fetch/search tools the skill
   relies on should be exercised on `cloud` during Phase 5's unchanged-brief validation
   before production cut-over.

**Items needing the owner's explicit sign-off beyond the core recommendation:**
- Ratify the environment topology: **full cloud-for-everything (staged)** vs. the
  conservative **hybrid (cloud eval, self-hosted production)** fallback. (Decision 1.)
- Confirm comfort with **retiring `deploy/managed-agent/cdk/` + `microvm/`** after the
  staged production cut-over.
- Confirm the **`deploy/delivery/` standalone stack + bearer-token auth** shape, **including that
  delivery now derives the brief HTML deterministically** (FR-2a) from the brief markdown — content
  generation no longer produces brief HTML, and the delivery-derived output is validated against a
  real production brief before ship (Decision 2a).
- Confirm the **`deploy/candidates/` layout with git-native versioning, reworked around the
  corrected agent-versioning primitive** (Decision 2c, third pass) — per-dimension declaration files;
  historical **declaration** state via `git show <ref>:<path>` (no repo rollback) **and** historical
  **live** state via `GET /v1/agents/{id}/versions`; each candidate keeping **one stable `agent_id`
  for life**, recorded as a **plain `agent_id` field in `candidate.json`** (written once at first
  sync) and **updated in place** each sync — **no git tag and no bespoke `registry.json`** (both from
  earlier passes are dropped); and, for multi-agent candidates, the sync's **ordered two-step**
  (update the changed sub-agent[s], then update the coordinator to re-pin its roster to the new
  version[s], since a coordinator does not pick up a sub-agent's new version automatically). Also that
  keeping every candidate agent registered forever (billing is per-session) is acceptable. *(This item
  is the specific place the third-pass correction lands for sign-off: the prior pass asked you to
  confirm a per-sync tag; this asks you to confirm the simpler plain-field design the corrected
  primitive enables.)*
- Confirm the **ADR-0008 reconciliation** (Decision "Reconciling ADR-0008"): the three-way lockstep
  collapses to **two-way** (in-repo ↔ live Skills-API) because the **local Desktop fallback is
  dead** — **unconditionally, with no reactivation hedge** (this is already the owner's stated
  direction via rev.-2 feedback #2, so this item is confirming the ADR reflects it, not re-deciding
  it); and, on the recommended `cloud` topology, the image-rebuild half of the 2026-07-04 amendment
  is superseded (no image to rebuild).
- Acknowledge the new **per-brief source-usage record** (FR-8a, issue #28): an additive,
  skill-content-driven sibling of `candidates.json`, emitted every run, not changing the shipped
  brief (Decision 2a).
- ~~Confirm the FR-8 PRD interpretation this ADR builds on~~ — **CONFIRMED by the owner,
  2026-07-06**: "The listening script is the output. No actual TTS for evals." A candidate run
  retrieves the listening-script *text* only; audio (Polly = AWS) is never synthesized
  or retrieved for a candidate run. Settled.
