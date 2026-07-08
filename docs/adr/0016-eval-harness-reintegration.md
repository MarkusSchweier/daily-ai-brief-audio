# 0016. Eval-harness re-integration: a local-first, git-native harness driving the decoupled candidate mechanism (cost-optimization epic, "step A")

- Status: **Accepted — Gate 0 passed 2026-07-07; owner signed off on all recommendations.** Owner
  decisions, recorded verbatim from sign-off: **D1–D4 backbone approved as recommended** (development
  starts); **D5 = option (b), the tiny local web app** that triggers and views; **`BriefEvalStack`
  teardown = destroy + clean up fully** (including the RETAIN'd `brief-eval-records` table, secrets,
  and site bucket — executed only after the new harness's pure-Python port is validated, per the
  phased plan's step 5); **Admin API key = skip** (no actual-cost cross-check; the pinned price table
  + drift-check is the cost source). Per `docs/prd/cost-optimization-candidates.md` §2/§4 this is
  "step A" of the combined A/B/C epic (B's candidate declarations are built + synced; A is designed
  here; C runs the comparison). The owner gave direct input on D1–D4 (2026-07-07, weighted heavily
  below); the §4.1 UI *requirements* were settled and final before sign-off.
- Date: 2026-07-07 (accepted same day)
- Deciders: architect (Claude, design), owner (all five decisions + open items 1/2/4 at Gate 0;
  open item 3, Platform trace retention, remains a build-time verification)
- Supersedes/amends: **ADR-0013** (the eval-harness backbone — "build vs. adopt," Option A, custom
  AWS-native). ADR-0013 chose an AWS-native harness (`deploy/eval/`: CDK stack, DynamoDB records,
  Lambda poll, S3 artifact retrieval, static review site) because the pipeline it measured **delivered
  to S3** and the only way to read a run's output was from that bucket. ADR-0014/ADR-0015 removed that
  premise: content generation is now decoupled from AWS delivery, a candidate run produces **no** S3
  output, and its artifacts are retrieved via **Claude-Platform-only** API calls
  (`deploy/candidates/candidate_sync/trigger.py`). This ADR re-founds the harness on that new reality;
  the parts of ADR-0013 that are pure-Python and delivery-agnostic (the four judges, the record
  dataclasses, calibration) are **preserved and moved**, and the AWS-native scaffolding it built is
  **retired** (D1).

## Context

The existing harness (`deploy/eval/`, ADR-0013) cannot evaluate the candidates this epic exists to
compare. Three concrete, file-verifiable reasons:

1. **It only ever runs one hardcoded configuration.** `functions/trigger/handler.py` targets
   `PRODUCTION_AGENT_ID` / `PRODUCTION_ENVIRONMENT_ID` (`:84-85`) — the live `self_hosted` production
   agent/environment. The `candidateConfigId` it records is a **label only** (`:221`); it does not
   change what runs. There is no path to trigger `haiku-swap`, `multiagent-aggressive-haiku`, or
   `session-restructure` at all.
2. **It retrieves artifacts from S3, which a decoupled candidate does not produce.**
   `functions/poll/handler.py` reads `s3://cowork-polly-tts-…/briefs/<date>/` and resolves *which*
   run it is processing by the fragile "most recently created `briefs/<date>/` prefix" heuristic
   (`:133-140`, with its own flagged same-day-collision assumption). A `cloud` candidate run writes to
   `/workspace` in an Anthropic sandbox and archives **nothing** to S3 — so this retrieval path finds
   nothing for a candidate.
3. **Its cost miner is Sonnet-only and single-thread.** `eval_core/cost_miner.py` hardcodes
   introductory Sonnet-5 prices (`:68-71`), applies a single cache-write rate, and mines only the
   primary thread — its own docstring **explicitly defers** multi-agent per-thread attribution "until
   that epic actually needs it." **That epic is now**, and the candidate set is majority multi-agent
   with mixed models (Sonnet coordinator + Haiku sub-agents).

Meanwhile the redesign already built the mechanism a re-founded harness should stand on. `deploy/candidates/`
is a **pure-local, git-native, AWS-free** package: per-dimension candidate declarations,
`candidate_sync/sync.py` (create/update agents via the Agents API), and `candidate_sync/trigger.py`
(create a temporary Deployment → `/run` → poll the Sessions API → recover artifacts by parsing
`cat`'d file bodies out of the session **events** stream — the confirmed substitute for the refuted
Files-API assumption, ADR-0014 Decision 1). A **real multi-agent run was captured end-to-end on
2026-07-07** (`deploy/candidates/runs/multiagent-aggressive-haiku/2026-07-07-142718/`): five threads
(Sonnet coordinator + four sub-agents), each thread's own `usage` from `GET /v1/sessions/{id}/threads`,
the session-level usage cross-checking **exactly** equal to the sum of per-thread usages, all four
artifacts recovered, and a model-aware per-thread cost computed offline ($2.32 total; the Sonnet
selection thread dominating at $1.21; Haiku writing collapsing to $0.10 vs the ~$2.60 single-agent
baseline). The capture script (`scratchpad/capture_run.py`) is the proven trigger→retrieve→cost flow
this harness generalizes.

**The owner's steer (2026-07-07, verbatim intent):** *"the initial harness was built before we split
content gen and delivery strictly. This needs to be re-worked widely… If possible let's keep the
design AWS free: candidate configs, output docs, metrics… are all not big data and could be handled
in GitHub together with the code. For deeper dives the traces are stored in Claude Platform."* On cost:
*"I would try to NOT hardcode the per-token costs of the models, but try to source it from a Claude
Platform API. That prevents drift."* On the eval email: *"If difficult to achieve an e-mail send from
Claude Platform Cloud sandbox, we can keep that optional… My eval can also mostly be conducted on the
MD."* On the record schema: *"Open for all options. We do not need to make the design complex, if there
is no value add."* On the UI, the owner re-confirmed the §4.1 requirements as **final and complete** —
leaving open only *which implementation shape* delivers them (D5).

## Decision

Re-found the eval harness as a **local-first, git-native Python CLI + a thin local viewer**, driving
the existing `candidate_sync` trigger/retrieve mechanism, storing run records as small git-tracked
files, and retiring the AWS-native `deploy/eval/` stack. Five sub-decisions:

### D1 — Harness topology: local-first, git-native; retire the AWS stack (recommended)

**We will build the harness as a pure-local Python package** (proposed home: a new sibling
`deploy/eval-harness/`) that imports `candidate_sync` for trigger/retrieve, ports the delivery-agnostic
pure-Python modules out of `deploy/eval/` (the four judges, the `record.py` dataclasses, `calibration.py`),
and stores every eval run as **git-tracked files** under `runs/<candidate-slug>/<eval-run-id>/`. It makes
**no** AWS calls in its core loop: triggering and artifact retrieval are Claude-Platform-only (already
proven), and the only outbound HTTP to AWS is the read-only `GET /recent-briefs` the candidates already
use and the **optional, deferred** owner-only eval email (D3). The existing **AWS `deploy/eval/` stack
is retired** (owner-gated teardown).

- **Options weighed.** (a) **Local-first, git-native** — matches the owner's explicit direction and the
  shape of `deploy/candidates/`; the harness's entire reason for being AWS-native (S3 retrieval) is
  gone. (b) **Adapt the existing AWS stack** — re-plumb the trigger Lambda to resolve a candidate, swap
  the S3 poll for Sessions-events retrieval, keep DynamoDB/CloudFront. Rejected: it keeps a whole
  serverless stack (5 Lambdas, DynamoDB, EventBridge, CloudFront) and its deploy/secret/DNS lifecycle
  for a **single-operator** tool whose data is "not big data," directly against the owner's steer; it
  also re-introduces the same-day-collision and cost-attribution problems in a harder-to-iterate place.
  (c) **Hybrid** — local trigger/retrieve, AWS for storage/UI. Rejected: the storage is a few hundred
  KB of JSON that belongs in git next to the code; splitting it across AWS adds a moving part for no
  value-add.
- **Fate of the old `deploy/eval/` stack (it is live on AWS).** Retire it: (1) **port first** — move
  `eval_core/judges/`, `eval_core/record.py`, `eval_core/calibration.py`, and the reusable dataclasses
  into `deploy/eval-harness/` (they are pure Python, no AWS import); (2) then `cdk destroy` the
  `BriefEvalStack` (owner-gated AWS action). Its DynamoDB table (`brief-eval-records`), two secrets, and
  S3 site bucket are `RemovalPolicy.RETAIN` and survive `cdk destroy` — they hold only the old
  production-only eval records (low residual value now) and must be deleted by hand if the owner wants a
  clean teardown (commands already documented in `deploy/eval/README.md#teardown`). Also archive any
  orphaned temporary eval deployments (`GET /v1/deployments?status=active`). **Recommendation: retire
  the stack; keep the git history and the ported pure-Python modules.** This is an owner-gated AWS
  mutation — do **not** run it as part of the design.
- **Resolving the gitignored-`runs/` tension.** The spike added a blanket `runs/` ignore
  (`deploy/candidates/.gitignore`). If run records become git-tracked deliverables, replace the blanket
  ignore with a **targeted** one: commit everything under `runs/` **except** the bulky raw trace. The
  split is sharp and evidence-backed from the spike dir: `events.json` = **200 KB** (the full
  primary-thread transcript — the "trace" the owner says lives on Platform), whereas the derived,
  decision-relevant files are tiny — `cost.json` 1.5 KB, per-thread usage ~1 KB, `run-summary.json`
  0.8 KB, the four artifacts ~33 KB combined. So **commit** artifacts + per-thread usage + cost +
  scores + run metadata (~40 KB/repetition → a 3-rep run ~120 KB → dozens of runs stay a few MB:
  genuinely "not big data"); **gitignore** `runs/**/events.json` (and any other full raw API dump).
  Crucially, **the per-thread `usage` numbers are committed** so cost is reproducible from git **even if
  Platform garbage-collects the session** — see the trace-retention open item.

### D2 — Cost attribution: per-thread usage × a git-pinned, model-aware price table (recommended)

**We will compute cost from per-thread token *usage* (Claude-Platform-sourced) multiplied by a small,
git-tracked, model-aware, cache-bucket-aware price table**, with a documented drift-check. This is a
**hard requirement** of the epic (per-agent cost for multi-agent, mixed-model runs) and the spike proved
the exact data path.

**What I verified live (read-only probes, 2026-07-07) — this reshaped the recommendation:**
- **The owner's hoped-for "source per-token cost from a Platform API" is only partly reachable, and not
  for what we need.** `GET /v1/models` and `GET /v1/models/{id}` succeed with the standard key but carry
  **no pricing field** at all (only `capabilities`, `max_tokens`, `effort` levels) — there is **no
  public price-list API** to source per-token prices from. Confirmed, not assumed.
- **The Admin usage/cost-report API exists but is out of reach and wrong-grained.** `GET
  /v1/organizations/cost_report` and `/v1/organizations/usage_report/messages` both return **401
  invalid x-api-key** — they require a separate **Admin API key** (`sk-ant-admin…`); the key in this
  setup is a standard `sk-ant-api0…` key, so no Admin key is provisioned (a real provisioning
  implication — see open items). And even **with** an Admin key, those reports are **time-bucketed and
  grouped by api-key/workspace/model/day**, not per-session/per-thread — so they **cannot** deliver the
  per-agent attribution this epic requires. They are viable only as an **optional aggregate cross-check**
  if an Admin key is ever provisioned, never the primary source.
- **Per-thread usage is the right, already-proven source.** `GET /v1/sessions/{id}/threads` returns each
  thread's own `usage` — `{input_tokens, output_tokens, cache_read_input_tokens, cache_creation:
  {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}}` — and the session total equals the sum of
  threads **exactly** (verified in the spike). Model per thread comes from the candidate declaration
  (`multiagent.json` / `model.txt`) or, as a cross-check, the thread's own echoed `agent.model.id`.

**Recommended fallback hierarchy (best available → floor):**
1. **API-sourced *actual* cost (Admin cost_report)** — *not adopted*: needs an Admin key that does not
   exist, and is org/day-grained, so cannot attribute per-thread. Documented as an optional future
   aggregate reconciliation only.
2. **API-sourced *usage* × git-pinned price table — the PRIMARY.** Per-thread usage (proven) × a
   `pricing.json` whose per-model entry pins base input, output, and the three cache multipliers, with a
   documented drift-check. Model-aware and cache-bucket-aware (the spike's arithmetic reproduces exactly
   under: input ×1, output ×5/×10, **cache-write-5m ×1.25·base-input, cache-write-1h ×2·base-input,
   cache-read ×0.1·base-input**).
3. **Pure pinned constants** — today's `cost_miner.py` floor (Sonnet-only, one cache rate). Superseded by
   #2, which generalizes it.
- **Why a pinned table is unavoidable and how drift is contained.** Since no price API exists, prices
  must be pinned somewhere; the owner's anti-drift concern is met by (a) isolating them in **one small
  reviewed `pricing.json`**, each entry carrying `source_url`, `captured_on`, and **`effective_until`**;
  and (b) a `--check-pricing-drift` guard that **fails loud** when `today > effective_until` or on any
  declared model id with no price entry. The concrete, live drift example proving the point: **Sonnet 5
  is $2/M in, $10/M out only *introductorily through 2026-08-31*, then $3/$15** — an eval run in
  September silently mis-prices by 50% without the guard. (Reference prices to seed the table, confirmed
  2026-07-07 from the pricing page: Sonnet 5 $2/$10 intro→$3/$15; Haiku 4.5 $1/M in, $5/M out; cache
  write 5m ×1.25 / 1h ×2 of base input; cache read ×0.1.)
- **What survives of `cost_miner.py` vs. what is replaced.** *Survives (ported):* the
  `TokenUsage`/`PhaseCost`/`ThreadCost`/`SessionCostBreakdown` dataclass shapes (good abstractions;
  keep for continuity). *Replaced:* the Sonnet-only single-rate constants (→ `pricing.json`), the
  primary-thread-only mining and the `last-web_search` phase heuristic (→ **per-thread is the natural
  unit**; each sub-agent thread *is* its own phase, exactly as the old docstring predicted, so no
  heuristic is needed for multi-agent). *New:* handling the nested `cache_creation` 5m/1h shape (the
  threads API differs from the flat `model_usage` the old miner parsed) and per-thread model resolution.
  The optional single-thread research/writing split (for single-agent candidates like `haiku-swap`) may
  be retained if cheap, but is **not** required by the §4.1 comparison table.
- The `/v1/models` endpoint (usable with the standard key) is kept as a **lightweight validity guard** —
  confirm every declared model id resolves — but is **not** a price source.

### D3 — Optional owner-only eval email: deferred, thin, operator-side (recommended)

**We will design the whole harness to flow with *no* email, and specify the eval email as an optional,
later, operator-side hook.** The owner deprioritized it ("my eval can mostly be conducted on the MD").
When built, the hook is a local CLI flag (`--email`) that POSTs the produced **brief markdown +
listening-script** to the existing `deploy/delivery/` `POST /deliver` in **owner-only mode**
(`enable_subscriber_fanout` = false), run **from the operator's machine** (which can read the delivery
bearer from Secrets Manager / env), **never from the cloud sandbox** — which sidesteps the
bearer-distribution-to-the-sandbox problem entirely. **Invariant (enforced now, in the design): an eval
run NEVER fans out to subscribers** — guaranteed by construction (a candidate run has no delivery path;
the optional email is a separate, explicit, owner-only operator action with fan-out off).
- **No audio — an older note is explicitly superseded.** An archived early draft of this feature said the
  eval email would be "(incl. audio)." That is **superseded** by two later owner decisions — no TTS in
  evals (owner-confirmed 2026-07-06, reaffirmed 2026-07-07) and email optional/deferred (2026-07-07) — and
  is recorded here so no future maintainer resurrects it. The eval email, if ever built, is **HTML-only,
  no Polly**.
- **One contract caveat to flag, not build now:** `POST /deliver` currently always synthesizes audio
  (Polly). Honoring "no TTS in evals" for the owner copy needs a `synthesize_audio: false` toggle on the
  `POST /deliver` contract — a small, later contract change scoped to the email hook. Deferred with D3;
  **do not** build a secret-distribution mechanism now.

### D4 — Record schema: a per-eval-run directory of small JSON/MD files (recommended)

**We will store each eval run as a boring, diffable directory of small files** (no DynamoDB, no schema
server), reusing the ported `EvalRecord` / `CandidateAggregate` dataclasses as the in-memory shape and
serializing them to disk. Concrete layout under `deploy/eval-harness/runs/<candidate-slug>/<eval-run-id>/`:

```
eval-run.json          # run-level: name; candidate identity (slug + agent_id + git ref of the
                       #   declaration at trigger time); composition (single|multi); model(s) +
                       #   thinking/effort params; repetitions (N); criteria subset judged;
                       #   state (configured|running|completed|failed); email_sent (bool);
                       #   is_production_config (bool); created_at
repetitions/
  01/
    artifacts/         # AI Brief - <date>.md, listening-script.txt, candidates.json, source-usage.json
    threads-usage.json # per-thread {role, agent_id, model, usage{...}} — small; the COST GROUND TRUTH
    cost.json          # per-thread + total, model-aware (from threads-usage.json × pricing.json)
    scores.json        # per-criterion {score, rationale, evidence, insufficient_data}
    run-meta.json      # deployment_id, session_id, thread_count, final_status, timestamp
    events.json        # GITIGNORED — the full trace (Platform / local-only)
  02/ …
summary.json           # aggregate across repetitions: per-criterion mean/stdev/min/max, mean cost;
                       #   the human-eval field
human-eval.md          # optional free-form owner assessment (the "human eval" column source)
```

- **Maps every PRD §4.1 field** with no ceremony: eval-run name, candidate identity (slug + agent_id +
  **git ref** — recovered via `git rev-parse` for the declaration dir at trigger time, satisfying
  FR-12-style "which git state produced this"), repetitions (a run = N repetition subdirs), criteria
  subset (`eval-run.json.criteria`), the four run **states**, cost, human-eval (`human-eval.md` +
  `summary.json`), and **production-config marking** (`is_production_config`, true for the
  `production-baseline` candidate — identified by slug / a `candidate.json` flag).
- **Options weighed.** (a) **Per-run directory of small files** — git-native, diffable, self-describing,
  zero infra. (b) **One append-only JSONL / a single `records.json`** — simpler to enumerate but merge-
  conflict-prone and bad at holding per-repetition artifacts. (c) **Keep DynamoDB** — rejected with D1.
  **Recommend (a).** The `SCHEMA_VERSION` field in the ported `record.py` is retained so a later field
  add stays additive.
- Candidate identity deliberately carries **all three** of {slug, agent_id, git ref} because they answer
  different questions: slug = human name, `agent_id` = what actually ran on Platform, git ref = the exact
  declaration bytes (recoverable via `git show <ref>:<path>` with no repo rollback, per ADR-0014's
  git-native principle).

### D5 — UI: requirements SETTLED; only the implementation shape needs an owner pick. Recommend a tiny local web app; static viewer as the simpler fallback

> **The §4.1 UI requirements are final and complete** (re-confirmed verbatim by the owner) and are **not**
> relitigated here — conducting runs (select candidate *or* production; single- *or* multi-agent; a run
> name; repetitions; email toggle; criteria subset; trigger; the four states
> configured/running/completed/failed), the assessment table (name / model / thinking params /
> agent-vs-multi / repetitions / production-config marking / one column per criterion, blank where
> untested / cost / human eval), and the deep dive (per-repetition exploration; candidate config incl.
> main + sub-agent prompts; MD/HTML render of the brief). **The only open D5 question is which
> implementation shape delivers those settled requirements.** Three options, biased to simplicity and
> consistent with D1's local-first direction:

- **(a) Static local page + CLI trigger** — a single HTML/JS page (reusing `deploy/eval/site/app.js`'s
  existing compare-table + detail-view logic almost verbatim, re-pointed from the Lambda API to the
  git-tracked JSON files) opened via `file://` or a one-line `python3 -m http.server`. Triggering runs
  stays a **CLI** command. **Simplest; no server, no framework, no credentials at rest.** Gap vs §4.1:
  it *views* and *compares* but does not *trigger* from the UI.
- **(b) Tiny local web app (Flask/FastAPI)** that serves the same views **and** can **trigger** a run
  (kicking it off as a background subprocess so the ~14-minute multi-agent long-poll doesn't block the
  request), then reads results from the same git-tracked files. Satisfies **every** §4.1 bullet
  (define/trigger/assess/deep-dive) in one small local process. Consequence: the Anthropic key stays in
  the operator's env (acceptable — it already does for `sync.py`/`trigger.py`); one running local
  process instead of a static file.
- **(c) Extend the existing Lambda-hosted review UI** — **rejected**: directly conflicts with the
  AWS-free D1 direction.
- **Recommendation: (b) the tiny local web app**, because §4.1 explicitly lists "Trigger the run" as a
  UI capability and (b) is the smallest thing that covers all of §4.1 while staying local/boring — with
  **(a) as the documented simpler fallback** if the owner is happy triggering from the CLI. The §4.1
  requirements are settled; **only this implementation-shape pick (a vs. b) is owner-gated** and is the
  single thing needed to unblock the UI slice.
- Either way the UI renders: the **one-page comparison table** (name, model, thinking params, agent vs
  multi-agent, repetitions, one column per criterion — blank where a run didn't test it — cost, human
  eval, production-config marker) and the **deep-dive page** (per-repetition drill-down, full candidate
  config incl. main + sub-agent prompts read from the declaration at the recorded git ref, and the
  rendered brief MD). Judge rationale/evidence stays rendered via `textContent`/`createElement` (the
  stored-XSS discipline `app.js` already follows — reader/web-influenced text must never hit `innerHTML`).

### Cross-cutting: judges, priors, and calibration

- **The four judges are reused unchanged** (pure Messages-API Haiku calls) — only their **input source**
  swaps from S3 reads to the artifacts recovered from the session event stream
  (`fetch_catted_file_contents`). Judge cost (small Haiku calls) is reported **separately** from pipeline
  cost, as PRD §7 already requires.
- **The dedup judge's "prior briefs" input** comes from the same `GET /recent-briefs` route the
  candidates already fetch at run time (the harness can read it directly, read-only, with the signing key
  it already holds for trigger-time placeholder substitution) — no S3.
- **Calibration (reader-feedback join) is de-scoped from the core loop.** It is the one legacy feature
  that reads an AWS resource (the `brief-feedback` DynamoDB table). Keep it as an **optional, separately
  invoked** local script the operator runs with read-only creds when wanted — it is not part of the
  git-native eval loop and is not in §4.1's UI requirements. (Not deleted; parked.)

## Alternatives considered

- **Adapt the AWS harness in place (D1 option b).** Rejected — keeps a serverless stack + its
  deploy/secret/DNS lifecycle for a single-operator, small-data tool, against the owner's explicit
  AWS-free steer, and re-solves cost/collision problems in a harder-to-iterate place.
- **Source actual cost from the Admin cost-report API (D2 option 1).** Rejected as primary — requires an
  unprovisioned Admin key **and** is org/day-grained, so it cannot attribute per-thread; retained only as
  an optional aggregate cross-check.
- **Source per-token prices from a Platform API (the owner's first preference).** Not possible — probed
  live: `/v1/models` carries no pricing and there is no price-list endpoint. The git-pinned table +
  drift-check is the honest realization of the owner's anti-drift intent given that constraint.
- **Build the eval email now (D3).** Rejected/deferred — the owner deprioritized it and it drags in a
  `POST /deliver` audio-toggle contract change and bearer handling; the harness flows without it.
- **One flat records file / keep DynamoDB (D4).** Rejected — merge-conflict-prone / infra for no
  value-add; the per-run directory is git-native and diffable.

## Consequences

Positive:
- The harness can finally **run and judge any candidate** (single- or multi-agent), attributing cost
  **per agent** with model-aware, cache-bucket-aware pricing — the capability the whole cost-optimization
  epic depends on, proven feasible by the spike.
- **AWS-free core**, git-native records living beside the code — matches the owner's direction; a run's
  inputs, artifacts, cost, and scores are diffable and reproducible from git, and cost survives Platform
  session GC because per-thread usage is committed.
- **One retrieval path** (Sessions-events) instead of the S3-poll + most-recent-prefix heuristic;
  the whole same-day-collision assumption class disappears.
- **Less standing infrastructure**: retiring `BriefEvalStack` removes 5 Lambdas, a DynamoDB table, an
  EventBridge rule, and a CloudFront site from the account.

Negative / follow-ups:
- **Prices are pinned, not live** (no API exists) — a permanent small maintenance duty; contained by
  `pricing.json` + the `--check-pricing-drift` fail-loud guard and the concrete 2026-08-31 Sonnet
  example. Must be re-verified before the September comparison run if it slips.
- **Retiring the AWS stack is an owner-gated AWS mutation** with RETAIN'd resources to clean up by hand;
  the ported pure-Python modules must land **before** teardown so nothing reusable is lost.
- **D5's implementation shape is unresolved** — the §4.1 requirements are settled, but the UI slice
  cannot start until the owner picks shape (a) or (b).
- **Trace retention is unverified** — if Platform bounds session/event retention, `events.json` deep
  dives expire; mitigated by committing the small derived data, but flagged below.
- **A new local package to maintain** (`deploy/eval-harness/`) alongside `deploy/candidates/`, sharing
  `candidate_sync` — a light coupling to keep in mind.

## Decision summary

| # | Decision | Recommendation | Status |
|---|---|---|---|
| D1 | Harness topology | Local-first, git-native package (`deploy/eval-harness/`) reusing `candidate_sync`; **retire** the AWS `deploy/eval/` stack; commit run records, gitignore only `events.json` | Recommended; teardown owner-gated |
| D2 | Cost attribution | Per-thread **usage** (Platform API) × git-pinned model-aware, cache-bucket-aware `pricing.json` + drift-check. No price API exists; Admin cost API unreachable & wrong-grained | Recommended (hard req.) |
| D3 | Eval email | Deferred, optional, operator-side `POST /deliver` owner-only hook; harness flows without it; invariant "never fans out" holds by construction | Recommended (deferred) |
| D4 | Record schema | Per-eval-run directory of small JSON/MD files; reuse `EvalRecord`/`CandidateAggregate` dataclasses; identity = slug + agent_id + git ref | Recommended |
| D5 | UI | Tiny local web app (Flask) that triggers + views; static viewer + CLI as simpler fallback | Requirements SETTLED; implementation shape (a vs. b) owner-gated |

## Open items for the owner (Gate 0)

1. **D5 — the UI implementation shape** (requirements are already settled). Pick: **(b)** a tiny local
   web app that can trigger *and* view (recommended, matches §4.1's "trigger from UI"), or **(a)** a static
   viewer + CLI trigger (simpler). This blocks the UI slice only; D1–D4 can proceed once approved.
2. **Admin-API-key provisioning.** The account has no Admin API key (probe returned 401). This does **not**
   block D2 (usage×table is the primary), but if the owner wants an **actual-cost aggregate cross-check**,
   a `sk-ant-admin…` key must be provisioned out-of-band and stored like every other secret. Provision, or
   explicitly skip?
3. **Platform trace retention.** The owner's model is "traces live on Claude Platform." Retention of
   `GET /v1/sessions/{id}/events` / `/threads` is **unverified** — if bounded (e.g. 30/90 days), the
   `events.json` deep-dive and any *recompute* of cost from raw usage expire with it. Mitigation is
   already in D1 (commit the small derived usage + cost so the numbers survive), but: verify retention,
   and if bounded, confirm whether an **optional local/git archive of the full trace** is wanted for
   candidates the owner intends to revisit.
4. **Retiring `BriefEvalStack`.** Confirm the owner wants the old AWS eval stack torn down (after the
   pure-Python modules are ported), including whether to hard-delete the RETAIN'd `brief-eval-records`
   table / secrets / site bucket or leave them dormant.

## Phased implementation plan (for the developer, after Gate 0)

Suggested branch: **`feat/eval-harness-reintegration`** (off the current `feat/cost-optimization-candidates`;
reconcile with the production-cut-over context per the PRD §6 branch-topology note).

1. **Scaffold `deploy/eval-harness/` + port pure-Python modules.** Create the package; move
   `eval_core/judges/`, `record.py`, `calibration.py` (and the reusable dataclass shapes) out of
   `deploy/eval/` unchanged; wire it to import `candidate_sync`. Bring their existing tests along.
2. **Cost model (D2).** Add `pricing.json` (Sonnet 5 + Haiku 4.5, cache multipliers, `effective_until`,
   `source_url`) and a `cost.py` that consumes the threads-API per-thread `usage` × the table,
   model-aware, cache-bucket-aware; add `--check-pricing-drift`; validate against the spike's
   `cost.json` numbers as a golden fixture (they must reproduce). Keep the `SessionCostBreakdown`
   dataclass names.
3. **Trigger + retrieve + record (D4).** A `run` CLI that: resolves a named candidate
   (`candidate_sync.loader`) → `candidate_sync.trigger.run_candidate` (N repetitions) → recovers artifacts
   from the event stream → fetches per-thread usage → runs the four judges → writes the per-run directory
   (incl. git ref via `git rev-parse`); set/track the four run states; targeted `.gitignore`
   (`runs/**/events.json`) instead of the blanket ignore.
4. **UI (D5 — only after the owner picks).** Build (a) or (b); reuse `app.js`'s compare-table + detail
   render, re-pointed at the git-tracked files; keep the `textContent` XSS discipline.
5. **Retire the AWS stack (owner-gated) — EXECUTED 2026-07-08** (owner-confirmed; the human ran the
   gated destructive commands per the `guard-destructive` hook). `BriefEvalStack` deleted (45
   resources incl. the CloudFront distribution, HTTP API, 5 Lambdas, EventBridge poll rule); the
   RETAIN'd `brief-eval-records` table (its 3 v1 records first exported to
   `docs/notes/brief-eval-records-export-2026-07-08.json`), both eval secrets
   (`eval-review-bearer-secret`, `eval-anthropic-api-key` — stored copies only; no key revoked),
   and the site bucket deleted. Zero orphaned Platform deployments found at teardown. The
   `deploy/eval/` directory was removed from the tree in the same change (code remains in git
   history; the many `deploy/eval/` mentions across ADRs/PRDs/code comments are intentional
   historical provenance notes, deliberately not rewritten).
6. **Validate on real candidates (feeds epic step C).** Run `production-baseline` and
   `multiagent-aggressive-haiku` through the harness for real; confirm per-agent cost matches the spike,
   the four judges score the retrieved artifacts, and the comparison table renders both — then hand off
   to step C (run all candidates, compare, decide).

## Verification note

The trigger/retrieve mechanics and per-thread usage shape were confirmed live end-to-end by the
2026-07-07 spike (`deploy/candidates/runs/multiagent-aggressive-haiku/2026-07-07-142718/`), and the
D2 pricing/cost-API facts were confirmed by read-only probes on 2026-07-07 (models endpoint carries no
price; Admin cost/usage endpoints 401 without an Admin key and are org/day-grained). The developer must
still, at build time: reproduce the spike `cost.json` exactly from `pricing.json` (golden test); confirm
the `git rev-parse` declaration-ref capture round-trips via `git show <ref>:<path>` with no rollback; and
verify Platform session/event retention (open item 3) before relying on `events.json` for any historical
deep dive.

## Amendment: Judge methodology v2 (2026-07-07, owner-directed)

Same-day amendment, after the harness above was built and run for real against three live
candidates (`production-baseline`, `multiagent-aggressive-haiku`, `haiku-swap`). The four
judges' MECHANICS (D1's "reuse `eval_core/judges/` unchanged" cross-cutting note) are
superseded for three of the four criteria by this amendment; the harness's topology (D1–D5)
is unaffected.

### What the real runs found

Two live-run findings, both visible in committed `runs/*/*/repetitions/01/scores.json`
records, motivated this rework:

1. **Knowledge-cutoff bias in `factual_accuracy`.** `runs/production-baseline/2026-07-07-
   174129-cdafb6-harness-validation-baseline/repetitions/01/scores.json` scored a real,
   correctly-dated production brief 2/5, with the rationale citing "The brief is dated July
   7, 2026 — a future date nearly two years from now" and unfamiliar product names as
   evidence of fabrication. The v1 judge (ADR-0016 Phase 1, ported unchanged from
   `deploy/eval/`) judged PLAUSIBILITY against its own training data, not accuracy against
   the brief's own sources — "I don't recognize this" was being treated as evidence of
   fabrication, when the brief was simply reporting events after the judge's training
   cutoff.
2. **Same-day dedup contamination.** `runs/multiagent-aggressive-haiku/2026-07-07-174852-
   aecc7c-harness-validation-multiagent/repetitions/01/scores.json`'s `dedup` entry was
   scored against priors that included the SAME DAY's production brief. `GET
   /recent-briefs` filters against the delivery Lambda's own wall-clock "today" at REQUEST
   time (`_today_local_date()`), not the date of the specific brief under evaluation — an
   eval run's own "today" and the delivery endpoint's "today" are not guaranteed to agree,
   and nothing in the v1 harness checked.

### The three judge changes (owner spec, 2026-07-07)

- **`factual_accuracy` — full rework.** Now ACTUALLY VALIDATES the brief's claims via the
  judge's OWN live research — server-side `web_search` (`web_search_20250305`, `max_uses:
  8`) and `web_fetch` (`web_fetch_20250910`, `max_uses: 8`) tools — instead of judging
  plausibility from training-data familiarity. Given this repo's curated `sources.md`
  (`deploy/managed-agent/skills/daily-ai-brief/sources.md`, read fresh by `run.py` and
  passed into the prompt) for context on where the brief's own sourcing should trace back
  to. The system prompt explicitly states the brief may legitimately postdate the judge's
  knowledge cutoff and that unfamiliarity is NOT evidence of fabrication — only live
  research is. Extracts a stated focus set (headlines, numbers, dates, dollar amounts,
  benchmark scores, direct quotes, named products/models) per section, then verifies each
  claim, emitting a structured `findings` array:
  `{claim, verdict: confirmed|contradicted|unverifiable, source_checked, note}`, with any
  deviation between the brief's version and the judge's research SPECIFICALLY documented.
- **`content_selection` — targeted upgrade.** Its proven `candidates.json`-vs-brief contrast
  is unchanged. Adds the same `web_search`/`web_fetch` tools (`max_uses: 5` each): whenever
  the judge believes a story should have been featured, or a featured story shouldn't have
  made it, it checks the sources/internet before committing to that view. A structured
  `selection_disagreements` array (`{story, judge_view, rationale}`) documents any case
  where the judge, after checking, would have selected differently.
- **`dedup` — feed fix (harness-side, not the judge) + richer assessment.** The feed fix
  lives in `harness/dedup_priors.py`, not the judge and not the delivery endpoint (which
  stays a thin, stateless, wall-clock read used by production candidates too, and gains no
  new parameter): `fetch_recent_prior_briefs(brief_date=...)` over-fetches (`count + 2`)
  from `GET /recent-briefs`, then locally drops any entry whose own date is the SAME AS OR
  AFTER the eval brief's own date (parsed by `run.py` from the brief's artifact filename,
  `AI Brief - YYYY-MM-DD.md`, via the new `_extract_brief_date()`), dedupes to one entry per
  date, and caps at the requested count. Each prior's date is now told to the judge
  explicitly in the prompt. The judge documents, per potential duplication, THREE things in
  a structured `findings` array — `{story, duplicate_of_date, labelled_as_followup,
  justified, note}`: is this actually a duplicate of a specific prior date; IS it labelled
  as a follow-up in today's brief; IS that follow-up justified by substantial new data (vs.
  a bare rehash). No web tools (comparing two texts the harness already has needs no
  external verification).
- **`length_format` — UNCHANGED**, per explicit owner instruction: its prompt and approach
  were never implicated by either finding above (a length/format check needs no live
  research), so only its MODEL changes (below), nothing else.

### Per-judge model: ALL FOUR default to Opus 4.8 (owner course-correction, mid-build)

The owner's original spec (models per-judge, "accuracy: Sonnet 5... content_selection/dedup
stay Haiku unless a hard reason otherwise") was superseded, mid-build, by a stronger,
uniform direction: **all four judges default to `claude-opus-4-8`**, on the principle that a
judge must be run on a model STRONGER than what it judges, or the evaluation doesn't mean
much — including `length_format`, whose prompt is otherwise untouched. Per-judge model
config is KEPT as the mechanism (`eval_core/judges/base.JUDGE_MODELS`, one small
`{criterion: model_id}` mapping each judge module resolves its own entry from) — the
all-Opus default is the owner's call for right now, not hardcoded dogma; flipping one
judge's model back down later is a one-line change to that mapping, not a rewrite.

`pricing.json` gained a `claude-opus-4-8` entry, VERIFIED live the same day against
`platform.claude.com/docs/en/about-claude/pricing`: base input $5/MTok, output $25/MTok,
standard (non-introductory) pricing, no `effective_until`. Its cache rates ($6.25/$10/$0.50)
reproduce the SAME uniform 1.25×/2.0×/0.1×-of-base-input ratios every other model family in
the table already uses, so no per-model `cache_multipliers` override was needed. Judge cost
resolves Opus 4.8 through the SAME fail-loud pricing path every other model uses
(`cost.price_usage()` / `cost.UnknownModelPriceError`) — an unrecognized judge model still
fails loud, never silently prices as $0.

Judge cost — MEASURED LIVE (2026-07-07, the v2 accuracy judge on a real stored brief, both
smokes on the same input): **uncached, the first smoke burned $1.60** (281,543 full-price
input tokens across the 8-search server-tool loop — every iteration re-sent the whole
accumulated context, cache_read=0). **Automatic prompt caching was then enabled on every
judge call** (`run_judge()` passes the top-level `cache_control: {"type": "ephemeral"}`; the
API auto-manages breakpoints as the tool loop grows — confirmed against the prompt-caching
docs the same day): the identical re-run cost **$0.88 all-in (-45%)** — input collapsed to 10
full-price + 98,935 cache-write (1.25×) + 192,990 cache-read (0.1×) tokens, cache writes now
the dominant term (each iteration's NEW search results, written once — near the loop's cost
floor). Extrapolated all-four-judges cost: roughly **$1.40–$1.60 per repetition, all-in**,
of which up to 8 (accuracy) + 5 (content_selection) = 13 web searches at $0.01 each (web
fetch is token-cost-only, no separate per-call fee — confirmed live the same day against the
web-fetch-tool docs page). Note the pricing page's own tokenizer caveat:
Opus 4.7+ (incl. 4.8) and Sonnet 5 use a newer tokenizer that produces **~30% more tokens for
the same text** than Haiku 4.5's tokenizer — part of the cost multiple, not a `pricing.json`
concern but worth knowing when reading a `judge-cost.json` total. Web-search cost is priced
as a SEPARATE axis from token cost (`harness.cost.price_web_searches()`, `pricing.json`'s
flat `web_search.cost_per_1000_searches_usd: 10.0`), never folded into `total_cost_usd` —
`judge-cost.json` carries both `total_cost_usd` (token) and `total_search_cost_usd` (search)
plus a convenience `grand_total_cost_usd` sum.

### Plumbing consequences (additive, not a schema break)

- `run_judge()` (`eval_core/judges/base.py`) now takes an explicit `model=` and optional
  `tools=` (passed straight through to `messages.create(...)`), and parses ONLY the LAST
  `text`-type content block for its JSON verdict — a server-side-tool response can carry
  MIXED content (narration, `server_tool_use`/tool-result blocks, then a final text block),
  and joining every text block (the v1 behavior) risked an earlier narration block's own
  stray braces corrupting the outermost-brace JSON scan. `JudgeResult` gained `.model`,
  `.search_count`, `.findings`, `.selection_disagreements` — all additive; the original
  `score`/`rationale`/`evidence`/`insufficient_data`/`usage` fields are unchanged.
- `scores.json` stays additive: `findings`/`selection_disagreements` are included only when
  a judge's result actually carries one — a v1-shaped judge (or a v2 judge's degrade path
  that never called the API) simply omits the key.
- `run.py`'s `_price_judge_results()` now prices each criterion against its OWN recorded
  model (`result.model`), not one shared constant, and adds the search-cost axis;
  `_run_selected_judges()` threads through the new `sources_md`/`prior_briefs` (now
  `{date, markdown}` dicts, not bare strings) parameters.
- The Deep Dive UI (`templates/run_detail.html`) renders `findings`/`selection_disagreements`
  as plain tables, escaped exactly like every other judge-authored field (never `| safe`) —
  and the judge-cost table reads every v2 field via `.get(key, default)` so the several
  ALREADY-COMMITTED runs under `runs/` (recorded before this amendment, still in the OLD
  judge-cost.json shape) keep rendering without erroring.

### Verification (web tool schemas, 2026-07-07)

Confirmed live by fetching the current docs pages directly (`platform.claude.com/docs/en/
docs/agents-and-tools/tool-use/{web-search,web-fetch}-tool` and `.../about-claude/pricing`;
no API key needed): `web_search_20250305` and `web_fetch_20250910` ("basic" variants) are
both still current/documented (newer dated versions exist, adding dynamic-filtering/
response-inclusion controls neither judge needs); **no `anthropic-beta` header is required
for either tool** — the docs' own cURL examples send none, correcting this task's own
starting assumption that web_fetch still needed the historical `web-fetch-2025-09-10` beta
header. Web search is billed "$10 per 1,000 searches" (flat, per-call, model-independent);
web fetch is "available... at no additional cost... you only pay standard token costs." A
response's `usage.server_tool_use.web_search_requests` (confirmed via the docs' own example
response JSON) is how `base._extract_search_count()` reads a call's actual search count.
