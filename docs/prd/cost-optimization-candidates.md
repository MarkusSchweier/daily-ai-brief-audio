# PRD: Agent cost optimization — eval-harness re-integration + candidate comparison

- Status (2026-10-11): **step C has produced its first matched, replicated comparison** — the production
  configuration and the three current-generation models, three repetitions each, judges on Opus 5.5
  (§6). **Decided the same day: production moved to Claude Haiku 5.5 at effort high** (§6, live from
  the Mon 2026-10-12 06:07 Europe/Berlin run).
- Status: **ACTIVE (2026-07-07).** The blocking dependency (Epic 2, `agent-system-redesign.md`)
  **shipped and merged**, so this epic is now being built as **one combined epic** (owner decision
  2026-07-07) with three steps: **A** re-integrate the eval harness with the new candidate
  mechanism, **B** configure & deploy the candidate set, **C** run the comparison and decide the
  future production set-up. Sequenced **B → A → C** (owner decision): B's candidate declarations are
  built first because they de-risk A's design (they surface exactly what the harness must support).
  **B's candidate declarations are BUILT** on branch `feat/cost-optimization-candidates` (see §3);
  **A is developed next in the main Claude Code thread** (with production-system context); **C**
  has since run (§6).
- Author: product-manager (Claude), in conversation with the owner — Date: 2026-07-05, **substantially
  updated 2026-07-07** (epic activated, combined A/B/C, candidate set finalized, A requirements added).
- Source: follow-on from the eval-harness epic (shipped) — that epic built the *measurement
  infrastructure*; this epic is *what gets measured* plus *re-wiring the measurement to the new
  candidate mechanism the redesign produced*.

## 1. Problem

The daily brief pipeline costs ~$2.60–2.65/run in Claude Sonnet 5 usage (real transcript-mined
analysis), dominated by cache-read tokens, with the **post-research writing phase costing *more*
than research** (~4.2M vs ~1.2M cache-read tokens) because every subsequent turn in one long
agentic session re-sends the accumulated research context. The owner wants to find a cheaper
configuration — a different model, a different session/task structure, or a different architecture —
that holds quality roughly constant while cutting cost.

The redesign (Epic 2) delivered the *mechanism* to declare/deploy/run candidates cheaply
(`deploy/candidates/`, cloud, artifact retrieval via Claude-Platform APIs) — but it deliberately did
**not** re-wire the existing `deploy/eval/` harness to that mechanism. The harness's trigger still
fires at a hardcoded `PRODUCTION_AGENT_ID`/`PRODUCTION_ENVIRONMENT_ID` and has **no** awareness of
`deploy/candidates/`. So today you can *run* a candidate and read its cost, but you cannot
systematically *judge quality-vs-cost* across candidates. This epic closes that gap and then uses it.

## 2. Epic structure — A / B / C

- **A — Re-integrate the eval harness with the new candidate mechanism.** Five seams (§4). Delivered
  with a UI (§4.1) for defining/triggering eval runs and assessing/comparing them. Built next, in the
  main thread.
- **B — Configure & deploy the candidate set.** The declarations in `deploy/candidates/` + syncing
  them to real Platform `agent_id`s. Declarations **built** (§3); **sync/deploy pending owner review**.
- **C — Run & decide.** Run the candidates through A, compare on quality + cost, and decide the future
  production set-up (which may or may not trigger a production cut-over — a separate, owner-gated
  decision).

## 3. B — Candidate set (BUILT as declarations; not yet synced)

Each candidate is a git-tracked declaration under `deploy/candidates/<slug>/` (per-dimension files;
multi-agent adds `multiagent.json`). All are **content-generation only**: they hold **no** AWS/delivery
role (FR-1), never fan out to subscribers, and **never synthesize audio** (no TTS in evals — owner-
confirmed 2026-07-06, reaffirmed 2026-07-07). Each candidate's only delivery-side contact is the
read-only `GET /recent-briefs` route; **every eval run always fetches recent priors** (owner
requirement) exactly as production does, via that route (Step 0 of each task prompt).

**Design principle (owner requirement 2026-07-07):** for the decomposition candidates, the
`daily-ai-brief` skill is **unchanged** and every sub-agent references it, scoped by a thin task-prompt
to a slice of the skill's own numbered Daily workflow. Prompt **wording is preserved verbatim** from
the baseline wherever possible, so a comparison contrasts *structure/model*, never reworded prompts.

| # | Slug | Structure | Models | Isolates | Built |
|---|------|-----------|--------|----------|-------|
| 1 | `production-baseline` | single agent, skill end-to-end | Sonnet | reference point | pre-existing (Epic 2 Phase 5) |
| 2 | `haiku-swap` | single agent, skill end-to-end | **all Haiku** | pure model swap, no structure change | ✅ |
| 3 | `multiagent-aggressive-haiku` | coordinator + 4 sub-agents | Sonnet coord+**selection**; **Haiku** research+writing+listening-script | how far Haiku goes with Sonnet only where editorial judgment lives | ✅ |
| 4 | `session-restructure` | coordinator + 4 sub-agents (**byte-identical prompts to #3**) | **all Sonnet** | does structural decomposition *alone* (no full-context replay) cut cost? | ✅ |
| 5 | `haiku-swap-verified` | Haiku-as-coordinator + 1 Sonnet verifier sub-agent | **all Haiku** pipeline + **Sonnet** verify/correct pass | does a cheap post-assembly verification pass fix haiku-swap's measured weaknesses (factual insurance + dedup labelling) at ~60% below baseline? (GitHub #38 item 1; verifier explicitly does NOT touch selection) | ✅ run 2026-07-07: **retired** — the Haiku coordinator raced past the verifier (fabricated the report, captured the uncorrected brief); $3.27, 3/3/4/2. Superseded by #6/#7's lessons (structural gates; verifier work preserved by contract). |
| 6 | `haiku-swap-hardened` | single agent (haiku-swap + checklist-hardened task prompt ONLY) | **all Haiku** | does prompt-forcing alone (mandatory per-prior reading, explicit labelling rule, enumerated self-check + overlap-notes audit) recover Haiku's diligence at ~haiku-swap price? First run 2026-07-08: $0.52, 3/4/4/2 — labels landed but one prior was neglected; enumerate-the-priors fix applied 2026-07-08. | ✅ matched batch 2026-10-11 (vs. the three 5.5 candidates): $0.57/run, mean 3.25; this is the production configuration (§6). |
| 7 | `haiku-digest-sonnet-select` | Haiku coordinator (file-gated) + research/writing/script on Haiku + **selection on Sonnet reading a compact digest contract** | Haiku everywhere except the selection decision | can the digest contract keep Sonnet-grade selection at ~1/4 of mah's selection cost? First run 2026-07-08: **$1.36, factual 5 / length 5 / dedup 4** (content blanked by an artifact truncation); all four file-gates executed, selection thread $0.29 vs mah's $1.21. | ✅ |
| 8 | `haiku-5-5-hardened` | single agent, #6 with only model + effort changed | **all Haiku 5.5**, effort high | model generation at a fixed prompt (Haiku 4.5 → 5.5) | ✅ synced 2026-10-11; matched batch 2026-10-11: $0.60/run, mean 3.92 (§6) |
| 9 | `sonnet-5-5-hardened` | single agent, #6 with only model + effort changed | **all Sonnet 5.5**, effort high | model generation at a fixed prompt (Sonnet tier) | ✅ synced 2026-10-11; matched batch 2026-10-11: $1.75/run, mean 4.17 (§6) |
| 10 | `opus-5-5-hardened` | single agent, #6 with only model + effort changed | **all Opus 5.5**, effort medium | model generation at a fixed prompt (Opus tier) | ✅ synced 2026-10-11; matched batch 2026-10-11: $3.99/run, mean 3.92 (§6) |

**#3 model split (made aggressive per owner direction 2026-07-07):** Sonnet retained **only** for
coordination and editorial **selection** (skill steps 4–5, where source/dedup judgment and
hallucination risk concentrate); **Haiku** does gathering (steps 1–3), the token-heavy writing (steps
6–7, the biggest cost phase), and the mechanical listening-script rewrite. The **factual-accuracy
judge is the make-or-break metric** for #3 (Haiku-on-research concentrates fabrication risk; Sonnet-
selection is an imperfect safety net that vets what Haiku gathered). #3 is bracketed by #2 (100% Haiku)
and #4 (0% Haiku, same structure) so a regression is attributable.

**#4** realizes the doc's original "session restructuring" idea: multi-agent **is** the mechanism —
each sub-agent is a fresh context that picks up only its predecessor's `/workspace` output file, not
the whole transcript. It is #3 with every sub-agent forced to Sonnet, so **#3-vs-#4 isolates the Haiku
lever** and **#4-vs-baseline isolates the decomposition structure**.

**#7 (thinking-budget/effort sweep) — BLOCKED BY THE PLATFORM (probed live 2026-07-07).** GitHub
issue #38's items 2/3 (`sonnet-low-effort`, `session-restructure-low-effort`) tried to realize this
sweep as concrete candidates, but the Managed Agents Agents API exposes **no effort/thinking knob**:
a top-level `parameters` field is rejected (`unknown field "parameters"` — so a non-empty
`parameters.json` fails loud at sync, never silently); `model.thinking` is rejected the same way; and
`model.effort` is **accepted but silently DISCARDED** (proven by the no-op discriminator: re-sending
the same model with `effort: low`, then `effort: high`, both returned the SAME version with no new
version created — the field is parsed and dropped, never stored; the only documented model-object
knob is `speed`, i.e. fast mode, the opposite of a cost lever). Building these candidates today would
have produced configurations that LOOK like low-effort but run at default — silent experiment
corruption. Parked until the Managed Agents surface exposes effort/thinking on agent definitions;
`parameters.json` stays declaration-only until then. **#4 and its low-effort derivative are also
deprioritized** (owner, 2026-07-07 evening): #4's cost is bounded below by #3's (same structure,
strictly costlier models) and #3 already showed almost no saving vs. baseline — its diagnostic value
doesn't currently justify a run.

**Update 2026-10-11 — unblocked.** The Agents API now accepts `model: {"id", "effort"}`, and a live
read-back echoes `{"effort": {"type": "high"}, …, "speed": "standard"}`, so the 2026-07-07 finding above
no longer holds. `parameters.json` now supports exactly one key, `effort` (`low`/`medium`/`high`/`xhigh`/
`max`), which the sync maps to `model.effort` and validates at load. The 5.5 batch (§6) uses it:
`haiku-5-5-hardened` at high, `sonnet-5-5-hardened` at high, `opus-5-5-hardened` at medium. Haiku 4.5
has no effort knob. The paragraph above is kept as history.

### Backburnered (owner decision 2026-07-07) — recorded, not built

- **#5 "hybrid split" (Haiku listening-script only, kept in-session).** LOW potential: it changes only
  the *model* of one small end-of-run step while still paying the in-session context-replay cost, and
  is strictly dominated by #3 (which already runs the listening-script on Haiku in a decomposed,
  no-replay way). Not worth the multi-agent complexity.
- **#6 "pull mechanical subtasks into stateless Messages/Batches calls."** LOW potential *now*:
  candidate **#4's full decomposition already eliminates the per-step context replay** its thesis
  targets (the listening-script sub-agent starts fresh with just the brief). #6's only marginal gain
  over that is the Batches 50% discount on one small call, at the cost of out-of-agent pipeline
  plumbing. **Latent value:** it is the proof-of-concept for the pattern *"move mechanical work out of
  the agent entirely into cheap Batches"* — revisit as a fast-follow **if** #3/#4 results show the
  mechanical steps still cost meaningfully.

### Finding that reshaped the set (2026-07-07)

The original candidate list (2026-07-05) predated the redesign making HTML deterministic. Candidates
#3/#5/#6 all listed **"HTML conversion"** as an LLM subtask to split off or cheapen — but HTML is now
`deploy/delivery/functions/deliver/delivery_core.py::derive_html()`, **zero-LLM**. That lever is
**void**; the only genuine "mechanical subtask" left is the listening-script, which is why #3/#4 keep
it as one Haiku/Sonnet sub-agent and #5/#6 lost most of their reason to exist.

## 4. A — Eval-harness re-integration (built next, in the main thread)

Re-wire `deploy/eval/` to target arbitrary candidates from `deploy/candidates/` and judge their
Claude-Platform-retrieved artifacts. Five seams:

1. **Trigger targeting** — `functions/trigger/handler.py` must resolve a **named candidate** (its real
   `agent_id` + the shared `cloud` environment) instead of the hardcoded
   `PRODUCTION_AGENT_ID`/`PRODUCTION_ENVIRONMENT_ID`; the candidate id must actually drive what runs.
2. **Artifact retrieval** — replace the S3 poll path with the redesign's **Sessions-events-API**
   retrieval (already in `candidate_sync/trigger.py`); candidates are delivery-free (no S3). Biggest
   rewrite; also a **consolidation** opportunity (one retrieval path).
3. **Record schema** (`eval_core/record.py`) — store real candidate identity (slug + `agent_id` + git
   ref) + retrieved artifacts, not a bare label.
4. **Judges + cost-miner** — point the 4 judges at the new artifact source; cost from Sessions-events
   token data.
5. **Review/comparison UI** (§4.1).

### 4.1 UI requirements (owner, 2026-07-07)

**Conducting eval runs** — from the UI you can define and trigger an eval run, configuring:
- **Select the agent**: a candidate **or** production; single-agent **or** multi-agent.
- **A name** for the eval run.
- **Number of repetitions.**
- **Whether an eval email is sent to `mail@mschweier.com`** for the run, **or not**. *(Owner decision
  2026-07-07: exclude TTS/Polly from evals — so the eval email, when enabled, is **HTML-only, no
  audio**. Architecturally this is the delivery boundary's `POST /deliver` invoked in **owner-only
  mode** (subscriber fan-out OFF) — which unlocks `POST /deliver` for eval use. **Invariant: an eval
  run NEVER fans out to subscribers.**)*
- **Which eval criteria** the judges test against (a **subset** — no need to test all every run).
- **Trigger** the run.
- **Run states:** `configured`, `running`, `completed`, `failed`.

**Assessing eval runs** — a one-page overview listing/comparing all eval runs in a table:
- Columns: eval-run **name**, **model**, **thinking parameters**, **agent vs multi-agent**, **# of
  repetitions**.
- Runs conducted with the **current production configuration are marked** as such.
- The **full set of criteria** is shown as columns (blank for criteria a run didn't test), plus the
  **cost** of the run.
- The **human eval** for the run is shown if applicable.

**Deep dive into an eval run** — clicking a run opens a detail page where you can:
- explore individual **repetitions**;
- see the **candidate configuration**, including the **prompts for the main and sub-agents**;
- **render the MD or HTML** of the brief.

## 5. C — Run & decide

Run the candidates through A (replicates each; judge on the selected criteria + cost), compare on the
comparison table, and **decide the future production set-up**. A decision to move production onto a
cheaper candidate is a **separate, owner-gated cut-over** (mirrors the delivery-decoupling cut-over
discipline) — not automatic from a good eval result.

## 6. Key decisions, flags & open items

- **[DECIDED — owner, 2026-10-11] Production moved to Claude Haiku 5.5 at effort high** (the
  `haiku-5-5-hardened` configuration: the unchanged hardened prompt, model and effort only), on the
  results entry below — the accuracy gain over Haiku 4.5 at about the same cost, with Sonnet 5.5's edge
  inside the noise at ~2.5× the cost and Opus 5.5 showing no gain at ~5.5×. Live: production agent
  `agent_01EswBTose8dnTAUDbGvzdLq` v3 = `{id: claude-haiku-5-5, effort: high}` (v2 = Haiku 4.5 and v1 =
  Sonnet 5 preserved); scheduled deployment `depl_01YRoLodhcr3HuhVS3zxVuLx` pins v3 with prompt,
  environment and schedule byte-identical to its predecessor `depl_016LWhcSEghrvGknVWhZvgdr` (archived).
  First Haiku 5.5 production send: Mon 2026-10-12 06:07 Europe/Berlin. Validated beforehand on the
  production self-hosted environment with the delivery step removed (one full run, 4.2 min, all five
  outputs written, nothing delivered — the delivery Lambda saw only the read-only recent-briefs call).
  **Rollback:** a deployment identical to the live one but pinned to agent v2, then archive the Haiku 5.5
  one (`deploy/managed-agent/deployment.json` history has the recipe). The eval harness's
  production-config marker now points at `haiku-5-5-hardened`.
- **[CORRECTION — 2026-10-11] Haiku 5.5 cost is understated in the results entry below.** Sessions now
  report the Platform's own `usage.list_cost`. For Haiku 4.5, Sonnet 5.5 and Opus 5.5 it matches the
  harness (tokens + $0.01 per billed search) to the cent on all nine batch sessions. For Haiku 5.5 it is
  $0.04–0.08 per run higher (0.71 / 0.87 / 0.86 against the harness's 0.63 / 0.79 / 0.82 all-in), which
  equals pricing **every** request on the over-100K-token rate card rather than request by request. So
  Haiku 5.5 is about **$0.81 per run all-in (~$18/month)**, not $0.75; the ranking and the decision are
  unaffected. Which rule the invoice follows is not yet settled — tracked as a harness follow-up (store
  the Platform figure in run records, reconcile the Haiku 5.5 rule).
- **[RESULTS — 2026-10-11] Model-generation matched batch.** Four candidates
  launched in the same second on the same news snapshot (a Sunday): the production configuration
  (`haiku-swap-hardened`) and three 5.5 candidates that change only model and effort. Each ran three
  sequential repetitions in the `cloud` environment, judged on all four criteria by Opus 5.5 at effort
  high (ADR-0016 amendment, 2026-10-11). Entries are mean ± sample stdev over n=3, scores out of 5;
  "Mean" is the unweighted mean of the four criterion means.

  | Candidate | Model / effort | Token cost per run | + web search | Content | Factual | Format | Dedup | Mean |
  |---|---|---|---|---|---|---|---|---|
  | `haiku-swap-hardened` (production) | Haiku 4.5 / — | $0.57 ±0.10 | $0.05 | 3.3 ±0.6 | 2.7 ±0.6 | 2.7 ±0.6 | 4.3 ±0.6 | 3.25 |
  | `haiku-5-5-hardened` | Haiku 5.5 / high | $0.60 ±0.09 | $0.15 | 3.3 ±0.6 | 4.0 ±0.0 | 3.7 ±1.2 | 4.7 ±0.6 | 3.92 |
  | `sonnet-5-5-hardened` | Sonnet 5.5 / high | $1.75 ±0.33 | $0.19 | 3.7 ±0.6 | 4.3 ±0.6 | 3.7 ±0.6 | 5.0 ±0.0 | 4.17 |
  | `opus-5-5-hardened` | Opus 5.5 / medium | $3.99 ±0.23 | $0.10 | 3.7 ±0.6 | 4.0 ±0.0 | 3.0 ±0.0 | 5.0 ±0.0 | 3.92 |

  Accuracy-judge "contradicted" verdicts per brief, out of 13–23 claims checked: haiku-swap-hardened
  5, 6, 4; haiku-5-5-hardened 1, 0, 0; sonnet-5-5-hardened 1, 0, 2; opus-5-5-hardened 1, 1, 0. Mean
  web searches per run, in table order: 5 / 15 / 19 / 10.

  Format scores are held down for every candidate by the same thing: deep-dives overshoot the skill's
  3–6 sentence target. That is a prompt and skill property, not a model difference. Haiku 5.5 is priced
  per request by prompt length ($0.10/$0.50 per MTok up to 100,000 prompt tokens, $0.50/$2.50 above).
  In this batch 70–80% of its requests exceeded 100,000 prompt tokens and were billed on the higher
  card; an all-short-prompt run would have cost about a fifth of what was observed. That is why it costs
  about what Haiku 4.5 does here, despite a list price 10× lower.

  Caveats: n=3 on one news snapshot, so differences under roughly half a point are within noise. Effort
  differs by design (high / high / medium; Haiku 4.5 has no effort knob). The hardened prompt was written
  for Haiku 4.5's weaknesses, and the simpler baseline prompt was not tested on the 5.5 models. `cloud`
  blocks four curated source domains that self-hosted production reaches. The Opus 5.5 candidate is judged
  by the same model it runs on. The batch was re-pinned on 2026-10-11 (`haiku-swap-hardened` and the three
  5.5 candidates) from skill version 1783340601977967 to 1783347998026490, the version production's
  `latest` resolves to; the other candidates remain on the old pin.

  Repaired repetition: in `haiku-swap-hardened` repetition 3 the agent wrote its five outputs, but its
  capture step (`cat … | head -100`) captured only prior briefs, so the harness graded the 2026-10-09
  production brief as the output (superseded scores: factual 2, format 3, dedup 4). The five files were
  recovered verbatim from the stored write-tool inputs, and the judges re-ran on the real brief with no
  pipeline re-run. The table uses the repaired scores (content 3, factual 3, format 2, dedup 5); the
  superseded scores and their spend stay in that repetition's `run-meta.json`. The harness now refuses
  to judge a brief equal to a delivery-feed entry, or when no brief was captured. All nine 5.5-model runs
  followed the capture step.

  Total spend for the exercise was about $44 (pipelines $20.73, pipeline web search $1.46, judges $18.68,
  a $2.32 smoke run, and one superseded $1.01 judge call). The batch itself made no production change; the
  owner's decision taken on it is the entry above. Run records:
  `deploy/eval-harness/runs/<slug>/2026-10-11-104933-*-matched-5-5-2026-10-11/`.

- **[DECIDED — owner, 2026-07-13] Production moved to candidate #6 (`haiku-swap-hardened`) ahead of
  step C.** Overrides this PRD's §7 "no production cut-over in this epic" non-goal by explicit owner
  decision (no pre-flip validation run, owner-waived). Live: production agent v2 = Haiku 4.5 (v1 =
  Sonnet preserved for rollback), scheduled deployment `depl_016LWhcSEghrvGknVWhZvgdr` (pins agent v2,
  hardened prompt + unchanged decoupled delivery), old Sonnet deployment archived. First Haiku
  production send: Tue 2026-07-14 06:07 Europe/Berlin. The step-C matched evals still measure the
  Sonnet config via the `production-baseline` candidate (its own cloud agent, unaffected), so the
  comparison remains valid — and can still argue production back to Sonnet or on to #7.

- **[DECIDED] Combined A/B/C epic**, sequenced **B → A → C** (owner, 2026-07-07).
- **[DECIDED] No TTS/Polly in evals** (owner, reaffirmed 2026-07-07). Eval email, when enabled, is
  HTML-only to the owner via `POST /deliver` owner-only mode. Evals never touch Polly and never fan out
  to subscribers.
- **[DECIDED] Aggressive #3** (Haiku on research+write+script; Sonnet only coord+select) and
  **backburner #5/#6** (owner, 2026-07-07 — rationale in §3).
- **[A-verification item — creation shape + shared filesystem RESOLVED 2026-07-07; live run still
  pending] Multi-agent execution semantics.** The candidate *declarations* (models, structure, which
  skill-steps each sub-agent runs) are final. Two of the previously-open questions are now confirmed
  against the live Platform + official docs (platform.claude.com/docs/en/managed-agents/multi-agent):
  (1) **the coordinator references each sub-agent by id** as
  `multiagent.agents: [{"type":"agent","id":<id>}]` — a plain reference object, **no `entry` wrapper**
  (the sync's original guess; fixed, and all four candidates are now synced with real coordinator +
  sub-agent `agent_id`s); and (2) **all agents share one sandbox filesystem** ("All agents share the
  same sandbox, filesystem, and vault credentials"), so the decomposition candidates' `/workspace`
  artifact hand-off **is valid** (only per-agent context/tools are isolated, not the filesystem). What
  remains for A is a **live end-to-end multi-agent RUN** confirming the coordinator actually delegates
  the skill-scoped phases and the `/workspace` hand-off works in practice — the sub-agent *delegation*
  wording may still be refined then (not the structure/models).
- **[BRANCH TOPOLOGY]** B is built on `feat/cost-optimization-candidates`, branched off `origin/main`
  (which has the delivery-**decoupling** work, PR #33, but **not** the later production-delivery-
  **cut-over** branch). A is to be developed in the main thread, which carries the cut-over context.
  Whoever integrates must reconcile these branches.
- **[NOT DONE] B deploy (sync).** The declarations are written but **not synced** to the Platform
  (no real `agent_id`s minted yet) — pending owner review of the declarations.

## 7. Non-goals

- **No production cut-over in this epic.** Choosing a cheaper config for production is a separate,
  owner-gated decision after C.
- **No change to the `daily-ai-brief` skill content.** Decomposition candidates reference the
  unchanged skill, scoped by prompt (preserves the wording-contrast discipline).
- **No re-litigation of the eval v1 criteria** (content selection, factual accuracy, length/format,
  dedup) beyond letting a run test a **subset** of them. *(Amended 2026-07-07: this non-goal was
  lifted by the owner for judge **METHODOLOGY** specifically — the criteria SET stays the same four
  above, unchanged, but real committed runs exposed genuine judge-quality defects (knowledge-cutoff
  bias in `factual_accuracy`; same-day dedup contamination), so the owner directed a same-day judge
  methodology v2 rework of HOW three of the four are judged — see
  `docs/adr/0016-eval-harness-reintegration.md`'s dated amendment and `deploy/eval-harness/README.md`'s
  judges section. `length_format` stayed unchanged, and no criterion was added or removed.)*
- **Source-list trimming** (GitHub issue #28) stays separate/orthogonal (applies under any candidate).

## 8. Dependencies

- **Epic 2 (agent-system-redesign) — SHIPPED/merged.** Delivered `deploy/candidates/` (declarations +
  `sync.py`/`trigger.py`), the shared `cloud` environment, and `GET /recent-briefs`. Its native
  update-in-place agent versioning (one stable `agent_id` per candidate) is what B's declarations sync
  into.
- **`deploy/eval/` (shipped).** A re-wires it (§4). Its fail-closed `ENABLE_SUBSCRIBER_FANOUT` gate
  remains the correct guard for the **production** delivery path; eval runs use `POST /deliver`
  owner-only.
- **`deploy/delivery/` (live).** `GET /recent-briefs` used by every eval run; `POST /deliver` (owner-
  only) is what an "email this eval to me" run invokes. `POST /deliver`'s bearer secret is currently
  undistributed — A must arrange eval-side access without granting subscriber fan-out.

## 9. Rollout / status

- **B (declarations): DONE** — `haiku-swap`, `multiagent-aggressive-haiku`, `session-restructure` built
  and loader-validated on `feat/cost-optimization-candidates`; `production-baseline` pre-exists.
  **Pending:** owner review → sync/deploy (mint `agent_id`s) → `#7` parameter-sweep variants.
- **A (eval re-integration): NEXT** — in the main thread (§4 + §4.1).
- **C (run & decide): after A** — run replicates, compare, decide future production set-up (§5).
- **Success metric:** the owner can trigger an eval of any candidate from the UI, compare candidates on
  quality + cost in one table, and make an evidence-based call on a cheaper production config — with
  zero risk to the live daily send at any point (evals are content-only, delivery-free except an
  optional owner-only HTML email).
