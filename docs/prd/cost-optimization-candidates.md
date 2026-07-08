# PRD: Agent cost optimization — eval-harness re-integration + candidate comparison

- Status: **ACTIVE (2026-07-07).** The blocking dependency (Epic 2, `agent-system-redesign.md`)
  **shipped and merged**, so this epic is now being built as **one combined epic** (owner decision
  2026-07-07) with three steps: **A** re-integrate the eval harness with the new candidate
  mechanism, **B** configure & deploy the candidate set, **C** run the comparison and decide the
  future production set-up. Sequenced **B → A → C** (owner decision): B's candidate declarations are
  built first because they de-risk A's design (they surface exactly what the harness must support).
  **B's candidate declarations are BUILT** on branch `feat/cost-optimization-candidates` (see §3);
  **A is developed next in the main Claude Code thread** (with production-system context); **C**
  follows.
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
| 6 | `haiku-swap-hardened` | single agent (haiku-swap + checklist-hardened task prompt ONLY) | **all Haiku** | does prompt-forcing alone (mandatory per-prior reading, explicit labelling rule, enumerated self-check + overlap-notes audit) recover Haiku's diligence at ~haiku-swap price? First run 2026-07-08: $0.52, 3/4/4/2 — labels landed but one prior was neglected; enumerate-the-priors fix applied 2026-07-08. | ✅ |
| 7 | `haiku-digest-sonnet-select` | Haiku coordinator (file-gated) + research/writing/script on Haiku + **selection on Sonnet reading a compact digest contract** | Haiku everywhere except the selection decision | can the digest contract keep Sonnet-grade selection at ~1/4 of mah's selection cost? First run 2026-07-08: **$1.36, factual 5 / length 5 / dedup 4** (content blanked by an artifact truncation); all four file-gates executed, selection thread $0.29 vs mah's $1.21. | ✅ |

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
