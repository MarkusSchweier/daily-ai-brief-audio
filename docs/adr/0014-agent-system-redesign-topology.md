# 0014. Agent-system redesign: environment topology, decoupled delivery boundary, and git-native candidate versioning

- Status: **Decision 1 (topology) ACCEPTED — hybrid, ratified by the human 2026-07-06.** The
  other decisions (2a delivery boundary, 2b bearer auth, 2c candidate versioning, and the
  ADR-0008 reconciliation) remain **recommended, pending sign-off**, and the build-time
  verification items (esp. the Files-API/Sessions-events retrieval linkage) stay open. This is
  the Gate-0 decision for the agent-system-redesign epic (`docs/prd/agent-system-redesign.md`
  §7/§8). It is a major, cross-cutting, hard-to-reverse decision (it decides the runtime the live
  subscriber-facing pipeline runs on) and was therefore, per this repo's standing convention,
  escalated to the human — **who has now ratified the environment topology (Decision 1: the
  hybrid).** The delivery-boundary shape and candidate versioning layout remain recommended
  concretely and are not yet locked. Nothing is deployed to production yet.
  **AMENDED 2026-07-06 by [ADR-0015](0015-production-delivery-decoupling.md):** Decision 1 ratified the
  hybrid AND recorded that the Phase-7 production cut-over would be a **no-op** (production keeps
  delivering in-VM). ADR-0015 **reverses that specific sub-decision** on the owner's approval:
  production **content generation stays self-hosted** (the hybrid itself is unchanged), but production
  **delivery** (Polly/SES/S3/fan-out/HTML) moves out of the MicroVM to the `deploy/delivery/` boundary
  (full decouple; the MicroVM ends up with the same zero-AWS-delivery posture as a cloud candidate).
  Wherever this ADR below says "Phase 7 is a no-op" or "production delivers in-VM," it is superseded by
  ADR-0015 for the **delivery** half; the topology (where content generation runs) is not changed.
  **Decision 1's recommendation was reassessed on 2026-07-06 (fifth pass, below) in light of two
  live-confirmed `cloud` findings to the HYBRID (`cloud` for candidate/eval, `self_hosted` retained
  for production) rather than full cloud-for-everything, and the human RATIFIED the hybrid on
  2026-07-06 (sixth pass, below).**
- Date: 2026-07-05 (revised 2026-07-06 against PRD **revision 2**; revised again 2026-07-06 —
  **third pass** — to correct the agent-versioning premise; **fifth pass**, 2026-07-06 — reassessed
  Decision 1's recommendation to the hybrid on two live `cloud` findings, see the fifth-pass note
  below. The fourth pass was the transport-only amendment in Decision 2a. **Sixth pass**, 2026-07-06 —
  the human **ratified Decision 1 (the hybrid)**; Decision 1's status is now Accepted and a new
  Decision 2d records the delivery-side recent-priors read endpoint that closes the hybrid's
  eval-vs-production "read recent priors" fidelity gap. **Seventh pass**, 2026-07-06 — token-delivery
  correction: a live validation found the env-var-on-environment token-injection mechanism Decision 2d
  and 2b originally specified is **not settable via the current beta API**; Decision 2d now records that
  correction and recommends a **short-lived HMAC-signed read token** as the permanent mechanism —
  flagged for the human's sign-off; the interim runtime-injection approach stays usable until then.)
- Deciders: architect (Claude, recommendation); **human — RATIFIED Decision 1 (topology: hybrid) on
  2026-07-06; sign-off on Decisions 2a/2b/2c/2d still pending.**

> **Revision note (2026-07-06 — SEVENTH pass — token-delivery correction to Decision 2d (and a
> corrected cross-reference in Decision 2b), prompted by a live validation of the `GET /recent-briefs`
> endpoint).** Documentation-only; no code, skill, or `deploy/` file edited (confirmed via
> `git diff --name-only`: only this ADR changed). One substantive correction with a recommendation:
> 1. **The env-var-on-environment token-injection mechanism does NOT work on the current beta.** Decision
>    2d (and Decision 2b) originally specified injecting the bearer token via the shared `cloud`
>    environment's declarative `config.environment` (env-vars) block. A live validation proved that block
>    is **not settable via the current beta Managed Agents API** — `POST /v1/environments` with
>    `config.environment` returns `400 "Extra inputs are not permitted"`; the field is read-only/reserved,
>    and so are `config.init_script` and `config.packages` (I re-verified this live: only `config.type` and
>    `config.networking` are settable at create). The **only** working per-run injection channel today is
>    the deployment's `initial_events` (the task prompt), which lands the token in the session transcript.
> 2. **Recommended permanent mechanism: a short-lived, HMAC-signed read token, minted per run and verified
>    with an `exp` claim.** Reusing the ADR-0011/0012 feedback signed-token scheme already vendored into
>    the delivery Lambda (`feedback_token.py`), plus a small `exp`-carrying variant, a transcript-leaked
>    token dies within minutes and needs **no per-run rotation** — the property the deferred, many-run eval
>    epic needs — for near-zero added machinery. Signed with the **existing** read secret; no new secret,
>    no new IAM; FR-1/FR-7 auth-separation and the whole endpoint contract are **unchanged**. **Flagged for
>    the human's sign-off** (affects the eval epic). The **interim** approach used during validation (static
>    token via `initial_events`, read secret rotated + Lambda cold-started immediately after) is confirmed
>    **fine to keep using until the human ratifies**, given the read token's low sensitivity (already-public
>    content, structurally auth-separated from the send path). Sections touched by this pass: the
>    top-of-file date line (this pass added), Decision 2d's heading amendment note + a new "Correction
>    (2026-07-06)" subsection + its retracted "how the token reaches the sandbox" paragraph, Decision 2b's
>    token-delivery bullet (corrected cross-reference; its core bearer-secret/fail-closed recommendation
>    unchanged), and the sign-off items (a new token-delivery item). **Not touched:** Decision 1, Decisions
>    2a/2c, the ADR-0008 reconciliation, and every other part of Decision 2d (the endpoint contract, the
>    separate read-only secret, the no-new-IAM property, the additive-to-production guarantee).
>
> **Revision note (2026-07-06 — SIXTH pass — the human RATIFIED Decision 1 (the hybrid), and a new
> Decision 2d records the delivery-side recent-priors read endpoint that closes the hybrid's
> eval-vs-production fidelity gap).** Two changes, both documentation-only (no code, skill, or `deploy/`
> file edited):
> 1. **Decision 1 is now ACCEPTED — hybrid, ratified by the human 2026-07-06.** The fifth pass
>    reassessed the recommendation from full-cloud to the hybrid (`cloud` for candidate/eval,
>    `self_hosted` retained for production) on Finding 2; the human has now ratified that topology. Its
>    heading/status become Accepted; the full-cloud option stays fully documented as "considered, not
>    chosen" (the leading alternative). Genuinely still-open items are unaffected — the delivery-boundary
>    shape (2a), bearer auth (2b), candidate versioning (2c), the ADR-0008 reconciliation, and the
>    build-time verification items (esp. the Files-API/Sessions-events retrieval linkage) remain
>    recommended/pending, exactly as before. This pass ratifies **only** the topology call.
> 2. **New Decision 2d — the recent-priors read endpoint (`GET /recent-briefs`).** The hybrid creates a
>    real eval-vs-production fidelity gap ("Difference B"): production (self-hosted) reads the last few
>    days' briefs from S3 first (via `audio_email.py read-recent-briefs` →
>    `brief_history.read_recent_prior_briefs()`) so it can avoid repeating recent stories, but a `cloud`
>    candidate has no AWS access and currently skips that step (see the `production-baseline/task-prompt.md`
>    "this candidate does NOT have access to any prior briefs" note) — so a cloud-eval candidate can
>    repeat a story production would not. Decision 2d closes that gap by exposing recent-priors *reading*
>    (only) through the already-decoupled `deploy/delivery/` boundary — the one place that holds AWS
>    credentials and already has the S3 briefs-bucket read IAM. Its **centerpiece is the auth-separation
>    decision**: the read capability must NOT confer the send capability (FR-1/FR-7 — a candidate run must
>    never be able to trigger a real delivery/send), so `GET /recent-briefs` gets its **own separate,
>    read-only bearer secret**, distinct from the `POST /deliver` delivery bearer secret. The endpoint is
>    a plain synchronous `GET` (reading ~3 small markdown objects is well under the HTTP-API 30s ceiling —
>    unlike `POST /deliver`), needs **no new IAM** (the delivery Lambda's existing `S3ListBriefsPrefix` +
>    `S3AudioReadWrite` grants already cover it), and is **purely additive** — production's own S3-backed
>    `read-recent-briefs` step is untouched (FR-14/AC-14). Sections touched by this pass: the top-of-file
>    status/date/deciders lines, Decision 1's heading + status lead (Accepted), a new Decision 2d section,
>    and the sign-off items (Decision 1 marked ratified; 2d added). **Not touched:** Decisions 2a/2b/2c and
>    the ADR-0008 reconciliation are unchanged (2d reuses 2a's contract-version discipline and 2b's
>    fail-closed bearer pattern by reference; it does not alter them).
>
> **Revision note (2026-07-06 — FIFTH pass — Decision 1's recommendation reassessed from full-cloud to
> the hybrid, on two live-confirmed `cloud` findings from a Phase-5 candidate run).** A live candidate
> run on a `cloud` environment surfaced two real constraints, both then empirically confirmed, and
> both folded into Decision 1's actual reasoning (and the "What I verified live" section):
> **Finding 1 — `web_search` transient 429:** a Phase-5 cloud run saw all five `web_search` calls fail
> with a Brave-originated `HTTP 429`, but this was proven a **transient Brave-backend blip** (a
> concurrent direct Messages-API `web_search` succeeded with headroom; two fresh single-`web_search`
> cloud sessions ~47 s apart both succeeded cleanly). It routes through the same service on any
> environment type and is a fallback-of-a-fallback in the skill, so it **does NOT weigh against
> `cloud`** — flagged explicitly so it is not mistaken for a strike.
> **Finding 2 — `cloud` egress safety-blocklist:** four curated `sources.md` domains
> (`theverge.com`/`arstechnica.com`/`reddit.com`/`reuters.com`) are permanently blocked at `cloud`'s
> safety layer (`403 hostname_blocked`; `web_fetch` → `url_not_allowed`) with **no config workaround**
> (a `limited`-networking env explicitly allow-listing `theverge.com` still 403'd), whereas
> `self_hosted` (customer AWS egress) is not subject to it. That is a real `cloud`-only production
> content-coverage constraint. Weighing it honestly (the four are all Tier 4/7 of 47 sources; Tiers
> 1–3 are reachable; the Phase-5 cloud brief still came out equivalent — bounded but real), **I now
> recommend the hybrid (`cloud` candidate/eval, `self_hosted` production)** — which the prior passes
> already carried as the named fallback — because the epic's entire primary value lands identically
> under the hybrid while production is spared a permanent source-coverage ceiling. **Consequence for
> rollout: Phase 7 (production cut-over to `cloud`) becomes a no-op / not-done and `deploy/managed-agent/cdk/`
> + `microvm/` are retained; Phase 1 (delivery decoupling) still delivers its full value.** Full
> cloud-for-everything remains fully specified as the leading alternative the human may ratify instead.
> **This is the big topology call and requires the human's ratification — I present the reassessment,
> I do not unilaterally flip it.** Sections touched by this pass: Decision 1 (heading, lead, points
> 4/5, the honest-costs analysis, staging/retirement, rollout consequence), the "What I verified live"
> section (the two findings + an egress-bullet refinement), the "Reconciling ADR-0008" image-rebuild
> half (now retained for production under the hybrid), the "Alternatives considered" self-hosted/hybrid
> entries (hybrid adopted; a new full-cloud entry as leading alternative), Consequences (topology +
> source-coverage bullets, reversibility), and the sign-off items. **Not touched:** Decisions 2a
> (delivery boundary), 2b (bearer auth), 2c (candidate versioning) — the delivery decoupling and
> candidate mechanism are independent of the topology choice and land under either option. This is a
> documentation/analysis pass; no code, skill, or `deploy/` file was edited.
>
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
  `curl` to the decoupled delivery endpoint both need it. **Refinement (2026-07-06, see the
  two `cloud` retrieval findings below):** `unrestricted` egress works in general (any
  domain not on Anthropic's blocklist, and the delivery-endpoint `curl` in particular), but
  a live Phase-5 cloud run then surfaced that a *handful of specific curated sources* are
  hard-blocked at the sandbox's safety layer regardless of networking mode — a real, bounded
  `cloud`-only constraint folded into Decision 1 below. It does not change that egress works
  broadly; it bounds *which* domains a `cloud` sandbox can reach.

- **`cloud` web_search transient failure — a minor known-limitation, NOT a topology blocker
  (live-confirmed 2026-07-06).** During a live Phase-5 candidate run on `cloud`, all five
  `web_search` calls failed with a Brave-originated `HTTP 429 no_brave_error_type` — including
  serialized calls ~90 s apart, which a per-second rate limit cannot explain (a 30/s limit
  resets every second; the org's console limit is "30 searches/second" and is raisable via
  Anthropic sales, but 30/s was not the cause). This was empirically established to be a
  **transient Brave-backend blip**, not a reproducible `cloud`-path constraint: (a) a direct
  Messages-API `web_search` succeeded cleanly at the same time (8 results, huge rate-limit
  headroom, no web_search-specific limit even surfaced — so the account is not
  quota-exhausted); and (b) **the decisive retest** — two fresh single-`web_search`
  cloud-agent sessions ~47 s apart — **both succeeded with real results, no 429.** (Honest
  caveat: two clean data points at today's backend state, not proof it can never fail — but
  it directly refutes "the `cloud` web_search path is specially rate-limited.") **Bearing on
  Decision 1: this does NOT weigh against `cloud`.** Transient search blips are possible on
  *any* backend (self-hosted included — `web_search` routes through the same Anthropic/Brave
  service regardless of environment type); and in this skill `web_search` is already a
  fallback-of-a-fallback (feeds → same-outlet HTML → `web_search`), so an occasional transient
  failure is low-impact and, if desired, mitigable by retry-robustness in the skill. This is
  a minor operational note, not a differentiator.

- **`cloud` egress safety-blocklist hard-blocks a handful of curated `sources.md` domains —
  a REAL, unconditional `cloud`-only constraint (live-confirmed 2026-07-06).** The same
  Phase-5 run surfaced that several `sources.md` domains — **`theverge.com`,
  `arstechnica.com`, `reddit.com`, `reuters.com`** — are blocked at the `cloud` sandbox's
  egress safety layer: a raw `curl` from inside the sandbox returns **`HTTP 403` with header
  `x-block-reason: hostname_blocked`**, and the `web_fetch` tool returns **`url_not_allowed`**.
  Other domains (TechCrunch, Anthropic's own site, `example.com`) work fine. This is
  **not a networking-config problem and there is no config workaround on `cloud`:** the
  decisive test created a **new** `cloud` environment with
  `networking: {type: "limited", allowed_hosts: ["theverge.com","www.theverge.com"],
  allow_package_managers: true}`, and raw `curl` to `theverge.com` **still** returned
  `403 hostname_blocked` **and** `web_fetch` **still** returned `url_not_allowed` — the safety
  blocklist is applied **unconditionally, independent of `networking` mode**. The official docs
  (`platform.claude.com/docs/en/managed-agents/environments.md`) confirm the mechanism:
  `unrestricted` networking = "full outbound access EXCEPT a general safety blocklist," and the
  `networking` field "does not affect the allowed domains for the web_search or web_fetch
  tools." Crucially, the docs describe this blocklist **only for `cloud` environments**;
  `self_hosted` sandboxes run on the customer's own AWS network egress (this repo's current
  production path) and are **not** subject to Anthropic's cloud safety blocklist. **This is a
  genuine, concrete `cloud`-vs-`self_hosted` differentiator** — on `cloud`, this handful of
  sources is permanently unreachable by any in-sandbox retrieval; on `self_hosted`, they are
  reachable. Its bearing on the topology recommendation (with the source-tier impact quantified)
  is worked through in Decision 1 below. *(Incidental correction to note here: environments
  CAN be archived — `POST /v1/environments/{id}/archive` → 200 — and deleted; if any prior
  probe note implied otherwise, this supersedes it. This ADR does not, in fact, claim
  environments are un-archivable anywhere.)*

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
  Agent-create accepts `multiagent: {type: "coordinator", agents: [{"type":"agent","id":<sub_agent_id>}]}`
  — each roster item is DIRECTLY a reference object (`{"type":"agent","id":...}`, with an optional
  `"version"` to pin), **not** an `entry`-wrapped object. **[CORRECTED 2026-07-07]** The original probe
  used an `entry`-wrapped shape and only reached `multiagent.agents[0].entry.type: Field required`; the
  correct shape was then confirmed live (by syncing real coordinators) and against the official docs
  (platform.claude.com/docs/en/managed-agents/multi-agent). So the coordinator is **one** agent
  resource whose roster **references N separate sub-agent resources by id** — each sub-agent is its own
  agent resource with its own stable `agent_id` (exactly what `deploy/candidates/`'s sync creates:
  sub-agents first, then a coordinator referencing them). The docs also confirm **all agents in a
  session share one sandbox filesystem** (only per-agent context/tools are isolated), so the
  decomposition candidates' `/workspace` artifact hand-off is valid.

## Decision 1 (ACCEPTED — hybrid, ratified by the human 2026-07-06): Hybrid — `cloud` for candidate/eval, retain `self_hosted` for production

> **Status: ACCEPTED. The human ratified the hybrid on 2026-07-06** (`cloud` for candidate/eval,
> `self_hosted` retained for production). The recommendation was reassessed 2026-07-06 to the hybrid in
> light of a newly-confirmed `cloud`-only constraint (Finding 2, "What I verified live"); the human has
> now confirmed that call. The full-cloud alternative below remains fully documented as **considered,
> not chosen** — it stays a legitimate future option, but the topology is now settled as the hybrid.
> **Rollout consequence of the ratified hybrid: Phase 7 (production cut-over to `cloud`) is a NO-OP /
> not-done, and `deploy/managed-agent/cdk/` + `microvm/` are RETAINED** to run production. The
> discussion below is preserved as the reasoning that led to the ratified decision.
>
> *(The reasoning as originally written for the fifth-pass recommendation is retained verbatim below.)*
> The earlier passes of this ADR recommended
> **full cloud-for-everything (staged)** with the hybrid as the explicit fallback. A live Phase-5
> candidate run then surfaced a real, unconditional `cloud`-only egress constraint — Anthropic's
> `cloud` safety blocklist permanently blocks a handful of curated `sources.md` domains
> (`theverge.com`, `arstechnica.com`, `reddit.com`, `reuters.com`), with **no config workaround**,
> whereas `self_hosted` (the customer's own AWS egress) is not subject to it. Weighing this honestly
> (below), **I now recommend the more conservative hybrid: run candidates/eval on `cloud`, and keep
> production on the existing `self_hosted` path** — which the prior passes already carried as the
> named fallback. The core value of the epic (cheap, infra-free candidate iteration + the delivery
> decoupling) lands **identically** under the hybrid; what changes is that production is spared a
> permanent, unfixable source-coverage ceiling on the one surface where reaching every curated
> source actually matters. The full-cloud option remains fully specified below as the alternative
> the human may still choose (its trade-off is now explicit).

**Decision (ratified by the human 2026-07-06): adopt the `cloud` environment type for candidate/eval
use, and RETAIN the existing `self_hosted` path (`deploy/managed-agent/cdk/` +
`deploy/managed-agent/microvm/`) for production.**
Eval/candidate work moves to `cloud` immediately at zero production risk (an infra-free, delivery-free
runtime for arbitrary candidates); the live weekday brief keeps running on the battle-tested
self-hosted microVM path, so it continues to reach **every** curated source. This captures the epic's
entire primary goal — a pure-API-deployed, infra-free candidate mechanism decoupled from delivery —
without moving the subscriber-facing production runtime onto a runtime with a permanent, config-
unfixable source-coverage ceiling.

*The full cloud-for-everything option (the prior passes' recommendation) is preserved as the leading
alternative in "Alternatives considered" and remains a legitimate choice the human may ratify instead;
the reasoning below establishes why `cloud` is the right runtime for candidates/eval unconditionally,
and where — solely because of Finding 2 — production is the one surface I now recommend leaving on
`self_hosted`.*

The evidence establishes `cloud` as the correct runtime for candidate/eval work, and (absent Finding
2) would have made cloud-for-everything the strongest option:

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
   self-hosted or a hybrid would be warranted on data-residency grounds — it is not. Note
   this is separate from Finding 2's *source-reachability* asymmetry, which is a
   content-quality concern, not a data-residency one, and is what actually tips the
   production recommendation toward the hybrid below.)

5. **No self-hosted-only *platform feature* this pipeline depends on is lost — but see
   Finding 2 for a self-hosted-only *reachability* advantage that does matter.** The PRD
   flagged the docs' note that the `Memory` feature is "not available on `cloud`." **This
   pipeline does not use Managed Agents Memory** — cross-run "yesterday's brief"
   persistence is done via S3 (`brief_history.py`, ADR-0005), read at session start by
   the wrapper, entirely outside any platform Memory feature. So the one asymmetric
   *platform capability* the PRD worried about is irrelevant here. (The reverse — a
   `cloud`-only capability self-hosted lacks — does not bind us either; nothing here needs
   it.) **However**, FR-13/AC-13 also asks whether the production pipeline *relies on
   anything* `cloud` would lose, and the live Phase-5 run answered that with a genuine
   one: `self_hosted`'s **unrestricted-by-Anthropic egress** reaches the four blocklisted
   `sources.md` domains that `cloud` permanently cannot (Finding 2, worked through in the
   honest-costs analysis below). That is not a "platform feature," but it is a concrete
   self-hosted-only advantage the production content-gathering step does use — the crux of
   the reassessed recommendation.

### The honest costs of `cloud`, and why they now tip the *production* recommendation to the hybrid

Two live-confirmed findings (2026-07-06, "What I verified live") bear on the topology call. I weigh
each honestly and state which way it cuts — because it matters that the human not read both as strikes
against `cloud`.

**Finding 1 (`web_search` transient 429) — does NOT weigh against `cloud`.** A Phase-5 cloud run saw
all five `web_search` calls fail with a Brave-originated `HTTP 429`, but this was empirically
established as a **transient Brave-backend blip**, not a `cloud`-path constraint: a direct Messages-API
`web_search` succeeded at the same time with huge headroom, and — decisively — two fresh
single-`web_search` cloud-agent sessions ~47 s apart both succeeded cleanly with no 429. `web_search`
routes through the same Anthropic/Brave service on **any** environment type, so a transient blip is not
a `cloud`-vs-`self_hosted` differentiator; and in this skill `web_search` is only a
fallback-of-a-fallback (feeds → same-outlet HTML → `web_search`), so its occasional failure is
low-impact and mitigable by skill-side retry-robustness. **I flag this explicitly so the human does not
count it against `cloud`: it is a minor, environment-agnostic operational note, not a reason to prefer
self-hosted.**

**Finding 2 (`cloud` egress safety-blocklist) — a real, unconditional `cloud`-only constraint that
DOES weigh against `cloud` for production.** Four `sources.md` domains — `theverge.com`,
`arstechnica.com`, `reddit.com`, `reuters.com` — are permanently hard-blocked at the `cloud` sandbox's
safety layer (`403 hostname_blocked` on raw `curl`; `url_not_allowed` on `web_fetch`), and the decisive
test proved there is **no config workaround** (a `limited`-networking env explicitly allow-listing
`theverge.com` still 403'd). `self_hosted` runs on the customer's own AWS egress and is **not** subject
to this blocklist, so on `self_hosted` these four sources remain reachable. This is a genuine content-
gathering asymmetry between the two runtimes — exactly the kind FR-13/AC-13 requires the ADR to weigh.

**How much does losing those four sources actually cost the brief? — quantified, not hand-waved.** The
skill's `sources.md` lists **47 bulleted source entries across 9 tiers**. The four blocked domains sit
entirely in the **mid/lower tiers**:
- **Tier 4 — Tech press:** The Verge, Ars Technica, Reuters (3 of 6 Tier-4 entries; TechCrunch, Axios,
  and VentureBeat's category feed remain reachable).
- **Tier 7 — Community:** Reddit (r/LocalLLaMA + r/MachineLearning) (1 of 3 Tier-7 entries; Hacker
  News, GitHub Trending, HF Trending remain reachable).

**None** of the four touches the brief's authoritative backbone — **Tier 1 (frontier labs — the
primary sources), Tier 2 (papers), Tier 3 (benchmarks)** are **entirely reachable on `cloud`.** So the
impact is real but **bounded**: the brief draws from ~30+ sources across the tier stack, and the loss
is four mid/lower-tier press/community outlets whose stories are frequently *also* covered by reachable
outlets (a Verge/Reuters scoop typically shows up via TechCrunch, the labs' own posts, or the Tier-6
newsletters). Consistent with that, **the Phase-5 cloud brief came out structurally and qualitatively
equivalent to production despite these four being blocked.** I neither overstate this (it is not
"cloud can't produce the brief") nor dismiss it (it is a permanent, unfixable ceiling on which curated
sources production can ever reach).

**Where that lands the recommendation.** The distinction that matters is *which surface* pays this cost:
- For **candidate/eval**, the loss is **immaterial** — candidates are compared *against each other* on
  a level playing field, not judged on absolute source coverage, and every candidate on `cloud` faces
  the identical blocklist. `cloud` is unambiguously the right runtime here (all of the reasoning above:
  infra-free, delivery-free, pure-API deploy, dynamic skills). **No reason to keep eval on self-hosted.**
- For **production**, the calculus differs. Production is the one surface where reaching *every* curated
  source genuinely matters — it is the real daily brief to real subscribers, with no experimentation
  upside to offset a permanent content ceiling. The **entire primary value of this epic** (cheap,
  infra-free candidate iteration + delivery decoupling) is delivered **whether or not production moves
  to `cloud`** — moving production is a *secondary* benefit (operational-surface reduction) that Finding
  2 now shows carries a real, unfixable content cost. Since the hybrid **avoids that cost for free**
  (production simply stays where it already reliably runs) **without sacrificing any of the epic's core
  goal**, the balance now favors the hybrid for production. That is the reassessment: not "cloud is
  bad," but "moving *production* to cloud trades a permanent source-coverage ceiling for an operational
  saving that the epic does not need, so don't."

**The full-cloud option is still legitimate — here is what would make its source loss acceptable.** If
the human decides the operational-surface reduction (retiring the whole microVM stack) is worth more
than reaching those four sources, cloud-for-everything remains a defensible choice, and the ADR keeps
it fully specified (Decision 1's original recommendation, now the leading "Alternatives considered"
entry). The blocked-domain loss would be acceptable if: the brief's authoritative backbone (Tiers 1–3,
all reachable) is deemed sufficient; the skill's existing fallback chain (feeds → same-outlet HTML →
`web_search`) is understood to already cover *most* of what the blocked outlets would carry via
reachable outlets and the search tool (minus only what is *uniquely* on those four domains and
nowhere else); and the Phase-5 "unchanged brief" validation is accepted as evidence the delta is
tolerable in practice. That is a real, values-based trade the human may take — I simply no longer
recommend it as the default now that the cost is concrete.

**The other, previously-noted `cloud` cost (egress not IAM/VPC-governed) — unchanged and minor.** The
`cloud` sandbox's outbound network is Anthropic-managed (`unrestricted`) rather than governed by *our*
IAM/VPC. Egress hardening was a documented "available future lever" ADR-0006 deliberately did **not**
pull (the research step needs broad public web access anyway, and least privilege is enforced at the
IAM layer, which for content generation will now be **empty** of AWS rights). So we are not losing a
control we were using. This point stands regardless of the hybrid-vs-full-cloud choice and is not, by
itself, decisive either way.

**Why staged / reversible, whichever way the human rules (FR-14).** Production emails real subscribers
every weekday, so nothing about production changes as a hard swap under *either* option:
- **Under the recommended hybrid**, production simply **stays on `self_hosted`** — there is no
  production cut-over at all, so the largest regression risk (moving the live runtime) is avoided
  outright. `deploy/managed-agent/cdk/` + `microvm/` are **retained, not retired.** (See the rollout
  consequence below.)
- **If the human chooses full cloud instead**, the cut-over is staged exactly as the prior passes
  specified: re-express today's exact configuration as candidate #1 on `cloud`, validate it produces an
  unchanged brief (content, structure, listening script), run it in parallel/behind validation, and
  only then supersede the live `self_hosted` deployment — mirroring the parallel-run discipline
  ADR-0006/0007 established for the original migration. Retiring `cdk/` + `microvm/` happens **after**
  that validation, never before.

**Rollout consequence of the reassessed (hybrid) recommendation.** If the hybrid is ratified,
**Phase 7 (the conditional production cut-over to `cloud`) becomes a no-op / not-done, and production
stays on `self_hosted`.** Critically, **this does not diminish the rest of the epic**: Phase 1 (the
delivery decoupling — content generation loses all AWS delivery rights; delivery becomes the
authenticated `deploy/delivery/` boundary that also derives the brief HTML deterministically) still
delivers its full value, and Phases 2–6 (git-native candidate declarations, pure-API candidate deploy,
dynamic skills, the source-usage record, the candidate/eval runs on `cloud`) all land unchanged. The
hybrid keeps the microVM stack alive *purely to run production*, which is precisely what it already
does today reliably — so the redesign ships its entire primary goal (candidates/eval on `cloud`,
delivery decoupled) **without a risky production migration.** One consequence to note honestly: under
the hybrid there are **two runtimes to reason about** (a `cloud` candidate runtime and a `self_hosted`
production runtime that could, in principle, subtly diverge) — the "re-express current config as
candidate #1 and validate an unchanged brief" step (Phase 5) still runs and is the guard against that
drift, even though under the hybrid it validates the `cloud` *candidate* path rather than a production
cut-over. Under full cloud, that drift risk collapses to one runtime — a genuine (but, given Finding 2,
now outweighed) point in full cloud's favor, recorded in the "Alternatives considered" hybrid entry.

**What is NOT retired (under either option):** the `deploy/managed-agent/` directory does not vanish —
its `skills/daily-ai-brief/`, its `deployment.json`/`agent.json` source-of-truth payloads, and its
README runbook remain relevant. Under the recommended hybrid, the self-hosted *infrastructure* subtrees
(`cdk/` and `microvm/`) are **retained** (production runs on them). Under full cloud, those subtrees are
retired **only after** the staged production cut-over validates.

## Decision 2a (recommended): the decoupled delivery boundary — a new standalone `deploy/delivery/` CDK stack, bearer-token authenticated, that derives the brief HTML deterministically and runs delivery as an async trigger-and-poll

*(Transport amended 2026-07-06 — **fourth-pass, surgical, transport-only**. The `POST /deliver`
contract below was originally described as **synchronous** — one HTTP call that returns once
everything (HTML derivation, Polly synthesis, SES fan-out, archival) is done. That is **not viable
on an API Gateway HTTP API**: the real delivery work takes several minutes (Polly synthesis alone
carries an existing 5-minute allowance — `audio_email.py:171` `deadline = time.time() + 300`,
polling every 5s — and the SES fan-out then sends one `send_raw_email` per confirmed subscriber on
top), while an HTTP API caps the integration response at well under a minute (constraint confirmed
against the AWS docs below). So the contract is changed from synchronous to an **async
trigger-and-poll** shape — `POST /deliver` returns `202` immediately and a new
`GET /deliver/{deliveryId}` route reports completion — **mirroring this repo's own already-shipped
`deploy/eval/` precedent** (`POST /trigger` kicks off a long-running run and returns immediately;
a separate poll mechanism finishes once it completes, status tracked in the `brief-eval-records`
DynamoDB table). **This is a transport change only.** Everything else in Decision 2a — the
deterministic no-LLM HTML derivation, the thin-wrapper-around-`audio_email.py` principle, the
delivery-side IAM grants, the byte-for-byte HTML regression check, the `contractVersion` field —
is unchanged; the delivery Lambda does exactly the same work once invoked, only the way it is
invoked and its result is retrieved changes. Decisions 1, 2b, and 2c are untouched by this pass.)*

**Recommendation: build the decoupled delivery boundary as a NEW, standalone
`deploy/delivery/` CDK app** — a sibling of `deploy/subscribers/`, `deploy/feedback/`,
and `deploy/eval/`, matching this repo's proven one-CDK-app-per-surface convention
(ADR-0012 established exactly this reasoning for keeping `deploy/feedback/` standalone).
No static review site is needed; the shape is **API Gateway (HTTP API) + a delivery
Lambda that wraps today's `audio_email.py` logic and takes on the Markdown→HTML
derivation that today's content-generation agent does ad hoc**, plus a small
delivery-tracking DynamoDB table, the delivery-side IAM, and a bearer secret. Because
the AWS work behind a delivery call runs for minutes, the boundary is an **async
trigger-and-poll** pair (`POST /deliver` + `GET /deliver/{deliveryId}`), not a single
synchronous request/response (see "Why async" below).

Concretely:

- **Why async — the confirmed HTTP API constraint.** An API Gateway **HTTP API**
  (`aws_apigatewayv2.HttpApi` — the type this repo already uses for `deploy/eval/`,
  `deploy/feedback/`, and `deploy/subscribers/`) has a **maximum integration timeout of
  30 seconds that CANNOT be raised** — a hard platform limit, not a tuning knob, and
  independent of the backing Lambda's own timeout (which can go to 15 minutes). A Lambda
  behind an HTTP API that takes minutes to respond will therefore always fail the request
  (a 5xx integration timeout) long before it finishes, no matter how the Lambda is
  configured. **Confirmed against the AWS docs (2026-07-06):** the "Quotas for configuring
  and running an HTTP API" table
  (`docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html`) lists
  **"Maximum integration timeout — 30 seconds — Can be increased: No"**. This is the
  HTTP-API-specific row: it is distinct from, and stricter than, the REST-API execution
  quota (`api-gateway-execution-service-limits-table.html`), which lists integration
  timeout as `50 ms – 29 seconds` and *raisable* above 29s at the cost of throttle quota —
  that raisability applies to **REST** (`aws_apigateway.RestApi`) only and does **not**
  transfer to HTTP APIs, whose row is explicitly non-increasable. The effective ceiling is
  ~29 s (the practical integration limit) against a documented hard maximum of 30 s;
  either way it is far below the several minutes real delivery needs. The delivery work is
  genuinely minutes-long: Polly synthesis alone carries an existing 5-minute allowance
  (`audio_email.py:171`, `deadline = time.time() + 300`, polling every 5 s until the task
  completes or times out), and the SES fan-out then issues one `send_raw_email` per
  confirmed subscriber (`send_all`, `audio_email.py:383`) after Polly finishes — so a
  synchronous `POST /deliver` would reliably time out. Switching to REST API purely to buy
  the raisable-but-still-≤-throttle-capped timeout is rejected: it breaks the repo's
  uniform HTTP-API-everywhere convention, and even a raised REST timeout is a fragile way
  to hold a multi-minute request open. The correct pattern — and the one this repo already
  ships in `deploy/eval/` — is to make the long-running call **asynchronous**.

- **`POST /deliver`** on an HTTP API — an **async trigger** that returns immediately. Its
  JSON body is the brief content and minimal metadata: the **brief markdown**, the
  **listening-script text**, and the metadata delivery needs (email subject, the run's
  local date / `PIPELINE_TIMEZONE`, and the fan-out/feedback config toggles). **The request
  body does NOT include `brief_html`** (rev. 2, FR-2a) — content generation no longer
  produces brief HTML; delivery derives it itself (the HTML-derivation bullet below). This
  is the **stable, versioned contract** (FR-2): the request body schema carries an explicit
  `contractVersion` field so a change to it is a reviewable code change, not an invisible
  `initial_prompt` edit. On receipt the delivery Lambda **(1)** checks the bearer auth
  (Decision 2b, fail-closed) and validates the body, **(2)** writes a **pending** record to
  a new small DynamoDB table (`brief-deliveries` — PK `deliveryId`, a generated UUID; no
  sort key and no GSI, since single-item get-by-`deliveryId` is the only access pattern —
  the same minimal shape `deploy/eval/`'s `brief-eval-records` uses, `stack.py:276`),
  **(3)** kicks off the actual delivery work **asynchronously**, and **(4)** returns **`202
  Accepted`** with `{"deliveryId": "...", "status": "pending"}`. It does **not** wait for
  Polly/SES/archival — that is exactly what would blow the 30 s ceiling. *(The `brief_html`
  change from this ADR's original rev.-1 pass — where it was a contract **input** — stands:
  it is now a delivery-derived value, never an input.)*

- **How the async work is kicked off: Lambda self-invoke with `InvocationType="Event"`,
  not a second worker Lambda.** The `POST /deliver` handler asynchronously invokes **the
  same delivery function** (`lambda.invoke(FunctionName=<self>, InvocationType="Event",
  Payload=<the deliveryId + validated body>)`) and returns the `202` right away; the async
  invocation then runs the full derive→synthesize→send→archive path (below) with a Lambda
  timeout set generously above the real runtime (e.g. 10 min, comfortably over Polly's 5 min
  + fan-out), writing the outcome back to the `brief-deliveries` row when done. A single
  function handling both the synchronous trigger leg and the asynchronous worker leg
  (branching on whether the event is an API Gateway request or its own self-invoke payload)
  is chosen over a second, internal-only worker Lambda because it is **simpler and keeps all
  the delivery logic — the HTML derivation and the `audio_email.py` wrapper — in one place
  with one IAM role**, which matters here since that role is the *only* holder of
  SES-to-subscriber rights post-redesign (Decision 2a IAM bullet): one role to review, not
  two. `deploy/eval/` split trigger and poll into two functions only because its "poller" is
  a general **EventBridge-scheduled sweep** over *all* pending rows (`poll/handler.py`,
  invoked every 2 min by a schedule rule, `stack.py:547`) — a different mechanism serving a
  different need (there is no long-lived caller waiting on any one eval). Delivery has one
  discrete unit of work per call and a caller that will poll for *that* call's result, so a
  per-call self-invoke is the leaner fit; no EventBridge rule and no cross-function sweep are
  needed. *(Async self-invoke has at-least-once/retry semantics; the worker leg must be
  written so a duplicate invocation of the same `deliveryId` does not double-send — it should
  no-op or short-circuit if the row is already past `pending` — the same idempotency
  discipline the launcher's webhook guard (ADR-0010) already applies. Flagged for the
  Developer; the delivery-tracking row is the natural place to enforce it.)*

- **`GET /deliver/{deliveryId}`** on the same HTTP API — the **poll** route, bearer-auth
  gated identically. It reads the `brief-deliveries` row and returns the current status:
  `{"status": "pending"}` while the async work runs, `{"status": "succeeded", ...}` with the
  same **"what was derived (the HTML), synthesized, sent, and archived"** summary the
  original synchronous response was to carry once the work completes, or `{"status":
  "failed", "error": "..."}` (with a concise, non-leaking error detail) if the worker leg
  raised. The caller (the content-generation session's `curl`, or any manual/scripted check)
  polls this until it leaves `pending` — the same trigger-then-poll rhythm the eval harness's
  own trigger/poll pair uses. This poll route is what lets delivery report back its full
  outcome (which the 30 s HTTP-API ceiling forbids doing inline on `POST /deliver`) without
  the content-generation side needing any AWS identity — it is one more bearer-gated HTTP
  GET (Decision 2b, unchanged).

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
  2. wraps that body-plus-a-fixed-shell with the **existing, unchanged** delivery-side
     chrome already in `audio_email.py`: `_html_with_header(...)` (`audio_email.py:309` — the
     top banner: per-recipient feedback prompt when available + "subscribe here" forward
     prompt + AI-curation disclaimer, in one styled box) and
     `_html_with_unsubscribe_footer(...)` (`audio_email.py:344` — the `<hr>` + "Unsubscribe"
     footer on subscriber copies). These two functions are **already delivery-side today**,
     are **genuinely stable, code-defined chrome** wrapped around whatever the body-conversion
     produces, and stay **exactly as-is** — only the *body-and-document-shell* conversion moves
     from the agent into delivery.

  *(Corrected 2026-07-06 — evidence-driven; supersedes the "reverse-engineer THE standardized
  design and confirm byte-for-byte parity against a single real production `brief.html`"
  framing this paragraph originally carried. That framing rested on a false premise: that a
  single, stable HTML design exists today to be preserved. Diffing three real, genuine
  production `brief.html` files — `s3://cowork-polly-tts-740353583786/briefs/<date>/brief.html`
  for 2026-07-03, 2026-07-04, and 2026-07-06 — disproves it: they are **three structurally
  different HTML documents**. 2026-07-03 uses a single `<div style="max-width:680px…">` wrapper
  (no table), a `.footer` CSS class, link colour `#0645ad`, background `#f2f2f4`. 2026-07-04
  uses a `.email-wrapper`/`.email-card` `<div>` structure with CSS as **named classes in a
  `<head>`-level `<style>` block**, a `.tldr` callout class absent on the other days, link
  colour `#2b6cb0`, and a coloured `h1` bottom border absent elsewhere. 2026-07-06 uses a
  `<table role="presentation">` email-client-robust layout with an inline `<style>` block
  inside the body's inner `<td>` (not in `<head>`), an uppercase "eyebrow" label div absent
  elsewhere, and link colour `#2563eb`; its footer disclaimer ("You're receiving this because
  you subscribed to the Daily AI Brief.") matches **neither** `_html_with_header()` nor
  `_html_with_unsubscribe_footer()`'s actual text (`audio_email.py:347` reads "You **are**
  receiving this because you subscribed to **the daily AI brief**. Unsubscribe at any time." —
  different wording, and it adds an unsubscribe link) — confirming the archived `brief.html`
  is the **raw pre-header/footer-wrap file the agent itself writes** (`audio_email.py:159`),
  so the variance is the **agent's**, re-improvised every run, not something added later by
  delivery-side wrapping. In short: the agent re-improvises the entire HTML document — wrapper
  strategy, CSS class system, colour palette, presence/absence of elements like a `.tldr` box
  or eyebrow label — fresh every run; **there is no fixed template underneath to reverse-
  engineer.**)*

  **This is a determinism improvement, not a fidelity-to-a-moving-target constraint (rev. 2 /
  PRD §6; corrected 2026-07-06).** Because the current output is not one design but a different
  agent-improvised document every run (evidence above), `derive_html()` should **establish one
  fixed, deterministic, well-designed HTML email template — chosen once by the Developer and
  applied consistently on every future run.** This is a genuine upgrade over today's
  non-determinism, not a constraint fighting against it: it *ends* the per-run variance rather
  than trying to reproduce it. **Recommendation (with reasoning): the Developer should favour a
  table-based layout** — `<table role="presentation">`, closest to what 2026-07-06 happened to
  use — over a plain-`<div>` wrapper (the 2026-07-03 / 2026-07-04 approach). This is a real
  engineering reason, not an arbitrary pick among three equally-valid shapes: table-based email
  layouts are the long-established best practice for cross-client CSS rendering compatibility —
  Outlook's rendering engine and various webmail clients handle modern CSS (flex, border-radius,
  box-shadow, `<head>`-scoped `<style>`) inconsistently, whereas presentation-table layouts
  degrade far more gracefully across that client matrix. **The acceptance bar is
  content-conversion fidelity, not byte-for-byte parity with any one historical day.** The
  Developer/Reviewer must verify that `markdown.markdown(...)`'s handling of the brief's actual
  constructs — headings, paragraphs, lists, emphasis, links, `<hr>` — is correct against
  **multiple real markdown fixtures** (e.g. the 2026-07-03/04/06 `brief.md` sources), choosing
  and pinning the `markdown` extensions/options the source Markdown actually requires. It is
  **not** "diff the output against a single day's `brief.html` and demand a match" — that
  target does not exist and never did. `_html_with_header()`/`_html_with_unsubscribe_footer()`
  (`audio_email.py:309`/`:344`) are unaffected — genuinely stable, code-defined, delivery-owned
  chrome, wrapped around whatever the new fixed template produces, exactly as the sub-bullet
  above states. *(The content-generation side stops producing `brief.html` entirely, so
  `deployment.json` step 2 and `audio_email.py`'s `BRIEF_HTML_PATH` input are removed as part
  of decoupling delivery — see the migration sketch, Phase 1.)*

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
  (repo convention: no secret in git/CDK). The wrapper `curl`s `POST /deliver` with
  `Authorization: Bearer <token>`.
  - **Correction (2026-07-06): how the token reaches the sandbox.** This bullet originally
    said the `cloud` sandbox "receives it as an environment variable via the environment's
    declarative `environment` config block (confirmed present on the cloud env config)."
    That is **retracted** — a live validation (2026-07-06) proved `config.environment` is
    **not settable via the current beta Managed Agents API** (it is rejected at
    environment-create with `400 "config.environment: Extra inputs are not permitted"`, and
    is read-only/reserved; the same is true of `config.init_script` and `config.packages`).
    The **only** working channel to pass a per-run value to a `cloud` candidate today is the
    deployment's **`initial_events` (the task prompt)** — see the full evidence and the
    recommended remedy in **Decision 2d → "Correction (2026-07-06): how the read token
    actually reaches a `cloud` candidate."** That correction's short-lived-signed-token
    recommendation is written for the **read** token (`GET /recent-briefs`), the one the live
    validation exercised; the **delivery/send** token here reaches the sandbox by the same
    `initial_events` channel (never `config.environment`), and whether it too should become
    short-lived is a follow-up the Developer/human can take up when Phase 1 wires the send
    path from a `cloud` candidate. Decision 2b's core recommendation — a bearer secret,
    checked fail-closed with a constant-time compare — is **unchanged** by this correction;
    only its claimed token-delivery mechanism was wrong.
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

## Decision 2d (recommended): the recent-priors read endpoint — `GET /recent-briefs` on the `deploy/delivery/` boundary, gated by its OWN separate read-only bearer secret (distinct from the delivery/send secret)

> **Amended 2026-07-06 (seventh pass — token-delivery correction).** A live validation of `GET /recent-briefs`
> found that the token-injection mechanism this decision (and Decision 2b) originally specified —
> setting the read token as an env var in the shared `cloud` environment's `config.environment` block —
> is **NOT available via the current beta Managed Agents API** (probe evidence: `400 "config.environment:
> Extra inputs are not permitted"` at environment-create; the field is read-only/reserved, as are
> `config.init_script` and `config.packages`). The **only** working per-run injection channel today is the
> deployment's `initial_events` (the task prompt), which lands the token in the session transcript. This
> pass records that correction and recommends a **permanent mechanism — a short-lived, HMAC-signed read
> token minted per run and verified with an `exp` claim** (reusing the ADR-0011 signed-token scheme already
> vendored into the delivery Lambda), so a transcript-leaked token dies within minutes and needs no per-run
> rotation. See the new subsection **"Correction (2026-07-06): how the read token actually reaches a `cloud`
> candidate"** below. **Flagged for the human's sign-off** (it affects the deferred eval epic's many-run
> cadence). The endpoint contract, its separate read-only secret, its no-new-IAM property, and the
> FR-1/FR-7 auth-separation guarantee are all **unchanged** — only the token-delivery mechanism is
> corrected. The interim approach used during validation (a static token injected at trigger time, the read
> secret rotated + the Lambda cold-started immediately after) **remains fine to keep using until the human
> ratifies.**

*(Added 2026-07-06, sixth pass, after the human ratified Decision 1 (the hybrid). This decision exists
**only** because the hybrid was chosen: it closes an eval-vs-production fidelity gap the hybrid creates.
Under full cloud it would still be needed — a `cloud` production run would face the same "no S3 access"
gap — so nothing here is hybrid-specific in its mechanism; it is simply the hybrid that made the gap
concrete and worth closing now. This is a modest, well-bounded endpoint, not a new subsystem.)*

### Why this endpoint exists — the hybrid's "read recent priors" fidelity gap

Production (self-hosted) begins each run by reading the **last few days' briefs** from S3 so the
research step can avoid repeating recent stories and correctly label genuine multi-day follow-ups. That
is `deployment.json`'s `initial_prompt` **step 0**:
`python3.13 /opt/pipeline/audio_email.py read-recent-briefs`, which calls
`brief_history.read_recent_prior_briefs()` (reads `s3://cowork-polly-tts-740353583786/briefs/<date>/brief.md`
for the most-recent-N dates) and writes each prior into `WORKING_FOLDER` under the skill's own dated
filename convention, `AI Brief - <date>.md` (`audio_email.py:136-155`), so the skill finds them via its
normal `WORKING_FOLDER` search.

A **`cloud` candidate has no AWS access at all** (that is the whole point — FR-1/AC-1). So it currently
**skips** this step: `deploy/candidates/production-baseline/task-prompt.md` says explicitly *"this
candidate does NOT have access to any prior briefs (there is no S3/AWS access available in this
environment) -- skip any 'read recent prior briefs' step entirely."* The consequence under the ratified
hybrid is a real **eval-vs-production fidelity gap**: a `cloud`-eval candidate, lacking the recent
priors production reads, can select and write up a story that production would have suppressed as a
recent repeat. When comparing candidates against the production baseline, that difference is noise that
does not come from the candidate's own configuration — exactly the kind of unfair-comparison artifact
the redesign should avoid.

The fix is to expose recent-priors **reading** through the already-decoupled `deploy/delivery/`
boundary. `deploy/delivery/` is the **one** place that holds AWS credentials post-redesign, and its
delivery Lambda **already carries S3 read IAM on the briefs bucket** (`S3ListBriefsPrefix` +
`S3AudioReadWrite`, `stack.py:310-326`) and **already contains a hand-duplicated copy of
`read_recent_prior_briefs()`** (`deploy/delivery/functions/deliver/brief_history.py:107`). So a `cloud`
candidate can `curl` the delivery boundary for the same recent priors production reads directly from S3,
and reach parity — without ever holding an AWS credential (FR-1 preserved).

### The central constraint — read capability must NOT confer send capability (the auth-separation decision)

Giving a `cloud` candidate the ability to call a read endpoint must **not** give it the ability to
trigger a real delivery/send. FR-1/FR-7 (AC-1/AC-7) guarantee that a candidate run **never touches the
delivery path and never emails a subscriber**, and the security review of Phase 1 specifically praised
the delivery bearer secret as the tightest control in the redesign (Decision 2b: *"the delivery endpoint
is the only new surface that can email real subscribers … its auth must be the tightest thing in the
redesign"*). So whatever credential a candidate holds to reach `GET /recent-briefs` must be **incapable**
of reaching `POST /deliver` (or `GET /deliver/{deliveryId}`). The read capability and the send capability
must be genuinely, structurally separable — not merely "the candidate is trusted not to call `/deliver`."

**Recommendation: `GET /recent-briefs` is gated by its OWN separate, read-only bearer secret** — a
**new** Secrets Manager secret distinct from the `POST /deliver` delivery bearer secret (Decision 2b).
The candidate is given **only** the read secret; it never holds the delivery/send secret. Because the
two endpoints check two different secrets, a candidate holding the read token **cannot** authenticate to
`POST /deliver` at all — the send path is closed to it by construction, not by trust. This preserves
FR-1/FR-7 exactly: the only thing the read token unlocks is "read the last few briefs," a capability that
cannot email anyone or trigger any delivery.

Concretely:
- A **second Secrets Manager secret** in the `deploy/delivery/` stack — e.g.
  `daily-ai-brief/recent-briefs-read-bearer-secret` — created **empty** (`RemovalPolicy.RETAIN`, no
  initial `SecretString`), populated out-of-band, exactly as the delivery bearer secret and
  `deploy/eval/`'s reviewer secret already are (repo convention: no secret in git/CDK). The delivery
  Lambda's role gets a second ARN-scoped `secretsmanager:GetSecretValue` grant for **just this** secret
  (a one-line addition alongside the existing `ReadDeliveryBearerSecret` statement — not a broadening of
  any AWS delivery grant).
- The `GET /recent-briefs` handler checks **this** secret with the **same fail-closed constant-time
  discipline** `delivery_auth.py` already enforces (`hmac.compare_digest`; no configured secret, no
  supplied value, or a mismatch → **401**, never a fall-open; `Authorization: Bearer <token>` header
  only, no query-string fallback). The natural implementation is a **sibling module** mirroring
  `delivery_auth.py` (e.g. `recent_briefs_auth.py`) bound to its own env var
  (`RECENT_BRIEFS_READ_BEARER_SECRET_ARN`), or the same module parameterized over which secret it reads —
  the Developer's call; the invariant is that **the two secrets are distinct and the read handler checks
  only the read one.** `POST /deliver` and `GET /deliver/{deliveryId}` continue to check **only** the
  delivery secret (Decision 2b, unchanged) — the send path never accepts the read token, and the read
  path never accepts the send token.

**Why the separate secret, not the alternatives (evaluated):**
- **(a) A separate, read-only bearer secret — CHOSEN.** It makes the capabilities genuinely separable
  at the token level: the candidate holds only the read token, so `POST /deliver` is unreachable to it
  full-stop. It reuses everything already proven — the same stack, the same HTTP API, the same
  fail-closed `delivery_auth.py` pattern, the same "empty secret populated out-of-band" convention — for
  the cost of one new empty secret and one one-line IAM grant. Blast radius is independent per the same
  reasoning Decision 2b/`deploy/eval/` already give for keeping secrets separate: if a `cloud` session's
  read token ever leaked (e.g. surfaced in a session log), rotating it does not disturb the delivery/send
  secret, and — crucially — a leaked read token **cannot send** in the first place.
- **(b) The same shared delivery bearer secret — REJECTED.** This **breaks the guarantee.** If
  `GET /recent-briefs` and `POST /deliver` shared one secret, a candidate holding it to read priors could
  immediately `POST /deliver` and trigger a real send/fan-out to subscribers — precisely the capability
  FR-1/FR-7 strip from content generation and the exact thing the Phase-1 security review flagged as the
  redesign's tightest control. No argument rescues (b): the whole point is that a candidate must **not**
  be able to reach the send path, and sharing the secret hands it the send path. Rejected unconditionally.
- **(c) A separate tiny read-only surface/stack — REJECTED as disproportionate.** A standalone
  `deploy/recent-briefs/` CDK app (its own HTTP API, its own Lambda, a **third** hand-duplicated
  `brief_history.py` copy) would also separate the capabilities — but it buys essentially no additional
  isolation over (a) while adding a whole deploy lifecycle for a single read-only GET over **the same
  bucket the delivery Lambda already reads.** The capability separation that matters here is at the
  **token** level, and (a) achieves it inside the existing stack; a stack split would only add marginal
  network/identity isolation that is not needed for a read that co-locates with a Lambda already holding
  strictly broader (write + SES + DynamoDB) rights. The repo's one-CDK-app-per-*surface* convention
  (ADR-0012) argues for a separate stack when a surface has a **distinct deploy lifecycle and a distinct
  IAM/auth blast radius** — but recent-priors reading is neither a distinct lifecycle (it ships and
  changes with delivery) nor a broader blast radius (it is a strict subset of what the delivery Lambda
  can already do to that bucket). So it belongs **in** `deploy/delivery/`, gated by its own token —
  reuse where it is safe (the IAM, the HTTP API, the bucket read), separate where it matters (the token).
- **(d) Any better option?** None found. A capability-scoped short-lived token or a signed-request
  scheme would be heavier key machinery than a single trusted service-to-service caller warrants (the
  same reasoning Decision 2b gives for rejecting mTLS/SigV4/Cognito), and would not improve on the
  clean, structural separation two distinct bearer secrets already give.

### Endpoint contract

- **Route:** `GET /recent-briefs?count=<n>` on the **same** `deploy/delivery/` HTTP API, integrated to
  the **same** delivery Lambda (branching on the route, as the Lambda already branches between its API
  legs and its self-invoke worker leg). `count` is optional and defaults to
  `brief_history.DEFAULT_RECENT_BRIEFS_COUNT` (3), matching production's default; it should be clamped to
  a small sane maximum (e.g. ≤ 10) so a caller cannot request an unbounded listing. **Bearer-auth gated**
  by the read-only secret (above), fail-closed.
- **Synchronous — no async trigger/poll (unlike `POST /deliver`).** Reading the most-recent-N brief
  markdown objects is a cheap `list_objects_v2` (delimiter-scoped, one level) plus N `get_object` calls
  on small text files — comfortably within API Gateway's **30 s HTTP-API integration ceiling** (the same
  hard limit Decision 2a documents and which forced `POST /deliver` to be async). There is no
  minutes-long work here (no Polly, no SES fan-out), so a plain synchronous request/response is correct
  and simpler: no `brief-deliveries`-style tracking row, no self-invoke, no poll route. This is the
  **read** counterpart to `POST /deliver`'s **write**, and its cheapness is exactly why it can be
  synchronous where delivery cannot.
- **Response — `200`:**
  ```json
  {
    "briefs": [
      { "date": "2026-07-04", "markdown": "# Daily AI Brief …" },
      { "date": "2026-07-03", "markdown": "# Daily AI Brief …" }
    ]
  }
  ```
  The list is **most-recent-first** and contains **0..count** entries — exactly what
  `read_recent_prior_briefs()` already returns (`brief_history.py:107`, a list of
  `PriorBrief(date, markdown)`): fewer than `count` when fewer priors exist, and an **empty list**
  (`{"briefs": []}`, still `200`) on a first-ever run or when the store is young — never an error. This
  mirrors production's own graceful-degradation contract (the `read-recent-briefs` CLI prints
  `PRIOR_BRIEFS_NOT_FOUND` and exits 0), so a candidate with no priors behaves exactly as production
  does with no priors. A transient S3 listing/read failure degrades to the same empty/partial result the
  underlying function already returns (it logs and skips a failed date rather than raising), so reading
  priors can never abort the endpoint — consistent with ADR-0005's "the read must tolerate an empty
  listing" and CLAUDE.md's "never lose the brief over a glitch."
- **`contractVersion` discipline (consistent with Decision 2a).** The `200` response body carries an
  explicit `contractVersion` field (e.g. `{"contractVersion": 1, "briefs": [...]}`), so a future change
  to this read contract is a reviewable code change rather than an invisible drift — the same discipline
  Decision 2a puts on the `POST /deliver` request body. (The request side is a trivial query string with
  no versioned schema to speak of; the version lives on the response, which is the shape a consumer
  actually parses.)
- **401 on missing/invalid/absent-secret** — identical to `delivery_auth.py`'s `unauthorized_response()`
  (`{"error": "unauthorized"}`), fail-closed.

### IAM — NO new IAM is needed (verified against `stack.py`)

The delivery Lambda's execution role **already holds exactly the S3 grants this endpoint needs**, because
it already archives and (via the migrated `brief_history.py`) can read the `briefs/` prefix:
- **`S3ListBriefsPrefix`** (`stack.py:318-326`): `s3:ListBucket` on
  `arn:aws:s3:::cowork-polly-tts-740353583786` with `StringLike s3:prefix ["briefs/*"]` — covers
  `read_recent_prior_briefs()`'s `list_objects_v2(Prefix="briefs/", Delimiter="/")` folder listing
  (`brief_history.py:99`).
- **`S3AudioReadWrite`** (`stack.py:310-317`): `s3:GetObject` (and `PutObject`) on
  `arn:aws:s3:::cowork-polly-tts-740353583786/*` — covers reading each `briefs/<date>/brief.md`
  (`brief_history.py:138`).

Both are already present and already scoped to exactly this bucket/prefix (they are the same grants
production's own read-recent-briefs step uses, moved to the delivery Lambda in Decision 2a). So
**`GET /recent-briefs` requires no new AWS delivery IAM** — the only new IAM anywhere is the
**read-only bearer secret's** `secretsmanager:GetSecretValue` grant (one ARN-scoped statement for the
new secret), which is auth machinery, not an AWS-service delivery capability, and does not widen what the
Lambda can do to Polly/SES/DynamoDB/S3 in any way. This is the tight-IAM outcome the design intends: the
read endpoint adds a *token check*, not a *capability*.

### How the `cloud` candidate consumes it — restoring production parity

The candidate's `task-prompt.md` currently instructs it to **skip** the read-recent-priors step
entirely. Under this decision, a candidate that wants production parity instead does what production's
step 0 does, but over HTTP rather than S3:
1. `curl -s -H "Authorization: Bearer $RECENT_BRIEFS_READ_TOKEN" "$DELIVERY_API_BASE_URL/recent-briefs?count=3"`
   to fetch the recent priors as JSON.
2. For each returned `{date, markdown}`, write the markdown into `WORKING_FOLDER` under the **skill's own
   expected filename convention** — `AI Brief - <date>.md` — **exactly what production's
   `read-recent-briefs` CLI writes** (`audio_email.py:148`). Writing under that same convention is what
   makes the skill find the priors via its normal `WORKING_FOLDER` search (its "Configuration:
   WORKING_FOLDER" behavior), with **no skill-side special-casing** — restoring parity with production's
   behavior precisely.
3. Proceed to research/write exactly as today, now able to avoid recent repeats and label genuine
   follow-ups, just as production does.

The read bearer token (and the delivery API base URL) reach the `cloud` sandbox **at trigger time via the
deployment's `initial_events` (the task prompt), NOT via any environment-config channel** — see the
correction and the recommended permanent mechanism below ("**Correction (2026-07-06): how the read token
actually reaches a `cloud` candidate**"). The original phrasing here (and in Decision 2b) — "injected via
the `cloud` environment config's declarative `environment` (env-vars) block" — is **retracted**: that
channel is **not settable via the current beta Managed Agents API** (live-probe evidence below). The
invariant for the Developer is unchanged: the candidate receives **only** the read-only token (never the
delivery/send token), so it can fetch priors but can never trigger a send. Whether the
`production-baseline/task-prompt.md` "skip the read step" note is replaced by the curl-then-write steps, or
the read step is expressed in the candidate's declaration another way, is left to the Developer.

### Correction (2026-07-06): how the read token actually reaches a `cloud` candidate — env-vars-on-environment do NOT work; recommend a short-lived signed read token injected per run

> **Status: ACCEPTED — ratified by the human 2026-07-06.** The human chose the short-lived HMAC-signed
> read-token mechanism recommended below AND chose to build it immediately (rather than defer it to the
> eval epic or keep the static-token-plus-rotation interim). The static-token interim is therefore
> superseded once the signed-token mechanism ships; it remains a valid fallback only if the build is ever
> rolled back.

**What was originally specified, and why it does not work.** Decision 2d (and Decision 2b) originally said
the read/delivery bearer token would reach the `cloud` sandbox as an **environment variable set in the
shared `cloud` environment's declarative `config.environment` (env-vars) block**, populated out-of-band.
A live validation of the recent-priors read endpoint (2026-07-06) proved that channel is **not available
via the current beta Managed Agents API.** Real probes against `api.anthropic.com`
(`managed-agents-2026-04-01`; every probe environment archived immediately, the shared env
`env_01W3Envi4NfK7ypQMfoZccRY` and production never touched):

- **Setting `config.environment` at environment-create is rejected.**
  `POST /v1/environments` with `config: {type: "cloud", environment: {FOO: "bar"}}` →
  **`400 invalid_request_error: "config.environment: Extra inputs are not permitted"`**. Placing
  `environment` at the top level (sibling of `config`) is rejected identically.
- **A `GET` on an existing environment shows `config.environment: {}` (empty) but you cannot SET it.** The
  field is **modeled** in the returned config shape but comes back as an empty default, and there is no
  working update path — a create rejects the field, and `POST`/`PATCH` to `/v1/environments/{id}` do not
  provide one (they 307-redirect). So `config.environment` is effectively **read-only / reserved** in this
  beta.
- **This is NOT limited to the env-vars block — `config.init_script` and `config.packages` are ALSO
  rejected at create** with the identical `"Extra inputs are not permitted"` error (both inside `config`
  and at the top level), and both come back as empty defaults (`init_script: ""`, all `packages` arrays
  empty) on a `GET`. **Only `config.type` and `config.networking` are actually settable at create.** (This
  corrects the task brief's assumption that `packages`/`init_script` were settable and only `environment`
  was not — in this beta, the whole structured-config surface beyond `type`/`networking` is reserved.)

**Consequence: the deployment's `initial_events` (the task prompt) is the ONLY working channel today** to
pass any per-run value (a token, the delivery base URL) to a `cloud` candidate. `trigger.py` already builds
the run this way (`create_temporary_deployment(...)` puts the task prompt into
`initial_events[0].content[].text`, `candidate_sync/trigger.py:132`). Because `initial_events` is echoed
into the session transcript (readable by anyone holding the org API key), **any token injected this way
appears in that run's transcript.** For the low-sensitivity read token — it reads only already-public brief
content and is auth-separated from the send path by its own distinct secret (the core Decision 2d
guarantee, unaffected by this correction) — that exposure is bounded, but as a **standing** mechanism it
means *every* candidate run would leak the token and (with a static token) need a rotation afterward. The
future eval epic triggers **many** candidates, so it needs a clean, repeatable approach that does not
require a rotation per run.

**Recommendation (permanent mechanism): mint a SHORT-LIVED, HMAC-signed read token per run in the
orchestrator, inject it via `initial_events`, and have `GET /recent-briefs` verify the signature + expiry.**
Instead of injecting the long-lived static bearer secret, the trigger side (`trigger.py` now, the eval
harness later) mints a **short-TTL signed token** for each run and injects *that* into the task prompt; the
delivery Lambda's `GET /recent-briefs` handler verifies it. Even when it lands in the transcript, it is
**dead within minutes**, so there is nothing to rotate per run and a leaked transcript token cannot be
replayed. Concretely, this **reuses the repo's existing signed-token prior art almost verbatim**:

- The scheme is the **feedback signed-token scheme (ADR-0011/0012)** — `<payload_b64url>.<sig_b64url>`,
  `HMAC-SHA256(secret, payload_b64url)`, stdlib-only, constant-time verify — **already vendored into this
  very Lambda** at `deploy/delivery/functions/deliver/feedback_token.py`. The one change from the feedback
  token is that this token's payload carries an **`exp` (expiry) claim** — the feedback token deliberately
  omits expiry (attribution-integrity, not a capability), but this read token **is** a short-lived
  capability, so a small **read-capability token variant** (a sibling helper, e.g.
  `recent_briefs_token.py`, mirroring `feedback_token.py`'s shape) adds `exp` and the verifier enforces it
  (reject if `now > exp`). Suggested payload: `{"v": 1, "scope": "recent-briefs", "exp": <unix-ts>}` — no
  identity needed (the capability is uniform: "read the last N public briefs").
- The **signing secret** is the **same** `daily-ai-brief/recent-briefs-read-bearer-secret` that already
  gates this endpoint — no new secret, no new IAM. The **orchestrator** reads that secret from Secrets
  Manager (the trigger side is the operator's own machine/CI, which legitimately can), signs a token with a
  short TTL (**as built: 20 minutes** — `RECENT_BRIEFS_TOKEN_TTL_SECONDS` in `candidate_sync/trigger.py`;
  the range originally sketched here was 5–15 min, but the build added headroom for
  trigger→deployment-create→`/run`→sandbox-boot→first-tool-call latency plus clock skew, while staying "dead
  within the run's own lifetime, not hours" — the priors fetch happens in the run's first minute, not at the
  end), and injects it. The
  `GET /recent-briefs` handler switches from `hmac.compare_digest(supplied, static_secret)` to
  `verify_signed_read_token(supplied, secret)` (signature + `exp`). This keeps the endpoint **fail-closed**
  exactly as today: no token, a bad signature, or an expired token → **401**, never a fall-open.
- **Nothing about the auth-separation guarantee changes.** The read token still cannot reach `POST /deliver`
  (that endpoint checks the *delivery* secret, and this token is signed with the *read* secret and carries a
  `recent-briefs` scope), so FR-1/FR-7 hold by construction, exactly as Decision 2d already establishes. The
  signed-token change only bounds the *read* token's lifetime; it does not widen what the read token can do.

**Why this over the alternatives (evaluated):**

- **(b) Short-lived signed read token — CHOSEN.** Given that `initial_events` is the *only* injection
  channel today, the right mitigation is to make the injected value **non-durable** rather than to keep
  hunting for a config channel that does not exist. It removes the per-run rotation entirely (an expired
  token is self-neutralizing), which is exactly the property the **many-candidate eval epic** needs, and it
  costs almost nothing because the HMAC sign/verify machinery is **already in this Lambda** (`feedback_token.py`)
  — a small `exp`-carrying sibling and one verifier swap, mirroring a scheme two prior ADRs already reviewed.
  This is the same "signed, self-attesting, stdlib-only, no new dependency" posture ADR-0011 chose, applied
  to a capability that (unlike the feedback token) genuinely *should* expire.
- **(a) Static read token via `initial_events` + a rotation policy — acceptable, but not recommended as the
  standing mechanism; RETAINED as the interim.** This is what the live validation used and it works. Its
  only cost is that each run leaks a *long-lived* token into its transcript, so a clean posture requires
  rotating the read secret on a cadence (and after any known exposure). For a **one-off or low-frequency**
  use, that is genuinely tolerable given the token's low sensitivity (public-content read, auth-separated
  from send) — so the interim approach is fine to keep using now (below). But at the eval epic's **many-run**
  cadence, "rotate after every run" is operational friction the signed-token approach removes for near-zero
  extra machinery, so (a) is not the right *permanent* choice. (If the human prefers maximum simplicity over
  minimizing transcript exposure, (a) with a documented rotation cadence — e.g. rotate weekly and after any
  exposure — remains a defensible permanent choice; the read token's low sensitivity makes this a legitimate
  option, not a security hole. The recommendation is (b) because it is *both* cleaner operationally *and*
  lower-exposure, for trivial added cost.)
- **(c) `init_script` injection — REJECTED (does not work, and would not help even if it did).** The live
  probes show `config.init_script` is rejected at create identically to `config.environment` — so it is
  **not** an available channel at all. Even if it were, it would be **strictly worse** than a per-run
  injection: `init_script` is set at *environment-create* time (not per run) on the single shared
  environment, so it would bake a **static** token into a long-lived, shared resource, and a `GET` on the
  environment echoes `init_script` back (same readability exposure as the env-vars block). It solves nothing
  the env-var channel didn't, and it is not settable regardless.
- **(d) Wait for platform env-var support — NOT relied upon.** The `config.environment` field is *modeled*
  (it appears, empty, on a `GET`), which suggests write support may arrive in a later beta — but it is not
  available now, and this repo's discipline is to **recommend something that works today** rather than block
  on a platform change. If `config.environment` becomes settable later, a static token in the shared
  environment's env-vars block would remove the token from the transcript entirely and could supersede this
  mechanism — but that is a **future** revisit, not a dependency. (Even then, a short-lived signed token is
  arguably still preferable to a static env-var secret on a shared resource, so this is not clearly a future
  win; note it and move on.)
- **A separate short-lived surface / pre-signed S3 URL — REJECTED as disproportionate.** A pre-signed S3 GET
  URL per prior object (minted by the orchestrator, which would then need S3 access it otherwise does not
  have) or a bespoke short-lived-credential service is heavier machinery than a single trusted
  service-to-service read warrants — the same reasoning Decision 2b gives for rejecting mTLS/SigV4/Cognito.
  The signed-token approach reuses what is already here and needs no new AWS surface.

**Is the interim runtime-injection approach OK to keep using until the human ratifies? — Yes.** The live
validation injected the **static** read token via `initial_events` and mitigated the transcript exposure by
**rotating the read secret and cold-starting the Lambda immediately after**, so the exposed value was dead
before this record was written. For a low-sensitivity, read-only token (already-public brief content;
structurally auth-separated from the send path by its own distinct secret) on the owner's own org, that
one-off exposure-then-rotation is tolerable. So **the interim static-token-plus-rotation approach remains
fine for occasional manual/scripted runs until the human ratifies the permanent mechanism.** What it is
**not** suited to is the eval epic's many-run cadence — which is why the *permanent* recommendation is the
short-lived signed token above. (Token shell-safety is already handled: both bearer secrets generate
alphanumeric-only values via `exclude_punctuation=True`, `deploy/delivery/brief_delivery/stack.py:282,311`,
so whichever mechanism is ratified, the token is safe to inject into a shell `curl`.)

**Scope of this correction.** This changes **how the read token reaches a `cloud` candidate** (the delivery
mechanism), not **what the read endpoint does** (Decision 2d's endpoint contract, its own separate read-only
secret, its no-new-IAM property, and the FR-1/FR-7 auth-separation guarantee are all **unchanged**). It also
corrects the parallel "env-vars config block" claim in **Decision 2b** for the *delivery/send* token by the
same reasoning: that token, too, must reach the sandbox via `initial_events`, not `config.environment` (see
the note added to Decision 2b). Whether the delivery/send token should *also* become short-lived is a
Decision-2b question the Developer/human can take up when Phase 1 wires the send path from a `cloud`
candidate; this correction records the mechanism gap for both tokens and recommends the signed-token
remedy for the read token specifically (the one the live validation exercised).

*(Note on the candidate baseline: re-expressing today's production configuration as `production-baseline/`
and validating an **unchanged** brief (Phase 5, FR-14/AC-14) becomes cleaner with this endpoint in place —
the baseline candidate can read the same recent priors production reads, removing "the candidate lacked
priors" as a confound in the unchanged-brief comparison. This endpoint therefore also strengthens the
Phase-5 drift-guard the hybrid relies on.)*

### Production is untouched — additive only (FR-14/AC-14)

This endpoint is **purely additive**. Production (self-hosted) keeps its **existing S3-backed
`read-recent-briefs` step exactly as-is** — `deployment.json`'s step 0 →
`audio_email.py read-recent-briefs` → `brief_history.read_recent_prior_briefs()` reading S3 directly via
the microVM's IAM role, unchanged. Production does **not** call `GET /recent-briefs` and does not depend
on it in any way; the endpoint exists **only** so `cloud` candidates can reach parity with what production
already does. There is **no change** to production's content, schedule, send cadence, IAM, or behavior,
and **no change** to `deploy/subscribers/` or `deploy/feedback/`. The `deploy/delivery/` stack gains one
read route, one Lambda branch, one empty secret, and one secret-read IAM grant — nothing that touches the
live production path.

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

- **The image-rebuild half of the amendment applies per-runtime — and under the reassessed (hybrid)
  recommendation it STILL applies to production.** This reconciliation is **orthogonal** to the
  Desktop-fallback question above and turns on the topology of each runtime, not on the fallback. On
  `cloud` (and with the standard self-hosted worker), skills download **dynamically per session** from
  the Skills API — the whole reason the amendment existed (this repo's bespoke worker that never
  fetched skills at runtime) disappears with the microVM image itself; a Skills-API version push **is
  sufficient** to reach the session, with no image to rebuild. How this lands depends on the topology
  the human ratifies (Decision 1):
  - **Under the recommended hybrid** (candidates/eval on `cloud`, **production retained on
    `self_hosted` with the current custom worker**): candidate/eval skills reach the session via a
    Skills-API push **with no image rebuild** (the amendment's failure mode is gone on the candidate
    path — this is exactly FR-5), **but the production skill still lives in the image-baked microVM**,
    so **ADR-0008's 2026-07-04 image-rebuild amendment remains in force for the *production* skill**
    and must **not** be marked superseded. The Developer must keep the README §3a image-rebuild
    guidance for production and scope the "no rebuild needed" relief to the candidate/`cloud` path only.
  - **Under the full-cloud alternative** (production also on `cloud`): there is no microVM image
    anywhere, so **this ADR supersedes ADR-0008's 2026-07-04 image-rebuild amendment entirely** — the
    Developer marks that amendment superseded-by-ADR-0014 and updates the README §3a correction so no
    stale note tells a future maintainer to rebuild an image that no longer exists.

  (The two-way-lockstep reconciliation above is **unconditional** either way; only this image-rebuild
  half is topology-contingent — and, under the recommended hybrid, it is **retained for production**.)

If a candidate ships its **own** `skill/` content (per-candidate skills, above), that candidate's
skill is a distinct Skills-API resource with its own version — it is **not** bound into the
production skill's lockstep at all, which is cleaner: candidate skill experiments cannot accidentally
disturb the live brief's skill. The new **source-usage record (FR-8a)**, being skill-content-driven
(Decision 2a), rides the same **two-way** production-skill lockstep as the rest of the skill content.

Net: **the redesign simplifies the ADR-0008 burden** — the dead Desktop fallback drops out of the
lockstep **unconditionally**, and the image-rebuild step **disappears for the candidate/`cloud` path**
(FR-5). Under the **recommended hybrid**, the image-rebuild step **remains in force for the retained
`self_hosted` production skill** (it is not superseded); under the **full-cloud alternative**, it
disappears everywhere. The Developer must make both reconciliations explicit in ADR-0008 and the
README — and, critically, must scope the image-rebuild relief to the candidate/`cloud` path if the
hybrid is ratified, rather than blanket-marking the amendment superseded.

## Migration / rollout sketch (consistent with PRD rev. 2 §8 phasing)

*(Rev. 2: the former "re-integrate the eval harness" phase is **removed** — that is a later, separate
epic; extracting Markdown→HTML into delivery is folded into Phase 1; the per-brief source-usage
record and the git-native versioning are added; the sketch ends with the redesigned system validated
**on its own terms**, not with `deploy/eval/` wired up.)*

1. **Decouple delivery + move Markdown→HTML into it (FR-1/FR-2/FR-2a/FR-3).** Stand up
   `deploy/delivery/` (HTTP API with the **async `POST /deliver` + `GET /deliver/{deliveryId}`
   trigger-and-poll pair**, Decision 2a; delivery Lambda wrapping `pipeline/audio_email.py`,
   self-invoked for the minutes-long worker leg; the `brief-deliveries` tracking table (PK
   `deliveryId`); delivery-side IAM = today's grants, moved not duplicated; empty bearer secret
   populated out-of-band). **As part
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
   delivery-derived HTML confirmed equivalent (FR-2a). This step runs **under either topology**: under
   the full-cloud alternative it is the safety baseline before the production cut-over; under the
   **recommended hybrid** it is the standing drift-guard between the `cloud` candidate runtime and the
   `self_hosted` production runtime (and the place the four `cloud`-blocked sources would first show as
   a candidate-vs-production delta, if at all). It calls the new `deploy/delivery/` boundary for the
   AWS work.
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
7. **(Conditional, staged) production cut-over — a NO-OP under the reassessed (hybrid)
   recommendation.** **Under the recommended hybrid this phase is not done**: production stays on
   `self_hosted` (so it keeps reaching every curated source, Finding 2), `deploy/managed-agent/cdk/` +
   `microvm/` are **retained**, and the redesign ships its full primary value (Phases 1–6) without a
   production migration. **Only if the human ratifies full cloud instead** does this phase run: stage
   the `cloud` production path in parallel/behind validation; confirm the owner-facing brief and the
   06:07 weekday send are unchanged (AC-14); supersede the `self_hosted` deployment; **then** retire
   `deploy/managed-agent/cdk/` + `microvm/`. Never a hard swap.

## Alternatives considered

- **Option: retain `self_hosted` for everything (status quo topology).** Rejected. Its sole
  original justification (ADR-0004: `boto3` reaching AWS via the microVM IAM role) is
  eliminated by decoupling delivery, and keeping it *everywhere* means permanently carrying a
  launcher Lambda, a public webhook + WAF + signing secret, a microVM image, the
  `create-microvm-image` build cycle, and the `--platform`-pinning packaging fragility (README
  §Prerequisites) **on the candidate/eval path too** — where it also **fails FR-4** (a candidate
  still needs an image rebuild) and **fails FR-6** (a candidate run still drags in AWS
  infrastructure). The recommended **hybrid retains self-hosted for *production only*** precisely
  to avoid that, while moving candidates/eval to `cloud`. Note the "would only win if the pipeline
  has a hard requirement `cloud` cannot meet" caveat is now **partially activated** by Finding 2:
  `cloud`'s safety blocklist *does* make four curated sources unreachable, which is a genuine
  self-hosted-only advantage — but it justifies self-hosted **for production, not for eval** (a
  candidate faces the blocklist identically and is judged relatively), so it argues for the hybrid,
  **not** for retaining self-hosted everywhere. (No Memory dependency, public data, egress hardening
  never in use — the other retain-everywhere justifications remain absent.)

- **Option: full cloud-for-everything, staged (this ADR's earlier recommendation; now the leading
  alternative).** `cloud` for **both** candidate/eval **and** production, retiring
  `deploy/managed-agent/cdk/` + `microvm/` after a staged, validated production cut-over. This was
  the recommendation in the ADR's prior passes and remains fully specified in Decision 1 as the
  option the human may still ratify. Its case is strong: the single largest, permanent
  operational-surface reduction (no launcher Lambda, webhook, WAF, signing secret, microVM image,
  or build cycle — for production too), and it collapses the redesign to **one** runtime (no
  cloud-vs-self-hosted drift risk to manage). **Why it is no longer the recommendation:** the
  live-confirmed Finding 2 (`cloud`'s safety blocklist permanently and unfixably blocks
  `theverge.com`/`arstechnica.com`/`reddit.com`/`reuters.com`) imposes a permanent source-coverage
  ceiling on the *production* brief — the one surface where reaching every curated source matters
  and where there is no experimentation upside to offset it. Since the epic's entire primary value
  (infra-free candidate iteration + delivery decoupling) lands identically under the hybrid, moving
  production to `cloud` now trades a real, permanent content cost for an operational saving the epic
  does not need. Full cloud stays defensible if the human values the operational-surface reduction
  and one-runtime simplicity above reaching those four mid/lower-tier sources (see Decision 1's
  "what would make its source loss acceptable"); it is simply no longer the default.

- **Option: hybrid — `cloud` for candidate/eval, retain `self_hosted` for production (ADOPTED as the
  reassessed recommendation, 2026-07-06).** Chosen. It gets the epic's big win (cheap, infra-free,
  delivery-free candidate/eval on `cloud`, plus the delivery decoupling) immediately, while leaving
  the live weekday send on the battle-tested self-hosted path — which, crucially, is **not** subject
  to `cloud`'s safety blocklist, so production keeps reaching **every** curated source (the Finding-2
  reassessment above). Its honestly-stated downsides: it keeps the microVM stack alive purely for
  production (so the operational-surface reduction does **not** land for production — only the
  candidate/eval path and delivery decoupling simplify), and it leaves **two** runtimes to reason
  about (a `cloud` candidate and a `self_hosted` production could subtly diverge — the "re-express
  current config as candidate #1 and validate an unchanged brief" step, Phase 5, is the guard against
  that drift). These downsides were, in the prior passes, judged to outweigh the hybrid; **Finding 2
  reverses that judgment** — the permanent production source-coverage ceiling that full cloud imposes
  is now the heavier cost, and the hybrid avoids it at no cost to the epic's core goal. The full-cloud
  option above remains the explicit alternative the human may choose instead.

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
- **The hybrid's eval-vs-production "read recent priors" gap is closed (Decision 2d) with the send
  path still closed to candidates.** A `cloud` candidate can fetch the same recent priors production
  reads via `GET /recent-briefs` on the delivery boundary, reaching parity — while holding **only** a
  read-only bearer token that **cannot** reach `POST /deliver`, so FR-1/FR-7's "a candidate never
  touches delivery / never emails a subscriber" guarantee is preserved by construction (two distinct
  secrets, not trust). It adds **no new AWS delivery IAM** (reuses the delivery Lambda's existing
  briefs-bucket read grants) and is **additive** — production's S3-backed read step is untouched.
- **Every run emits a per-brief source-usage record** (FR-8a, realizing issue #28) as an additive
  sibling to `candidates.json`, seeding a later source-list-consolidation effort — with no change to
  the shipped brief.

Negative / follow-ups (named plainly):
- **Topology choice: the reassessed recommendation (hybrid) does NOT move the production runtime;
  only the alternative (full cloud) does.** Under the recommended **hybrid**, production stays on
  `self_hosted`, so the single biggest regression risk — moving the live, subscriber-facing runtime —
  is **avoided outright**, and Phase 7 (production cut-over) is a no-op. **If the human instead
  ratifies full cloud**, that cut-over is the single biggest risk and is mitigated by Phase 5's
  "re-express current config as candidate #1 and validate an unchanged brief" gate plus a **staged,
  parallel** cut-over (never a hard swap, FR-14), with the security review confirming content
  generation holds no delivery rights (AC-1/AC-7) before cut-over. Either way, **the delivery
  decoupling (Phase 1) is independent of the topology choice** — content generation loses all AWS
  delivery rights and delivery moves to the `deploy/delivery/` boundary regardless — so that safety
  and simplification land under both options. The human's remaining call is narrowly: full
  cloud-for-everything (retire the microVM stack, one runtime, but a permanent production
  source-coverage ceiling) vs. the recommended hybrid (keep the microVM stack for production only,
  two runtimes, but production reaches every curated source). **The recommendation is the hybrid**,
  reassessed 2026-07-06 on Finding 2 (below); full cloud remains supported.
- **The reassessed recommendation trades an operational-surface reduction for full production source
  coverage (Finding 2) — and keeps two runtimes.** Recommending the hybrid means the microVM stack
  (launcher Lambda, webhook, WAF, signing secret, microVM image, build cycle) is **retained purely to
  run production** — so that operational-surface reduction does **not** land for production (it lands
  only for the candidate/eval path, which goes to `cloud`), and there are **two** runtimes to reason
  about (a `cloud` candidate runtime and a `self_hosted` production runtime that could subtly diverge;
  the Phase-5 "unchanged brief" validation is the guard). This is the deliberate, honestly-stated cost
  of not exposing the production brief to `cloud`'s permanent, config-unfixable safety blocklist on
  four curated `sources.md` domains (`theverge.com`/`arstechnica.com`/`reddit.com`/`reuters.com`, all
  Tier 4/7 — Tiers 1–3 are reachable on `cloud`). Finding 1 (the `web_search` 429) is **explicitly
  NOT** a cost here — it was a transient Brave blip, environment-agnostic, and does not weigh against
  `cloud`.
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
  (`GET /v1/agents/{id}/versions`). Under the **recommended hybrid**, production never leaves the
  `self_hosted` stack at all, so there is no production runtime to reverse — the strongest possible
  reversibility posture. Under the full-cloud alternative, the `self_hosted` stack (retained during
  the staged cut-over, or reconstructable from git history if retired) is the fallback if `cloud` ever
  proves unsuitable in production. Nothing here is a one-way door before the Phase-5 validation gate.

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

**Also confirmed live (2026-07-06, a later Phase-5 candidate run + targeted retests; two `cloud`
retrieval findings folded into Decision 1):** (a) **`cloud` `web_search` transient 429 is a Brave
backend blip, not a `cloud` constraint** — a direct Messages-API `web_search` succeeded concurrently
with headroom, and two fresh single-`web_search` cloud sessions ~47 s apart both succeeded cleanly;
this does **not** weigh against `cloud`. (b) **`cloud`'s safety blocklist unconditionally blocks four
curated `sources.md` domains** (`theverge.com`/`arstechnica.com`/`reddit.com`/`reuters.com`:
`403 hostname_blocked` on raw `curl`, `url_not_allowed` on `web_fetch`), with **no config workaround**
(a `limited`-networking env explicitly allow-listing `theverge.com` still 403'd), whereas
`self_hosted` is not subject to it — a real `cloud`-only production content-coverage constraint that
tips Decision 1's production recommendation to the hybrid. (c) Incidental: **environments CAN be
archived/deleted** (`POST /v1/environments/{id}/archive` → 200).

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
   `cloud`. — PARTIALLY ANSWERED live 2026-07-06 (Finding 2); a `cloud`-only constraint was
   found and is the reason the reassessed recommendation keeps production on `self_hosted`.**
   General `cloud` egress is confirmed working, but exercising the skill's actual fetch tools on
   `cloud` revealed that four curated `sources.md` domains
   (`theverge.com`/`arstechnica.com`/`reddit.com`/`reuters.com`) are hard-blocked by `cloud`'s
   safety blocklist with **no config workaround** — so on `cloud` those four are permanently
   unreachable (folded into Decision 1). This is **not** a reason production cannot run on `cloud`
   at all (the Phase-5 cloud brief came out equivalent despite the block), but it is why the
   recommendation now leaves production on `self_hosted` (which is not subject to the blocklist).
   Residual verification, still for the Developer: if the human nonetheless chooses full cloud for
   production, confirm during Phase 5's unchanged-brief validation that the reachable-source subset
   plus the skill's fallback chain (feeds → same-outlet HTML → `web_search`) still produces an
   acceptable brief before any cut-over. (Finding 1's `web_search` 429 was separately confirmed a
   transient blip, not a tooling constraint — see the "Also confirmed live" note above.)

**Items needing the owner's explicit sign-off beyond the core recommendation:**
- **~~Ratify the environment topology~~ — RATIFIED by the human 2026-07-06: the HYBRID** (`cloud` for
  candidate/eval, `self_hosted` retained for production). Reassessed to the hybrid on the live-confirmed
  Finding 2 (`cloud`'s safety blocklist permanently blocks four curated `sources.md` domains, no config
  workaround; `self_hosted` is not subject to it), which imposes a permanent source-coverage ceiling on
  the production brief that the hybrid avoids at no cost to the epic's core goal. **Full
  cloud-for-everything (staged)** remains fully documented as **considered, not chosen** — a legitimate
  future option, but the topology is now settled. (Decision 1 — now Accepted.)
- **Retirement posture (follows from the ratified hybrid): `deploy/managed-agent/cdk/` + `microvm/` are
  RETAINED** (production runs on them) and Phase 7 (production cut-over) is a no-op. (Retirement would
  have applied only under full cloud, which was not chosen.)
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
- Confirm the **recent-priors read endpoint** (Decision 2d): `GET /recent-briefs` on the existing
  `deploy/delivery/` boundary, gated by its **own separate, read-only bearer secret** (distinct from the
  `POST /deliver` delivery/send secret) so a `cloud` candidate can fetch the recent priors production
  reads **without** ever gaining the ability to trigger a send (FR-1/FR-7 preserved). Synchronous `GET`
  (no async needed — reads ~3 small S3 objects); **no new AWS delivery IAM** (the delivery Lambda's
  existing `S3ListBriefsPrefix` + `S3AudioReadWrite` already cover it; the only new IAM is the read
  secret's `GetSecretValue`); **purely additive** — production's own S3-backed `read-recent-briefs` step
  is untouched (FR-14/AC-14). This decision follows from the ratified hybrid (it closes the hybrid's
  eval-vs-production "read recent priors" fidelity gap).
- **[NEW — token-delivery correction, needs sign-off] Confirm how the read token reaches a `cloud`
  candidate** (Decision 2d → "Correction (2026-07-06): how the read token actually reaches a `cloud`
  candidate"). The originally-specified env-var-on-environment channel does **not** work (live-probe
  evidence: `config.environment`/`init_script`/`packages` are all rejected at environment-create in this
  beta; only `type`/`networking` are settable), so a token must be injected per run via `initial_events`
  (the task prompt) — which puts it in the session transcript. **Recommended permanent mechanism: a
  short-lived, HMAC-signed read token** (the ADR-0011 scheme + an `exp` claim, signed with the existing
  read secret, verified by `GET /recent-briefs`), so a transcript-leaked token dies in minutes with no
  per-run rotation — the property the **many-run eval epic** needs. This affects that (deferred) eval epic,
  so it is flagged for your sign-off. The **interim** approach (static token via `initial_events`, read
  secret rotated + Lambda cold-started after) is fine to keep using until you ratify. (Same `initial_events`
  correction applies to Decision 2b's delivery/send token; whether that one should also become short-lived
  is a Phase-1 follow-up.)
- Confirm the **ADR-0008 reconciliation** (Decision "Reconciling ADR-0008"): the three-way lockstep
  collapses to **two-way** (in-repo ↔ live Skills-API) because the **local Desktop fallback is
  dead** — **unconditionally, with no reactivation hedge** (this is already the owner's stated
  direction via rev.-2 feedback #2, so this item is confirming the ADR reflects it, not re-deciding
  it); the image-rebuild half of the 2026-07-04 amendment **disappears for the candidate/`cloud`
  path** (FR-5). Under the **recommended hybrid** it **remains in force for the retained `self_hosted`
  production skill** (not superseded — the production skill is still image-baked); under the full-cloud
  alternative it is superseded everywhere (no image to rebuild). The image-rebuild relief must be
  scoped to the candidate/`cloud` path if the hybrid is ratified.
- Acknowledge the new **per-brief source-usage record** (FR-8a, issue #28): an additive,
  skill-content-driven sibling of `candidates.json`, emitted every run, not changing the shipped
  brief (Decision 2a).
- ~~Confirm the FR-8 PRD interpretation this ADR builds on~~ — **CONFIRMED by the owner,
  2026-07-06**: "The listening script is the output. No actual TTS for evals." A candidate run
  retrieves the listening-script *text* only; audio (Polly = AWS) is never synthesized
  or retrieved for a candidate run. Settled.
