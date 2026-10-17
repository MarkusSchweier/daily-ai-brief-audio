# PRD: Put the curated source URLs in the user message (restore `web_fetch`)

- Status: **Deployed 2026-10-17 — AC-10 pending.** The owner approved the solution and authorized
  the full build → test → evaluate → deploy cycle (including the production cut-over) on
  2026-10-17. The cycle is complete except AC-10, which is judged on the first three production runs
  (2026-10-19 to 10-21). The outcome is in §9; two deviations from the approved plan are recorded
  there (the production agent is v4, and the block carries two rules beyond FR-4).
- Author: product-manager (Claude) · Date: 2026-10-17
- Linked ADRs: ADR-0018 (accepted 2026-10-17, amended post-evaluation). It touches the prompt layer
  of ADR-0006's scheduled deployment and the candidate/eval mechanism of ADR-0014/0016; none of
  those decisions changes.
- Scope boundary: **prompt layer only.** The `daily-ai-brief` skill, the delivery boundary
  (`deploy/delivery/`), and the environment configurations are **untouched**. The production agent
  definition was approved as unchanged (v3); at cut-over it moved to **v4** (v3 plus the
  `web_fetch` content cap). That is the one exception, recorded in §9.3.

## 1. Problem

Anthropic emailed on **2026-10-16** ("Changes to web_fetch and web_search in Claude Managed
Agents") announcing two changes:

1. **`web_fetch` can only retrieve a URL that already appeared in the conversation** — in a user
   message, in a client-side tool result, or in an earlier `web_search`/`web_fetch` result. A URL
   that exists only in Claude's own output or in the system prompt cannot be fetched. The
   recommended fixes are to **put the URL in the user message**, or to **search first and fetch the
   result URLs**.
2. A cloud environment's `limited` networking `allowed_hosts` now also limits the web tools.
   Official docs (platform.claude.com/docs/en/managed-agents/environments and
   /tools-web-restrictions) state that `unrestricted` networking and self-hosted environments do
   **not** limit these tools.

**Change 2 does not affect this pipeline.** Production runs in a `self_hosted` environment, the
eval `cloud` environment is `unrestricted`, and the agent sets no per-tool domain lists.

**Change 1 is already breaking production research**, and has been since **2026-10-08** — per the
session event logs:

| Production run | Successful `web_fetch` | Rejected |
|---|---|---|
| 2026-08-25 → 2026-10-07 | 2–11 per run | 0 |
| 2026-10-08 | 2 | 1 |
| 2026-10-09 | 1 | 6 |
| 2026-10-12 → 2026-10-16 (Haiku 5.5) | **0** | 3–6 **every run** |

The rejections carry the error `url_not_in_prior_context`, and the rejected URLs are **mostly the
curated sources from the skill's `sources.md`** — Anthropic, OpenAI, Mistral and Meta news pages,
Hugging Face Papers, TechCrunch AI.

**The agent already reads `sources.md`, and that does not count.** It opens the file (62 URLs) with
its built-in file/shell tools; in the 2026-10-16 run, **4 of the 5 rejected URLs had appeared
verbatim in an earlier bash/read tool result**. The email says *client-side* tool results count —
built-in tool results evidently do not.

**Consequences.** Sends are unaffected (every weekday: 5 sent / 0 failed), but **research now runs
on `web_search` alone** — 9–19 searches per run, up from 0–7 — and the curated **primary sources are
no longer read at all**. In the 2026-10-11 matched eval (same rejections, cloud), Opus 5.5 recovered
by searching first and then fetching the result URLs (the email's second recommended pattern,
14–20 successful fetches per run); **Haiku 5.5 — the production model — manages only 0–4**. So the
pipeline cannot be left to self-heal: the fix has to be in the prompt.

## 2. Goals & non-goals

### Goals
- **Restore first-party source reading.** The curated `sources.md` URLs are fetchable again in every
  production run, without the agent having to discover them via search first.
- **Keep `sources.md` the single source of truth.** The URL block in the prompts is *generated* from
  it, and a test fails if the two diverge.
- **Teach the agent the new rules** so a rejection is handled, not repeated: fetch feed/index pages
  to unlock the article links inside them, never retry a rejected URL, fall back to `web_search`.
- **Prove it before production.** A matched eval on the cloud environment against the current
  production configuration, then a no-delivery validation run on the production self-hosted
  environment, then the cut-over.
- **Cost stays visible.** Any per-run cost increase from fetched content is measured and explained
  before the cut-over, with a named mitigation.

### Non-goals (explicitly out of scope)
- **No change to the `daily-ai-brief` skill** — not `sources.md`, not the workflow, not the output
  contract. `sources.md` is read, never edited, by this change.
- **No model change.** Production stays on the agent v3 definition (Claude Haiku 5.5, effort high)
  decided 2026-10-11. Agent versions are immutable; none is created here. *(One was, at cut-over:
  v4 = v3 plus the `web_fetch` content cap, same model and effort. See §9.3.)*
- **No fetching via bash/curl.** The fix makes the *platform* `web_fetch` tool work again; it does
  not route around it with shell HTTP calls (which would lose the tool's content handling and
  bypass the sandbox's intended network surface).
- **No change to delivery.** No `POST /deliver` contract change, no delivery-side code, no
  subscriber-visible change, no microVM image rebuild.
- **No change to any environment configuration** — neither the production `self_hosted` environment
  nor the eval `cloud` environment, and no per-tool domain allowlists (change 2 is a no-op here).
- **No source-list trimming** (GitHub issue #28 stays separate), and no re-litigation of the four
  eval judge criteria.

## 3. Users & use cases

- **The daily brief reader (owner + subscribers).** Gets a brief whose stories are grounded in the
  labs' own announcement pages again, not only in whatever `web_search` surfaced. No visible change
  to the email; the change is in how the content was researched.
- **The owner, as operator.** Needs to see, on the first production runs after the cut-over, that
  source fetches actually succeed — from the session log, without reading the brief closely.
- **The owner, as decision-maker.** Needs the cost delta quantified before the flip, because
  fetched source pages mean more input tokens, and Haiku 5.5 is billed on a prompt-length-tiered
  rate card.
- **The eval harness.** Must be able to run the new prompt as a candidate against the current
  production configuration on the same news snapshot, so the quality and cost effect is
  attributable to this change alone.

## 4. Functional requirements

- **FR-1.** A **generator** shall derive a **source-URL block** from the skill's `sources.md`
  (`deploy/managed-agent/skills/daily-ai-brief/sources.md`), deterministically: the same
  `sources.md` always produces the same block, byte for byte.
- **FR-2.** The block shall be **appended to the scheduled deployment's `initial_prompt`**
  (`deploy/managed-agent/deployment.json`) so that every listed URL is present **in the initial user
  message**, before any research step runs.
- **FR-3.** The **same block** shall be appended to the task prompt of a **new eval candidate** in
  `deploy/candidates/`, which shall be identical to the current production configuration
  (`haiku-5-5-hardened`) in **every other respect** — model, effort, skill version, structure and
  prompt wording — so a comparison isolates this change.
- **FR-4.** The block shall **instruct the agent** that: these URLs are fetchable because they
  appear in this message; fetching a feed or index page makes the **article links inside it**
  fetchable too (they then count as an earlier `web_fetch` result); a URL rejected with
  `url_not_in_prior_context` shall **not be retried**; and `web_search` is the fallback for anything
  not fetchable.
- **FR-5.** A **drift test** shall fail if the block embedded in **either** prompt is not
  byte-identical to the generator's current output for `sources.md` — so editing `sources.md`
  without regenerating the prompts is a loud test failure, not a silent degradation.
- **FR-6.** The change shall require **no new agent version**: the new scheduled deployment pins the
  **unchanged agent v3**, differing from its predecessor only in `initial_prompt`.
- **FR-7.** The cut-over shall leave **exactly one active scheduled deployment**, with the schedule
  (`7 6 * * 1-5`, Europe/Berlin), environment and agent version unchanged, and shall **archive** the
  previous deployment — recording it in `deployment.json`'s `_deployment_history` with the rollback
  recipe, per the established convention.
- **FR-8.** Per-run **fetch observability** shall be derivable from the session event log: the
  number of successful `web_fetch` calls and the number of `url_not_in_prior_context` rejections.
  No new logging infrastructure is required if the event log already carries this (it does — §1 was
  measured from it).
- **FR-9.** The matched eval shall report **cost per run both ways**: the harness's own figure and
  the Platform's own `usage.list_cost`. If the increase is material, **`web_fetch`'s
  `max_content_tokens` setting is the named mitigation**, and applying it is an **owner-visible
  decision**, not an implementation detail.

## 5. Acceptance criteria

- **AC-1 (generator parity / drift).** *Given* the current `sources.md`, *when* the test suite runs,
  *then* the URL block in **both** the deployment `initial_prompt` and the eval candidate's task
  prompt is **byte-identical** to the generator's output, and the test **fails** if `sources.md` is
  edited without regenerating both. *(FR-1, FR-2, FR-3, FR-5)*
- **AC-2 (block content).** *Given* the generated block, *when* it is inspected, *then* it contains
  the `sources.md` URLs and states all four rules of FR-4 (URLs are fetchable here; feed/index
  pages unlock their article links; never retry a `url_not_in_prior_context` rejection; fall back to
  `web_search`). *(FR-4)*
- **AC-3 (matched eval — fetch restored).** *Given* a matched batch on the **cloud** environment —
  the current production config `haiku-5-5-hardened` vs. the new candidate, **same news snapshot**,
  **3 repetitions each**, judged on **all four** criteria — *when* the run records are read, *then*
  **every** repetition of the new candidate shows **successful `web_fetch` calls of URLs listed in
  the block**, and **no `url_not_in_prior_context` rejection for a URL that is in the block**.
  *(FR-2, FR-3, FR-4, FR-8)*
- **AC-4 (matched eval — quality not worse).** *Given* the same batch, *when* the four judge scores
  are compared, *then* the new candidate is **not worse beyond noise** (about half a point, the
  batch's own established noise floor at n=3) on any criterion. *(FR-3)*
- **AC-5 (matched eval — cost explained).** *Given* the same batch, *when* cost is reported, *then*
  **both** the harness figure and the Platform's own `list_cost` are stated per candidate, and any
  increase is **explained** (more fetched content ⇒ more input tokens); if the increase is material,
  the `max_content_tokens` mitigation is put to the owner as an explicit decision before the
  cut-over. *(FR-9)*
- **AC-6 (production no-delivery validation).** *Given* the new prompt on the **production
  self-hosted** environment with the **delivery step removed**, *when* one full run completes,
  *then* the session log shows **successful fetches of listed source URLs**, all expected outputs
  are written, and **nothing is delivered** (no email, no Polly, no fan-out, no archival — the
  delivery side sees at most the read-only recent-briefs call). *(FR-2, FR-8)*
- **AC-7 (cut-over).** *Given* the validated prompt, *when* the cut-over is done, *then* a **new
  scheduled deployment** exists pinned to the **unchanged agent v3** carrying the new
  `initial_prompt`; the previous deployment is **archived**; **exactly one** active scheduled
  deployment remains; and its **next run time is unchanged** (next weekday, 06:07 Europe/Berlin).
  *(FR-6, FR-7)*
- **AC-8 (rollback).** *Given* the live deployment, *when* rollback is required, *then* it is
  achieved by **recreating the previous deployment** — its exact `initial_prompt` is recoverable
  from this file's git history — and archiving the new one, with **no agent, skill, image or
  delivery change** needed. *(FR-6, FR-7)*
- **AC-9 (scope containment).** *Given* the merged change, *when* the diff is inspected, *then*
  `sources.md` and the rest of the skill, the agent definition, the environment configurations, and
  everything under `deploy/delivery/` are **unmodified**. *(§2 non-goals)*
- **AC-10 (first production runs).** *Given* the first **three** production runs after the
  cut-over, *when* each session log is checked, *then* each shows a **non-zero successful
  `web_fetch` count** of listed source URLs and **no block-URL `url_not_in_prior_context`
  rejections**, and each brief is delivered as usual (owner copy + subscriber fan-out, 0 failures).
  *(FR-8)*

## 6. Constraints & dependencies

- **Platform behaviour is the constraint.** `web_fetch`'s prior-context rule is Anthropic's, not
  configurable; the only levers are *where the URL appears* and *fetch-then-follow*. Built-in
  file/shell tool results demonstrably do **not** satisfy it (§1) — the fix must use the **user
  message**.
- **Deployments are immutable.** A prompt change means POST a new deployment + archive the old one
  (PATCH/PUT return 405) — the mechanism already documented in `deployment.json` and
  `deploy/managed-agent/README.md`.
- **Agent v3 is immutable and stays.** v3 = `{id: claude-haiku-5-5, effort: high}`; v2 (Haiku 4.5)
  and v1 (Sonnet 5) remain available for the model-level rollback that this change does not use.
- **One active scheduled deployment, always.** Verify the active set before and after the flip
  (`upcoming_runs_at`), as every previous cut-over in `_deployment_history` did.
- **Cloud eval caveat (unchanged).** The `cloud` environment blocks four curated source domains
  (The Verge, Ars Technica, Reuters, Reddit) that self-hosted production reaches — so AC-3 is
  evaluated on the fetchable subset, and AC-6 on production is what proves the full list.
- **Eval discipline.** Both batch arms pin the **same skill version** and launch in the same second
  on the same news snapshot (`run_matched_batch.sh`); n=3 keeps differences under ~0.5 points inside
  noise. Evals are content-only: no TTS, no subscriber fan-out, ever.
- **Depends on:** `deploy/managed-agent/deployment.json` (the prompt), the skill's `sources.md` (read
  only), `deploy/candidates/` + `sync.py` (the new candidate's `agent_id`), and
  `deploy/eval-harness/` (the matched batch and judges). The harness does not yet store the
  Platform's `list_cost` — AC-5 may be satisfied by reading it from the Platform alongside the run
  record (the already-tracked harness follow-up from 2026-10-11).
- **AWS account `740353583786`, us-east-1** — confirm the active account before any deploy step; no
  AWS resource change is expected from this PRD.

## 7. Risks & open questions

- **R1 — Cost increase from fetched content.** Fetching real pages adds input tokens on every run.
  Measured by AC-5 before the flip; mitigated by `web_fetch`'s `max_content_tokens` if material
  (owner decision). *(Likelihood: high that cost moves; severity: low-moderate, bounded and
  measured.)*
- **R2 — Haiku 5.5's long-prompt rate card.** Haiku 5.5 is priced per request by prompt length
  ($0.10/$0.50 per MTok up to 100,000 prompt tokens, $0.50/$2.50 above). In the 2026-10-11 batch
  70–80% of its requests already exceeded 100,000 prompt tokens; more fetched context pushes more
  requests over the line, so the cost effect can be **super-linear** in added content. This is why
  AC-5 requires the Platform's own `list_cost`, which is the figure that ran $0.04–0.08/run above
  the harness's for Haiku 5.5.
- **R3 — The block itself adds prompt tokens.** Roughly **1–2K tokens** on every run, every run,
  forever. Small in absolute terms, but it is paid even on runs that fetch nothing.
- **R4 — Partial restoration.** Fetchability is necessary, not sufficient: the agent still has to
  *choose* to fetch. AC-3 and AC-10 assert actual successful fetch counts rather than the mere
  presence of the block, precisely so "the prompt shipped" is not mistaken for "fetching works".
- **R5 — Platform rules change again.** The same email changed the contract twice in one message;
  this fix follows Anthropic's own recommended pattern, but the rule could tighten further. FR-8's
  fetch/rejection counts are the early-warning signal — this failure mode went undetected from
  2026-10-08 to 2026-10-16 because nobody was watching them.
- **R6 — Silent drift on a source-list edit.** A future `sources.md` change that skips
  regeneration would leave the prompts stale. Mitigated by FR-5/AC-1 (a failing test), which is the
  only reason `sources.md` can remain the single source of truth.
- **R7 — A safety-classifier refusal ends an unattended run (added 2026-10-17).** Restoring
  full-text fetching exposed a Haiku 5.5 failure mode: the classifier can decline a request after a
  fetched page is in context, the turn ends with no output, and nothing is sent. Measured: 3 of 3
  runs that fetched one particular page were declined, and 0 of 10 that did not fetch it were. A
  prompt rule reduces the exposure (§9.3 #3); it does not remove it, and no automatic alert or re-run
  exists yet (§9.5).
- **[Resolved — ADR-0018 D2, PM recommendation adopted] Where the generator lives and when it runs.** Options: a check-only
  test plus a manual regenerate step, or generation wired into the deployment-publish path. PM
  recommendation: **check-only test + explicit regenerate command** — it keeps `deployment.json`
  reviewable as the literal payload that was POSTed, which every prior cut-over relied on.
- **[Resolved — ADR-0018 D4, PM recommendation adopted] Block shape.** Flat URL list vs. preserving `sources.md`'s tier
  structure and feed-vs-page distinction. PM recommendation: **preserve enough structure to express
  "fetch the feed/index first"**, since that is what unlocks article links (FR-4).

## 8. Rollout & metrics

**In this order:**

1. **Build.** Write the generator, regenerate the block into `deployment.json`'s `initial_prompt`
   and into the new candidate's task prompt, add the drift test (AC-1, AC-2). No deploy.
2. **Sync the eval candidate.** Mint its `agent_id` via `deploy/candidates/sync.py`; it is an eval
   resource only and is never scheduled.
3. **Matched eval on cloud.** `haiku-5-5-hardened` vs. the new candidate, same news snapshot, 3
   repetitions each, all four judges (AC-3, AC-4, AC-5). Report fetch counts, scores, and cost both
   ways. **Gate:** if cost moved materially, put `max_content_tokens` to the owner before step 4.
4. **No-delivery validation on production self-hosted.** One full run with the delivery step
   removed; confirm real source fetches and that nothing was delivered (AC-6).
5. **Cut-over.** POST the new scheduled deployment (agent v3, new prompt, unchanged
   environment/schedule), confirm it active, archive the old one, confirm exactly one active
   deployment and an unchanged next run time; record the archive note + rollback recipe in
   `_deployment_history` (AC-7, AC-8).
6. **Watch the first three production runs** (AC-10).

**Rollback.** Recreate the previous deployment from this file's git history and archive the new one.
Nothing else moves — the agent, the skill, the microVM image, and the delivery boundary are all
unchanged by this PRD.

**Success metrics.**
- **Primary:** successful `web_fetch` count per production run back **above zero and into the
  historical 2–11 band**, with **zero** `url_not_in_prior_context` rejections for block URLs —
  read from the session event log on each of the first three runs, then spot-checked weekly for two
  weeks.
- **Secondary:** `web_search` count per run relaxes from the current 9–19 back toward the historical
  0–7 range (search as the fallback, not the whole research method).
- **Quality:** the matched eval's four judge scores are not worse beyond noise (AC-4).
- **Cost:** per-run cost (Platform `list_cost`) stays within the increase the owner accepted at the
  step-3 gate.
- **Delivery:** unchanged — every weekday run still delivers with 0 send failures.

## 9. Outcome (2026-10-17)

Built, evaluated, and deployed the same day under the owner's full authorization. Figures come from
the eval run records under `deploy/eval-harness/runs/` and from the Platform session logs. The
decisions behind the deviations are recorded in ADR-0018's amendment.

### 9.1 Matched evaluation on `cloud`

**Batch 1: control vs. source-URL block.** `haiku-5-5-hardened` (no block) vs.
`haiku-5-5-source-urls` (block). Same news snapshot, 3 repetitions each, all four judges. The
harness token cost excludes web search, which bills separately at $0.01 per search.

| Arm | Platform cost / run | Harness token cost / run | Web searches / run | Successful `web_fetch` / run | of which curated | `url_not_in_prior_context` / run |
|---|---|---|---|---|---|---|
| `haiku-5-5-hardened` (control) | $0.79 | $0.57 | 16 | 3.3 | 0 | 4.7 |
| `haiku-5-5-source-urls` | $1.24 | $1.18 | 3 | 30.7 | 20.3 | 0 |

Scores are out of 5, mean ± sample stdev over n = 3.

| Arm | Content | Factual | Format | Dedup | Mean of four |
|---|---|---|---|---|---|
| `haiku-5-5-hardened` (control) | 3.3 ±0.6 | 4.3 ±0.6 | 3.7 ±0.6 | 5.0 ±0.0 | 4.08 |
| `haiku-5-5-source-urls` | 3.3 ±0.6 | 4.0 ±0.0 | 4.0 ±0.0 | 4.7 ±0.6 | 4.00 |

Fetching is restored: every repetition of the block arm had 18–24 successful fetches of listed
curated sources and no rejections. Each criterion moved by at most 0.33 points, inside the batch's
noise floor. The Platform cost rose $0.45 per run, which is the fetched content the PRD predicted
(R1, R2).

**Batch 2: cost mitigation.** `haiku-5-5-source-urls` vs. `haiku-5-5-source-urls-capped`, which adds
the agent toolset setting `web_fetch` `max_content_tokens: 8000`. Both carry the block. 3 runs each.

| Arm | Platform cost / completed run | Mean score (n = 2 completed) | Runs without a brief |
|---|---|---|---|
| `haiku-5-5-source-urls` | $1.02 | 4.00 | 1 of 3 |
| `haiku-5-5-source-urls-capped` | $0.81 | 4.25 | 1 of 3 |

The cap cut the cost per completed run by about $0.21. The score difference is within noise. The
runs without a brief are explained by R7 (§7) and the ADR-0018 amendment.

**Stress test of the final configuration.** The capped arm with both rules from §9.3 #3, six runs on
the same news day, judged on length/format and dedup only. All six completed with a brief, with zero
declined requests and zero rejected URLs. Each run made 14–36 successful fetches (13–25 curated). The
Platform cost was $0.63–1.28 per run, mean about $0.92. The page that had triggered the refusals was
seen in a feed and not fetched in any run.

### 9.2 Production validation and cut-over

- **No-delivery run** on the production `self_hosted` environment, with agent v4 and the new prompt,
  delivery step removed. Session `sesn_01TGgfwmMEG752TjtUumxkFf` finished normally, wrote all five
  outputs, made 23 successful fetches, had 0 rejected and 0 declined, and cost $0.53. The delivery
  side logged only the read-only recent-briefs call.
- **Cut-over.** Scheduled deployment `depl_01WXFPPJfy5JaQju9LUtXrcH` pins agent v4 and carries the
  prompt from `deploy/managed-agent/deployment.json`. The v3 deployment `depl_01YRoLodhcr3HuhVS3zxVuLx`
  (no block) is archived. Exactly one scheduled deployment is active. The first run with the change
  is Mon 2026-10-19 06:07 Europe/Berlin.
- **Eval marker.** The production-config marker in the eval harness now points at
  `haiku-5-5-source-urls-capped`.
- **Rollback.** Recreate the previous deployment (agent v3, prompt without the block, both in
  `deployment.json`'s git history) and archive `depl_01WXFPPJfy5JaQju9LUtXrcH`. Agent v3 still exists,
  so no agent, skill, image or delivery change is needed.

### 9.3 Deviations from the approved plan

1. **Production runs agent v4, not the unchanged v3.** The content cap is a toolset setting, so it
   needs a new agent version. ADR-0018 D8 anticipated this and named it as an owner-gated cost. FR-6,
   AC-7 and AC-9 as written assumed v3. v4 is v3 plus the cap; model and effort are unchanged.
2. **The cap was adopted without a separate owner sign-off.** Batch 1 moved the cost materially,
   which fired the D8 gate. The orchestrator exercised the cap decision under the owner's explicit
   full authorization of 2026-10-17. This PRD (AC-5) had marked it owner-visible.
3. **The block carries two rules beyond FR-4.** Both were added after batch 2 and tested in the
   follow-on runs. (a) Fetch in batches of about 8 URLs and never end a turn with a progress message
   before the final step. (b) Do not fetch the full text of pages whose main subject is biology, the
   life sciences, medicine, pathogens or biosecurity; cover those from the feed or search snippet.
   Rule (b) is the response to the refusal risk (R7).
4. **The production marker is `haiku-5-5-source-urls-capped`,** the capped arm, not the
   `haiku-5-5-source-urls` slug that ADR-0018 D6 suggested.

### 9.4 Acceptance criteria: outcome

| AC | Outcome |
|---|---|
| AC-1 generator parity and drift | **Met.** Both carriers are byte-current, and the drift suite is green after the rule additions. |
| AC-2 block content | **Met.** The block lists the `sources.md` URLs and states the four FR-4 rules, plus the two rules in §9.3 #3. |
| AC-3 fetch restored | **Met.** Each block-arm repetition had 19, 18 and 24 curated successful fetches and 0 rejections. The control had 0 curated fetches and 0, 8 and 6 rejections. |
| AC-4 quality not worse | **Met.** Deltas: content 0, factual −0.33, format +0.33, dedup −0.33. Mean 4.08 → 4.00. |
| AC-5 cost explained | **Met, with an open reconciliation.** Both figures are stated per arm (§9.1). The increase is fetched content, and the cap cut the cost in batch 2. The harness and Platform figures differ, and reconciling them is the open harness follow-up from 2026-10-11. |
| AC-6 no-delivery validation | **Met.** See §9.2. |
| AC-7 cut-over | **Met, with a deviation.** One active scheduled deployment, schedule and environment unchanged, predecessor archived, first run Mon 2026-10-19 06:07 Europe/Berlin. It pins agent v4, not v3 (§9.3 #1). |
| AC-8 rollback | **Met.** Agent v3 remains available, so rollback is a deployment recreate plus an archive. |
| AC-9 scope containment | **Met, with one exception.** The skill, `deploy/delivery/` and the environments are unchanged. The production agent gained v4 (§9.3 #1). |
| AC-10 first three production runs | **Pending**, for 2026-10-19 to 10-21. |

### 9.5 Residual risk and follow-ups

**Residual risk (R7).** A declined request still ends a run with no send, on any day the agent
fetches a page that triggers the classifier despite the rule. The rule covers only the subject areas
it names. Today the only detection is the owner noticing a missing confirmation email.

**Follow-ups**

1. **AC-10.** For each of the first three production runs, check the session log for non-zero
   curated successful fetches, no rejections of block URLs, and a normal delivery. Then spot-check
   the fetch counts weekly for two weeks (§8 metrics).
2. **Refusal alert.** Add a "no send by 06:45" alert or an automatic re-run. Not built.
3. **Harness retry.** The eval harness's session polling has no retry for transient network errors.
   Two local connection resets aborted runs on 2026-10-17.
4. **Platform cost in run records.** Store the Platform `list_cost` in the run records and reconcile
   it with the harness figure (carried from 2026-10-11).
