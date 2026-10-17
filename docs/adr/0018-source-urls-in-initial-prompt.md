# 0018. Restore `web_fetch` by generating the curated source URLs into the first user message

- Status: **Accepted** — the owner approved this solution and authorized implementation and
  deployment on 2026-10-17. Not re-opened here.
- Date: 2026-10-17
- Deciders: architect (Claude), owner (Markus)
- PRD: [`docs/prd/web-fetch-source-urls.md`](../prd/web-fetch-source-urls.md)
- Touches: `deploy/managed-agent/` (the scheduled deployment's `initial_prompt`, a new generator
  module, the drift test), `deploy/candidates/` (one new eval candidate). **Read-only:** the
  `daily-ai-brief` skill, including `sources.md`.
- Unchanged by this decision: the production agent definition (v3), the microVM image, the
  `self_hosted` and `cloud` environments, `deploy/delivery/`, every subscriber-visible artifact.
- Related: ADR-0006 (the scheduled deployment is defined by `deployment.json`), ADR-0007 (the
  skill port), ADR-0008 (skill-content lockstep and the image-rebuild failure mode this decision
  deliberately avoids), ADR-0014/ADR-0016 (the candidate + eval mechanism this is validated on).

## Context

Anthropic's 2026-10-16 email ("Changes to `web_fetch` and `web_search` in Claude Managed Agents")
announced that, since **2026-10-08**, `web_fetch` retrieves only URLs that **already appeared in
the conversation** — in a user message, in a **client-side** tool result, or in an earlier
`web_search`/`web_fetch` result. A URL that exists only in the system prompt or in Claude's own
output is rejected with `url_not_in_prior_context`.

Four properties of this pipeline constrain the design. All four were verified against the repo and
the session event logs rather than assumed (evidence in the PRD §1):

1. **The pipeline is fully broken on this axis, silently.** Production runs from 2026-10-12 onward
   show **0 successful `web_fetch` calls and 3–6 rejections each**, against 2–11 successes and 0
   rejections before 2026-10-08. Sends never failed, so nothing alerted: research quietly fell back
   to `web_search` alone (9–19 searches per run, up from 0–7) and the curated **primary sources
   stopped being read at all**.
2. **Reading `sources.md` does not satisfy the rule.** The 62 URLs reach the agent today only
   because the skill tells it to open `sources.md` with the built-in file/shell tools. In the
   2026-10-16 run, **4 of the 5 rejected URLs had appeared verbatim in an earlier bash/read tool
   result** and were still rejected. The email's "client-side tool result" evidently does not cover
   built-in tool output. So the *only* lever available inside this architecture is **where the URL
   string appears**, and the user message is the one place that provably counts.
3. **The network half of the platform change is a no-op here.** Production is `self_hosted`, the
   eval environment is `cloud`/`unrestricted`, and the agent declares no per-tool domain lists;
   the platform docs state neither configuration limits the web tools. Nothing to decide.
4. **The agent cannot self-heal.** In the 2026-10-11 matched eval, Opus 5.5 worked around the
   rejections by searching first and fetching the result URLs; **Haiku 5.5 — the production model
   since 2026-10-11 — managed 0–4 fetches per run.** A prompt-level fix is required; waiting for
   the model to adapt is not an option.

Two structural facts shape *how* the fix is wired, and both were checked in code:

- **A candidate's task prompt is not part of its agent definition.**
  `candidate_sync.loader.AgentDeclaration.to_agent_body()` does not emit `task_prompt`; the prompt
  travels at run time as the session's `initial_events` user message. `_live_declaration_differs()`
  iterates the *body's* keys only, so a task-prompt-only edit triggers **no agent update and no
  version bump**. Editing a candidate prompt is therefore free; editing an agent is not.
- **The four "hardened" candidate prompts are contractually byte-identical.**
  `deploy/candidates/tests/test_model_generation_candidates.py` asserts that
  `haiku-5-5-hardened`, `sonnet-5-5-hardened` and `opus-5-5-hardened` have `task-prompt.md`,
  `system-prompt.md` and `skills.json` **byte-identical to `haiku-swap-hardened`'s** — the
  single-variable discipline the whole 2026-10-11 model-generation comparison rests on. All four
  files hash identically today. Appending anything to `haiku-5-5-hardened/task-prompt.md` in place
  would break that test *and* destroy the control arm this change needs to be measured against.

## Decision

### D1 — The curated URLs go into the first user message, generated from `sources.md`

We will append a generated **source-URL block** to the **first user message** of the run — the
scheduled deployment's `initial_prompt` in `deploy/managed-agent/deployment.json`, and the task
prompt of one new eval candidate (D6). The block lists every URL in the skill's `sources.md`
verbatim (feeds *and* site pages), states briefly why it is there, asks the agent to fetch from it
when the skill calls for a known source, and tells it not to retry a rejected URL but to fall back
per the skill. Because article links inside a fetched feed or index page arrive as an earlier
`web_fetch` result, the **feed → article chain works again** from the listed entry points.

`sources.md` **remains the single source of truth** and is never edited by this change. The block
is derived from it by a generator (D2) and the derivation is enforced by a drift test (D7).

This is the platform's own first recommended pattern, applied at the only layer that can carry it.

### D2 — The generator is a standalone module under `deploy/managed-agent/`, check-only

**Location:** `deploy/managed-agent/tools/source_url_block.py` — a new directory, deliberately
**not** `deploy/managed-agent/pipeline/`. The microVM `Dockerfile` does `COPY pipeline/
/opt/pipeline/` as a whole-directory copy, so anything placed there is baked into the image: an
authoring-time prompt tool would become runtime code in production and couple every future tweak
to an image rebuild — the exact ADR-0008 failure mode this repo has already paid for once. The
generator never runs inside the microVM, and the new directory is invisible to the image build.

**Interface** (pure functions first, CLI on top):

```python
BLOCK_BEGIN_MARKER: str
BLOCK_END_MARKER: str

def extract_source_urls(sources_md_text: str) -> list[str]: ...
def render_source_url_block(sources_md_text: str) -> str: ...
```

Both take **text, not a path**, so the test can feed fixtures and the CLI owns all file I/O.
`render_source_url_block()` is deterministic: the same input always yields the same bytes.

**CLI:** `python3 deploy/managed-agent/tools/source_url_block.py` prints the block to stdout,
reading the skill's `sources.md` by default; `--sources PATH` overrides it; `--json-string` prints
the block as a JSON-escaped string fragment for pasting into `deployment.json`. No `--write`, no
in-place editing of either prompt.

**Enforcement is check-only** (the PM's recommendation in the PRD, adopted): a human runs the CLI
and pastes; the drift test (D7) fails if the committed prompts and the generator output differ.
Generation is **not** wired into a publish step. `deployment.json` stays the literal, reviewable
payload that was POSTed — the property every previous cut-over in `_deployment_history` relied on,
and the reason a rollback is a copy-paste out of git history rather than a re-run of tooling.

### D3 — Fixed ASCII marker lines delimit the block, which is appended at the end of the prompt

The block begins and ends with exact, single-purpose marker lines, exported as module constants so
the generator and the test cannot disagree:

```
=== BEGIN GENERATED SOURCE URL LIST (from the daily-ai-brief skill's sources.md; do not edit by hand) ===
...
=== END GENERATED SOURCE URL LIST ===
```

- `render_source_url_block()` returns the markers **as part of** the block, so there is exactly one
  artifact to generate, paste and compare — no "block plus wrapper" ambiguity.
- A prompt surface must contain **exactly one** begin marker and **exactly one** end marker, in
  that order. The test extracts from the first character of the begin marker to the last character
  of the end marker and compares bytes; whatever separates the block from the prose around it is
  the prompt author's business, not the generator's.
- Markers are plain ASCII and unique enough to `grep` for across the repo.

**The block is appended at the end of the prompt**, after the final numbered step. Position is
irrelevant to the platform rule (the URL need only be *in* the message), and appending leaves every
existing word of the production and candidate prompts byte-for-byte unchanged — which is what keeps
this change a single-variable change for the eval, and keeps the diff readable.

### D4 — URL extraction rules

Stated precisely, because the drift test makes them a contract:

| Rule | Decision |
|---|---|
| What counts | Every `http://` or `https://` URL anywhere in `sources.md` (today: 62, all `https`). |
| Order | **Document order**, which is the skill's own tier order and keeps each outlet's feed URL adjacent to its site URL. |
| Duplicates | De-duplicated, **first occurrence wins** (today there are none; dedup is a guard, not a transform). |
| URL charset | Greedy match stopping at whitespace and at `<`, `>`, `"`, `'`, backtick, `)`, `]` — so a markdown `[label](url)` or an `<url>` never swallows its delimiter. |
| Trailing punctuation | A trailing run of `.,;:!?` and `·` is stripped. (`·` is `sources.md`'s own feed/site separator.) |
| Normalization | **None.** Scheme, case and trailing slash are preserved verbatim. The platform matches URL strings against prior context, so verbatim is the load-bearing property; "tidying" a URL could put a *different* string in the message than the one the agent will try to fetch. |
| `(verify)` markers | **Dropped.** They are maintainer-facing prose about an unconfirmed feed endpoint; the URL itself is listed unchanged, and the fallback rule (D5) already covers an endpoint that turns out to be wrong. `sources.md` keeps its markers. |
| Tier structure | Each `## ` heading line in `sources.md` is copied **verbatim** (minus the `## ` markup) as a group heading, with its URLs beneath it, one per line. URLs found before any heading — none today — go under the literal heading `Ungrouped`. |
| Outlet labels | **Not emitted.** URLs only. The block's job is to put URL *strings* in the message, and the skill already carries the labels. Re-emitting them would create a second copy of the exact label strings the skill's `source-usage.json` contract requires ("the name/label exactly as `sources.md` lists it"), which is a drift surface the drift test cannot see. |
| Empty result | **Fail loudly.** Zero extracted URLs raises (CLI exits non-zero); a `sources.md` restructure must never silently produce an empty block. |

Tier headings contain non-ASCII em dashes, so the block is UTF-8, not ASCII. That is fine:
`deployment.json` is written with `ensure_ascii=True` today (`—` escapes), the drift test
compares the **decoded** JSON string, and either escaping therefore passes.

### D5 — What the block says, and what it must not say

The prose preamble is short (the whole block is ~1–2K tokens, and most of it must be URLs) and
states exactly four things, per PRD FR-4/AC-2:

1. These URLs are listed here because `web_fetch` can only retrieve a URL that already appeared in
   the conversation — reading `sources.md` from disk does not count.
2. Fetching a feed or index page listed here makes the **article links inside it** fetchable too.
3. A URL rejected with `url_not_in_prior_context` must **not** be retried.
4. Anything not fetchable falls back to `web_search`.

Two constraints on the wording are deliberate:

- **It must not instruct exhaustive fetching.** 62 URLs in the message is an invitation to fetch 62
  pages, which is the cost risk (R1/R3 in the PRD) and is also *wrong* — the skill already says
  "budget the fetches… you don't need every source" and works top tiers first. The block says
  "fetch from this list when the skill calls for a known source" and leaves budgeting to the skill.
- **Rule 4 must defer to the skill's existing fallback chain, not replace it.** `SKILL.md`'s Gather
  step prescribes: feed fails → fetch **that same outlet's own site/index URL** (listed right
  beside the feed, and now also in the block) → only then `web_search`. The block's fallback
  sentence names that chain rather than jumping straight to search, so the block cannot contradict
  a skill we are explicitly not changing.

### D6 — The eval arm is a **new** candidate, not an edit to `haiku-5-5-hardened`

We will add a new single-agent candidate — suggested slug **`haiku-5-5-source-urls`** — that is
identical to `haiku-5-5-hardened` in `system-prompt.md`, `model.txt` (`claude-haiku-5-5`),
`parameters.json` (`{"effort": "high"}`), `skills.json` (the same pinned skill version) and
`agent.json`'s `tools`/`mcp_servers`, and whose `task-prompt.md` is
`haiku-5-5-hardened/task-prompt.md` **plus the appended block, byte-for-byte**. It gets its own
`agent_id` at first `sync.py` (create-once), and its `candidate.json` **must not** set
`is_production_config`.

`haiku-5-5-hardened` is **left untouched**, for two independent reasons:

- It is the **control arm**. A matched batch needs the current production configuration running
  *without* the block on the same news snapshot; editing it in place would leave nothing to compare
  against and would silently invalidate the 2026-10-11 records as a baseline.
- It would **break `test_model_generation_candidates.py`**, which asserts all four hardened
  prompts are byte-identical. That test is the single-variable discipline of the whole
  cost-optimization epic; weakening it to accommodate a prompt change would be trading a durable
  guarantee for a one-off convenience.

Per the Context section's second structural fact, a candidate task-prompt change needs **no agent
sync** and creates **no agent version** — but this candidate is *new*, so one `sync.py` run is
required to mint its `agent_id`. That is the only Platform write this decision implies before the
cut-over.

### D7 — One drift test, three rules

A single new test module, `deploy/managed-agent/tests/test_source_url_block_drift.py`, imports the
generator (the suite's `conftest.py` already establishes the `sys.path.insert` pattern) and reads
the other deploy unit's files by repo-relative path — the established cross-unit precedent of
`deploy/subscribers/tests/test_send_time_consistency.py`. It asserts:

1. **Required carriers.** `deployment.json`'s decoded `agent.initial_prompt` and the new
   candidate's `task-prompt.md` each contain exactly one block, **byte-equal** to
   `render_source_url_block(<the skill's current sources.md>)`. This is the whole point: editing
   `sources.md` without regenerating both prompts is a loud failure, not a silent degradation
   (PRD FR-5/AC-1).
2. **No stale copies.** Any other scanned prompt surface — `deploy/managed-agent/deployment*.json`
   and every `deploy/candidates/*/task-prompt.md` — that contains **either** marker must also carry
   exactly one byte-current block. A marker-driven rule rather than a fixed allow-list, so a block
   pasted somewhere new can never rot unnoticed.
3. **Hardened-candidate guard.** `haiku-swap-hardened`, `haiku-5-5-hardened`,
   `sonnet-5-5-hardened` and `opus-5-5-hardened` must **not** contain either marker — the negative
   assertion that keeps D6's control arm and the byte-identity contract intact.

Plus ordinary unit tests for the generator on fixture text (extraction order, dedup, trailing
punctuation, `(verify)` handling, markdown/angle-bracket delimiters, the empty-input failure) and
one assertion against the real `sources.md` that the extracted count is non-zero and every entry
starts with `http`.

All three rules live in the **managed-agent** suite, not split across two: the generator is
imported once, and the single-variable contract and the drift contract are read in one place.
`deploy/candidates/README.md` and the new candidate's `candidate.json` description should say where
the enforcing test lives.

### D8 — Measure the cost effect; `max_content_tokens` stays an owner decision, not a default

Successful fetches put real page content into the context, raising input tokens on every run. For
**Haiku 5.5 the effect can be super-linear**: it is billed per request by prompt length
($0.10/$0.50 per MTok up to 100,000 prompt tokens, $0.50/$2.50 above), and in the 2026-10-11 batch
**70–80% of its requests already crossed that line**. So we measure before we flip (PRD
FR-9/AC-5), reporting both the harness figure and the Platform's own `usage.list_cost`, and we do
**not** pre-apply a mitigation.

`web_fetch`'s `max_content_tokens` toolset setting is the named mitigation, and it is owner-gated
for a structural reason worth recording: it is **not** a prompt-layer change. It lives in the
agent's toolset config, so applying it to production means a **new agent version (v4)** — this
decision's "no new agent version" property (FR-6) would be spent — and applying it to the
candidates would break `test_model_generation_candidates.py`'s `tools` byte-identity assertion
unless applied to all four. A knob that costs an agent version and a candidate-discipline decision
is not an implementation detail.

### D9 — Rollout in four gates; rollback is one deployment recreate

1. **Build (no deploy).** Generator + block pasted into both prompts + drift test. Both suites
   green: `deploy/managed-agent/` and `deploy/candidates/`. Reviewer and security passes on the
   diff; the diff must show `sources.md`, the rest of the skill, the agent definition, the
   environments and `deploy/delivery/` unmodified (AC-9).
2. **Matched eval on `cloud`.** `sync.py` the new candidate to mint its `agent_id`, then
   `haiku-5-5-hardened` vs. `haiku-5-5-source-urls`, same news snapshot, 3 repetitions each, all
   four judges, same pinned skill version (AC-3/AC-4/AC-5). Read the per-run successful-fetch and
   `url_not_in_prior_context` counts out of the session event log. **Gate:** if cost moved
   materially, D8's mitigation goes to the owner *before* step 3. Known caveat, unchanged: `cloud`
   blocks four curated domains (The Verge, Ars Technica, Reuters, Reddit) that self-hosted
   production reaches, so the cloud arm proves the fetchable subset and step 3 proves the full list.
3. **No-delivery validation on the production `self_hosted` environment.** One full run of the new
   `initial_prompt` with **step 4 (the `delivery_client.py` delivery call) removed** — exactly how
   the 2026-10-11 Haiku 5.5 flip was validated (session `sesn_01BURb3ABHa7xAo6LmRRURSj`). Expect:
   successful fetches of listed URLs, all outputs written, and the delivery side seeing **only** the
   read-only recent-briefs call (AC-6). This prompt is **ephemeral** and is not committed.
   `deploy/managed-agent/deployment-validation.json` is **not** the vehicle and is not updated: it
   is the historical ADR-0015 artifact, its prompt predates the hardened steps, and it *does*
   deliver an owner copy — which AC-6 forbids. That pre-existing drift is recorded, not fixed here;
   D7's rule 2 keeps it honest if anyone ever pastes a block into it.
4. **Cut-over.** POST a new scheduled deployment pinned to the **unchanged agent v3**, differing
   from `depl_01YRoLodhcr3HuhVS3zxVuLx` **only** in `initial_prompt` — same environment, same cron
   (`7 6 * * 1-5`, Europe/Berlin), same skill. Confirm it active and its next run time unchanged,
   archive the predecessor, confirm **exactly one** active scheduled deployment, and record the
   archive note plus the rollback recipe in `deployment.json`'s `_deployment_history`, as every
   prior cut-over did (AC-7).
5. **Watch.** The first three production runs' successful-fetch and rejection counts (AC-10), then
   weekly spot checks for two weeks.

**Rollback:** create a deployment identical to the live one but carrying the **previous**
`initial_prompt` — recoverable verbatim from `deployment.json`'s git history — then archive the new
one. No agent, skill, image, IAM or delivery change is involved, in either direction (AC-8).

## Alternatives considered

- **(a) Instruct the agent to search first, then fetch the result URLs.** The email's second
  recommended pattern, and it needs no generator and no new artifact. **Rejected:** it is indirect
  and unreliable at the model tier production actually runs — Opus 5.5 managed it (14–20 fetches
  per run in the 2026-10-11 batch) while Haiku 5.5 managed 0–4. It also makes first-party source
  coverage depend on search ranking rather than on a curated list, and priming all 62 sources this
  way costs up to ~62 extra searches ≈ **$0.62/run** at $0.01 per billed search — more than the
  block's ~1–2K cached tokens, for a strictly weaker guarantee. It remains a *fallback* inside the
  block (D5 rule 4), which is the right place for it.
- **(b) Fetch via `bash`/`curl` from the self-hosted sandbox.** Technically available today (the
  self-hosted sandbox has egress) and would restore first-party reading immediately.
  **Rejected on four counts:** it bypasses `web_fetch`'s content extraction and its own limits, so
  the agent would be parsing raw HTML/XML at unbounded size; it behaves *differently* in the
  `cloud` eval environment, which would break the "eval what production runs" property the whole
  candidate mechanism exists to provide; it widens the prompt-injection surface, since shell-fetched
  bytes arrive with no tool-level handling at all; and it routes around the platform's intended path,
  so every future platform change to the web tools would pass this pipeline by. The PRD lists it as
  an explicit non-goal.
- **(c) Put the URLs in the skill, or in the agent's system prompt.** The obvious place for source
  data, and it would need no deployment change. **Rejected because it does not work:** the platform
  rule counts the system prompt as *not* prior context, and built-in file-read results demonstrably
  do not count either — 4 of 5 rejected URLs in the 2026-10-16 run had already appeared verbatim in
  a bash/read result. This is the single measured fact that forces the fix into the user message.
  Changing the skill would additionally require a Skills-API push (ADR-0008) and is out of scope.
- **(d) A custom tool that returns the source list.** Per the email, a **client-side** tool result
  *does* count as prior context, so this would work. **Rejected:** it adds a custom-tool definition,
  a round trip, and new infrastructure to put the same 62 strings in the conversation that the user
  message already carries for free — with the same token cost, plus an extra turn, plus a new thing
  to keep in sync. No benefit over D1.
- **Appending the block to `haiku-5-5-hardened` in place instead of adding a candidate (D6).** One
  fewer candidate and one fewer `agent_id`. **Rejected:** it destroys the control arm the matched
  eval needs and breaks the four-way byte-identity test that is the cost-optimization epic's
  single-variable discipline.
- **A flat URL list with no tier structure (D4).** Marginally fewer tokens and a two-line
  generator. **Rejected:** the skill works top tiers first and prefers feeds, and the PM's PRD
  explicitly asks the block to "preserve enough structure to express *fetch the feed/index first*".
  Copying the existing `##` heading lines verbatim costs ~9 lines, adds no parsing of prose, and
  introduces no new vocabulary.
- **Emitting outlet labels alongside the URLs (D4).** More readable for a human.
  **Rejected:** it creates a second copy of the exact label strings the skill's `source-usage.json`
  contract depends on, in a file the drift test checks for *generator* equality but not for
  *semantic* agreement with the skill.
- **Wiring generation into the deployment-publish path instead of a check-only test (D2).** Would
  make drift structurally impossible. **Rejected:** it turns `deployment.json` from "the literal
  payload we POSTed" into "the output of a script", and every rollback recipe in
  `_deployment_history` — including this one's — depends on the former. A failing test buys the same
  guarantee at a fraction of the blast radius.
- **Putting the generator in `deploy/managed-agent/pipeline/` (D2).** The directory where the
  pipeline's Python already lives. **Rejected:** `COPY pipeline/ /opt/pipeline/` is a
  whole-directory copy, so the module would be baked into the microVM image and a prompt-tooling
  change would imply an image rebuild — the ADR-0008 coupling this architecture has deliberately
  been moving away from.
- **Putting the block at the top of the prompt (D3).** Equivalent for the platform rule.
  **Rejected (weakly):** appending leaves the existing prompt bytes untouched, which is what makes
  the diff reviewable and the eval single-variable.

## Consequences

**Positive**

- First-party source reading is restored by the platform's own recommended mechanism, with the
  **feed → article chain working again**: the listed feed/index pages become legitimate entry
  points whose article links are then fetchable.
- **`sources.md` stays the single source of truth**, and that claim is now enforced by a test
  rather than by discipline — the only reason the duplication is acceptable at all.
- **No new agent version, no image rebuild, no skill push, no IAM change, no delivery change.** The
  entire change is one prompt string, one new candidate directory, one generator and one test; the
  cut-over is a deployment recreate, and so is the rollback.
- The eval arm is **attributable**: a new candidate differing from the live production
  configuration in exactly one appended block, run on the same news snapshot against an unmodified
  control.
- A measured early-warning signal now exists where there was none. This failure mode went
  undetected from 2026-10-08 to 2026-10-16 because nobody was reading fetch counts; AC-10 makes
  them a watched number.

**Negative / newly constrained**

- **`sources.md` is now effectively part of the deployment payload.** Any future edit to it —
  fixing a dead feed, the source-list trimming of GitHub issue #28 — now requires regenerating two
  prompts **and creating a new scheduled deployment** (deployments are immutable), on top of the
  Skills-API push it already required. This is the real price of the decision, it is recurring, and
  the drift test is what converts it from a silent-staleness risk into a loud one. Note the skill's
  own "keep `sources.md` current" instruction was never self-applying (the agent cannot edit the
  repo), so no automation is lost — but the human cost per source-list edit goes up.
- **A stale block is worse than no block.** It would put dead URLs in front of the agent with an
  instruction to fetch them. Rule 1 of D7 is the only defence; it must never be weakened to "the
  markers are present".
- **Cost rises on two axes, one of them non-linear.** ~1–2K prompt tokens every run, forever, paid
  even on runs that fetch nothing (cached, so cheap but not free); plus fetched page content,
  which for Haiku 5.5 pushes more requests over the 100,000-prompt-token rate-card threshold where
  per-token prices are 5× higher. D8 measures both before the flip; `max_content_tokens` is the
  mitigation, and spending it costs an agent version.
- **62 URLs in the message is a standing temptation to over-fetch.** D5's wording is the only
  guard, and it is prose. If fetch counts come back far above the historical 2–11 band, that is a
  cost signal to act on, not a success.
- **Fetchability is necessary, not sufficient.** The agent still has to choose to fetch; AC-3/AC-10
  assert actual counts precisely so "the prompt shipped" is never mistaken for "fetching works".
- **The platform rule may tighten again.** The same email changed the contract twice at once. This
  decision follows Anthropic's documented pattern, so it should age well, but the fetch/rejection
  counts are the tripwire, and a further change could invalidate D1 the way it invalidated reading
  `sources.md` from disk.
- **The 2026-10-11 matched-batch numbers stop being comparable to post-cut-over production.** They
  remain valid as the pre-change baseline (and `haiku-5-5-hardened` remains the live control arm),
  but any future model comparison must be re-run on prompts that carry the block.
- **One more prompt surface to keep in lockstep.** `sources.md` ↔ `deployment.json` ↔ the new
  candidate's `task-prompt.md`, with the hardened four held *out* of it by an explicit negative
  assertion. The set is small and test-enforced, but it is a set, and D7's rule 2 exists because
  someone will eventually paste the block somewhere else.

**Follow-ups this creates**

- **At cut-over:** move the eval harness's production-config marker
  (`deploy/eval-harness/harness/production_config.py`'s `PRODUCTION_CANDIDATE_SLUG`, currently
  `haiku-5-5-hardened`) to the new candidate, since it then *is* the production configuration —
  the same step the 2026-10-11 Haiku 5.5 move took (commit `3aafc42`).
- A `deploy/managed-agent/README.md` note: editing `sources.md` now requires regenerating both
  prompts (CLI command) and creating a new scheduled deployment, and the drift test is what fails
  if you forget.
- The already-tracked harness follow-up — storing the Platform's own `usage.list_cost` in run
  records — is what AC-5 leans on; until it lands, the Platform figure is read alongside the run
  record by hand.
- If D8's gate fires, a separate owner-gated decision on `max_content_tokens` (agent v4 + the
  candidate `tools` byte-identity question).
