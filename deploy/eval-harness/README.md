# deploy/eval-harness/ — the local-first, git-native eval harness

Cost-optimization epic, "step A" (`docs/prd/cost-optimization-candidates.md` §4/§4.1,
`docs/adr/0016-eval-harness-reintegration.md`). Re-founds the daily-AI-brief eval
harness on the decoupled candidate mechanism `deploy/candidates/` already built
(agent-system-redesign epic): a **pure-local Python package**, no AWS, no CDK, that
triggers any candidate (single- or multi-agent, any model mix), retrieves its
produced artifacts via Claude-Platform-only API calls, judges them, attributes cost
per agent/thread, and stores every eval run as small, git-tracked files.

This package is standalone and self-contained (its own `.venv`, `requirements.txt` /
`requirements-dev.txt`, `pytest.ini`) — it reuses `deploy/candidates/candidate_sync`
for candidate loading + trigger/retrieve (via a `sys.path` shim, not a copy) and
ports the delivery-agnostic pure-Python pieces of the OLD, AWS-native `deploy/eval/`
harness (the four judges, the record schema, calibration) unchanged. `deploy/eval/`
was **retired and fully torn down on 2026-07-08** (ADR-0016 Phase 5, owner-executed):
the directory was removed from the tree (the code lives on in git history), the
`BriefEvalStack` and its RETAIN'd table/secrets/bucket were deleted from AWS, and the
old eval records were archived to `docs/notes/brief-eval-records-export-2026-07-08.json`.

## Why this exists (one paragraph)

The old harness (`deploy/eval/`, ADR-0013) could only ever run ONE hardcoded
configuration (the live production agent) and retrieved its output from S3 — neither
of which makes sense anymore now that content generation is decoupled from AWS
delivery (ADR-0014/ADR-0015): a candidate run produces no S3 output at all, and the
cost-optimization epic's whole premise is comparing MANY candidates, most of them
multi-agent with mixed models. ADR-0016 re-founds the harness against that new
reality. See the ADR for the full decision record (D1–D5) and the PRD for the
product-level requirements (§4/§4.1).

## Layout

```
deploy/eval-harness/
  eval_core/             PORTED, unchanged, from deploy/eval/eval_core/ (the four
                          judges, record.py, calibration.py) -- see eval_core/__init__.py
  harness/                NEW code this ADR built:
                            cost.py          per-thread, model-aware cost attribution (D2)
                            run_store.py     the per-eval-run directory read/write (D4)
                            dedup_priors.py  GET /recent-briefs fetch for the dedup judge
                            git_show.py      historical candidate declarations via `git show`
  run.py                  The trigger -> retrieve -> judge -> cost -> record CLI (D4)
  ui.py                   The Flask local web app: Conduct / Assess / Deep dive (D5)
  templates/, static/     ui.py's Jinja2 templates + CSS
  pricing.json             The git-pinned, model-aware price table (D2)
  runs/                   Eval run records -- GIT-TRACKED except runs/**/events.json
                          (see .gitignore and "Run records" below)
  tests/                  Ported + new tests, incl. tests/fixtures/ (golden fixtures)
```

## Setup

```bash
cd deploy/eval-harness
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

The Anthropic API key is read at RUNTIME ONLY, from `$ANTHROPIC_API_KEY` (the CLI)
or `~/.anthropic-managed-agents/ant-api-key.txt` (the UI, at trigger time) — this
repo's established convention. Never hardcode, log, or commit it.

## Tests

```bash
source .venv/bin/activate
python -m pytest -q
```

All tests use fakes/fixtures — no real Anthropic Platform calls, no AWS calls
(`test_run_store.py`'s `current_git_ref` test and `test_git_show.py` run real,
read-only local `git` commands against this repo's own history; that is the one
kind of "real" call this suite makes).

## The cost model (D2)

`pricing.json` pins, per model family, a base input/output price per tier (each with
`effective_from`/`effective_until` and a `source_url`) plus cache-bucket multipliers
relative to the applicable rate card's own base input rate. `harness/cost.py` consumes a
session's per-thread `usage` (`GET /v1/sessions/{id}/threads`) and multiplies it out per
thread, resolving each thread's model PRIMARILY from the triggered candidate's own
declaration (`multiagent.json` roster / `model.txt`), falling back to the thread's own
embedded `agent` object (the same shape `GET /v1/agents/{id}` returns) when no declaration
is given or a thread's `agent_id` isn't in it.

**Cache multipliers are per model, not uniform (changed 2026-10-11).** The table's
top-level `cache_multipliers` (5m write ×1.25, 1h write ×2, read ×0.1) are now DEFAULTS; a
model entry may carry its own `cache_multipliers` object, merged over them key by key.
Claude Opus 5.5 and Claude Sonnet 5.5 use `{"read": 0.05}` — the pricing page's footnote
"Cache hits and refreshes on Claude Opus 5.5 and Claude Sonnet 5.5 are priced at 0.05x the
base input price" — and inherit the ×1.25/×2 write ratios. The pre-2026-10-11 claim that
the ratios apply uniformly to every family is no longer true; never add a model on that
assumption.

**Two rate cards, priced per request (Claude Haiku 5.5).** A tier may carry an optional
`long_prompt` card (`{threshold_prompt_tokens, input_per_million_usd,
output_per_million_usd}`). Haiku 5.5 is $0.10/$0.50 per MTok for prompts up to 100,000
tokens and $0.50/$2.50 above it, and the page bills **each request on its own card**
("prompt length counts all of its input tokens, including cache reads and cache writes …
a request over the threshold pays the higher prices even when part of its prompt is a
cache hit"). Aggregate thread usage cannot say which card each request hit, so such a
thread is priced **per request** from the `span.model_request_end.model_usage` entries in
the session events `run.py` already fetches (and stores as `events.json`) — no extra API
call. `cost.json`'s thread entry then also carries `request_card_counts`
(`{"standard": n, "long_prompt": m}`) so the split that produced the number is visible.
Four fail-loud refusals, never a guess (`cost.PerRequestPricingError`): no events
supplied; a **multi-thread** session (a `span.model_request_end` event carries no thread
identifier, so a request can't be attributed to a thread — single-thread sessions only);
a non-zero 1h cache-write bucket (the per-request `cache_creation_input_tokens` is flat,
with no TTL split); or per-request usages that don't sum to the thread's own totals
(verified exact on every stored single-thread run: 29/27/18 requests). `price_usage()`
(judge-call pricing) refuses a rate-carded model for the same reason.

Claude Sonnet 5's "standard from 2026-09-01 at $3/$15" tier was **corrected** on
2026-10-11: per the page's own footnote that increase will not occur and $2/$10 is the
standard price on every date. Claude Haiku 4.5 and Claude Opus 4.8 are retained unchanged
so every record committed under `runs/` still re-prices to exactly its stored numbers —
asserted by `tests/test_cost_golden.py`, which walks every committed `cost.json` /
`threads-usage.json`.

```bash
# Fail loud if today's date falls outside every declared model's price coverage:
python3 run.py --check-pricing-drift
```

The golden test (`tests/test_cost_golden.py`) reproduces the real captured
`multiagent-aggressive-haiku` spike run's numbers EXACTLY from the committed
`tests/fixtures/{threads,session,cost}.json` — total **$2.3170**; per-thread
coordinator $0.6023, research $0.3570, selection $1.2105, writing $0.1029,
listening-script $0.0442 — and confirms sum-of-threads usage equals the session's
own total usage.

## The four judges (`eval_core/judges/`, judge methodology v2 — 2026-07-07)

Each of the four v1 criteria (`content_selection`, `factual_accuracy`, `length_format`,
`dedup`) is scored by a one-shot Anthropic Messages API call (`eval_core/judges/base.py`'s
`run_judge()`) — not a Managed Agents session. **Judge methodology v2** (owner-directed,
2026-07-07, `docs/adr/0016-eval-harness-reintegration.md`'s dated amendment) reworked
three of the four after two real committed runs exposed genuine judge-quality defects;
`length_format` is unchanged.

**Models — ALL FOUR run on `claude-opus-5-5`** (owner-directed 2026-10-11; `claude-opus-4-8`
before that). The owner's principle: a judge must be run on a model STRONGER than what it
judges, or the evaluation doesn't mean much. Per-judge model config stays flippable —
`eval_core/judges/base.JUDGE_MODELS` is the one small mapping (`{criterion: model_id}`) every
judge module resolves its own entry from; changing one judge's model back down is a one-line
edit there, not a hunt through four files. Each `JudgeResult` records which model actually ran
the call (`.model`), so `run.py`'s `_price_judge_results()` prices every criterion against ITS
OWN model — `judge-cost.json`'s top-level `models` field is the sorted set of models actually
used across the criteria a run tested (usually just `["claude-opus-5-5"]`), and each
criterion's entry in **`scores.json` also carries its `model`** so a score committed before
2026-10-11 (judged by Opus 4.8) stays distinguishable from one after it. `pricing.json`
deliberately retains the Opus 4.8 entry for that reason.

**What the Opus 5.5 move changed in the judge call** (and nothing else — prompts, tool
versions, `max_uses` caps and top-level `cache_control` are untouched; verified live
2026-10-11 that `claude-opus-5-5` accepts `web_search_20250305` + `web_fetch_20250910`
together with `output_config.effort` and top-level `cache_control` on the installed SDK,
anthropic 0.116.0):

- **Explicit `output_config={"effort": "high"}`** on every call. Opus 5.5's API default is
  `medium` where Opus 4.8's was `high`, so a bare model swap would quietly drop the judges one
  deliberation level below every previously-committed run.
- **`max_tokens` raised** to 16000 for the three reasoning judges and 8000 for `length_format`
  (from 1024–4096). Thinking is always on and cannot be disabled on this model, and thinking
  tokens count against `max_tokens`, so the old caps would truncate. `run_judge()` sends no
  `thinking` field, no `budget_tokens`, no sampling params and no forced `tool_choice` — all
  of which 400 on Opus 5.5.
- **`stop_reason` is handled before the content is read** (there was none before):
  `"refusal"` raises `JudgeRefusedError` carrying `stop_details.category` — deliberately with
  NO fallback to another model, since a silent model swap mid-batch would change the measuring
  instrument; `"max_tokens"` raises `JudgeTruncatedError`; `"pause_turn"` (the server-side tool
  loop's 10-iteration limit) is resumed by sending the paused assistant turn back unchanged, up
  to 5 continuations, SUMMING token usage and web-search counts across iterations so a resumed
  call is never under-billed. All three are plain exceptions that land in `run.py`'s existing
  per-repetition containment (`final_status="judging_failed"`, run state `failed`, paid
  artifacts/events kept) — no new silent-degrade path.
- Thinking blocks (`type: "thinking"`, empty text under the default display) are ignored by the
  verdict extraction, which has always selected only `type == "text"` blocks.

- **`factual_accuracy` — full rework.** Previously judged PLAUSIBILITY against the judge's
  own training data (a real committed run penalized a correctly-dated brief for being
  "dated July 7, 2026 — a future date," i.e. knowledge-cutoff bias). Now ACTUALLY VALIDATES
  the brief's claims via the judge's OWN live research: given the brief markdown plus this
  repo's curated `sources.md` (read fresh at trigger time from
  `deploy/managed-agent/skills/daily-ai-brief/sources.md` — the ADR-0008 in-repo/live-Skills-API
  lockstep copy — and passed into the prompt, never fetched by the judge itself), it extracts
  headlines/claims per section (focus set: headlines, numbers, dates, dollar amounts,
  benchmark scores, direct quotes, named products/models), then verifies/falsifies each via
  server-side **web_search** (`web_search_20250305`, `max_uses: 8`) and **web_fetch**
  (`web_fetch_20250910`, `max_uses: 8`) tools. The system prompt explicitly forbids treating
  "I don't recognize this" as fabrication evidence — verification comes from live research,
  never training-data familiarity. Output gains a structured `findings` array:
  `{claim, verdict: confirmed|contradicted|unverifiable, source_checked, note}`, with any
  deviation between the brief's version and the judge's own research specifically documented.
- **`content_selection` — targeted upgrade.** Keeps its proven `candidates.json`-vs-brief
  contrast approach unchanged. Adds the same `web_search`/`web_fetch` tools (`max_uses: 5`
  each) with an instruction to check the sources/internet before disagreeing with a selection
  decision, sharpening the judge's editorial-priority call rather than relying on static
  topic familiarity. Output gains a `selection_disagreements` array:
  `{story, judge_view, rationale}`, populated only when the judge concludes (after checking)
  it would have selected differently.
- **`dedup` — feed fix + richer assessment.** A real committed run exposed structural
  contamination: the judge was handed a "prior" that was actually the SAME-DAY production
  brief, because `GET /recent-briefs` filters against the delivery Lambda's own wall-clock
  "today" at REQUEST time, not the date of the specific brief under evaluation. **The fix
  lives in the harness, not the judge or the endpoint**: `harness/dedup_priors.py`'s
  `fetch_recent_prior_briefs(brief_date=...)` over-fetches (`count + 2`) from
  `GET /recent-briefs`, then locally drops any entry whose own `date` is the SAME AS OR AFTER
  the eval brief's own date (parsed by `run.py` from its artifact filename,
  `AI Brief - YYYY-MM-DD.md`), dedupes to one entry per date, and caps at `count`. Each
  prior's date is told to the judge explicitly in the prompt (`PRIOR EDITION (YYYY-MM-DD):`).
  The judge now documents, per potential duplication, THREE things in a structured `findings`
  array — `{story, duplicate_of_date, labelled_as_followup, justified, note}`: is it a
  duplicate at all; IS it labelled as a follow-up in today's brief; IS that follow-up
  justified by substantial new data (vs. a bare rehash). No web tools (two same-day texts
  need no external verification).
- **`length_format` — UNCHANGED.** Prompt, rubric, and approach are exactly as they were
  (a length/format check needs no live research and was never implicated by either
  live-run finding) — the ONLY changes are its model, effort and `max_tokens`, per the
  uniform Opus default above.

**Web-tool schema/versions** (verified live 2026-07-07 against
`platform.claude.com/docs/en/docs/agents-and-tools/tool-use/{web-search,web-fetch}-tool`,
recorded in `accuracy.py`'s own docstring): `web_search_20250305` / `web_fetch_20250910`
("basic" variants, both still current/documented) — **no `anthropic-beta` header required
for either** (a change from the historical web-fetch beta-header requirement, confirmed by
the docs' own cURL examples sending none). `max_uses` caps the number of tool invocations
per judge call. A response's `usage.server_tool_use.web_search_requests` reports how many
searches a call actually made (`base._extract_search_count()`).

**Judge-cost accounting** (`judge-cost.json`, per repetition) prices token usage per the
per-judge model above (`cost_usd`), and web-search usage SEPARATELY (`pricing.json`'s flat
`web_search.cost_per_1000_searches_usd: 10.0` → `harness.cost.price_web_searches()`):
`search_count`/`search_cost_usd` per criterion, `total_search_count`/`total_search_cost_usd`
at the top level, never folded into `total_cost_usd` — `grand_total_cost_usd` is the one
convenience sum for "judging cost, all-in." Web fetch is NOT priced separately — Anthropic
bills it at ordinary token cost only, no per-call fee (confirmed live the same day). A judge
model with no `pricing.json` entry fails loud (`cost.UnknownModelPriceError`), never silently
prices as $0. Every judge call enables **automatic prompt caching** (top-level
`cache_control: {"type": "ephemeral"}` in `run_judge()`): a web-tool judge's server-side
loop re-sends the whole accumulated context every iteration, and the first live uncached
smoke of the v2 accuracy judge cost **$1.60** (281,543 full-price input tokens); the
identical cached re-run cost **$0.88 (-45%)** — 10 full-price input + ~99K cache-write +
~193K cache-read tokens. Those figures were measured on **Opus 4.8**; on Opus 5.5 the same
call is cheaper on both axes (base input $4 vs $5, and cache reads at 0.05x base input vs
0.1x — i.e. $0.20 vs $0.50 per MTok), though explicit `high` effort means more thinking
tokens, so re-baseline from the first real 5.5 run rather than scaling the old numbers.
Historical measured judge cost (Opus 4.8): **~$0.88 for the accuracy judge alone; roughly
$1.40–$1.60 per repetition all-in across all four** (incl. up to 8+5=13 web searches at
$0.01 each) — note Opus 4.7+/Sonnet 5 use a newer tokenizer that produces **~30% more
tokens for the same text** than Haiku 4.5's tokenizer (part of why an Opus judge costs more
per call than a raw token-count comparison alone would suggest — see `pricing.json`'s own
comment).

**Response parsing**: a server-side-tool response can carry MIXED content (`thinking` blocks,
narration text, `server_tool_use`/tool-result blocks, then a final text block) — `run_judge()`
parses ONLY the LAST text block for the JSON verdict, never joins every text block (an earlier
narration block's own stray braces could otherwise corrupt the parse) and never assumes the
first block is the answer. The `scores.json` schema stays additive: the original
`{score, rationale, evidence, insufficient_data}` shape is unchanged, plus `model` (always,
since 2026-10-11) and `findings`/`selection_disagreements` only when a judge's result actually
carries one. The
Deep Dive UI (`templates/run_detail.html`) renders both as plain tables, escaped exactly like
every other judge-authored field (never `| safe`).

## Triggering an eval run (`run.py`)

```bash
export ANTHROPIC_API_KEY=$(cat ~/.anthropic-managed-agents/ant-api-key.txt)

# 3 repetitions against a real candidate, judging two criteria:
python3 run.py production-baseline --name "baseline sanity check" \
    --repetitions 3 --criteria content_selection,factual_accuracy

# All four v1 criteria (the default), one repetition (the default):
python3 run.py haiku-swap --name "haiku swap quick look"

# A multi-agent candidate's real research/writing run takes longer than the
# 600s default poll timeout -- raise it:
python3 run.py multiagent-aggressive-haiku --timeout 1200
```

If a candidate's task prompt uses the `__RECENT_BRIEFS_TOKEN__`/
`__DELIVERY_BASE_URL__` placeholders (ADR-0014 Decision 2d — `production-baseline`,
`haiku-swap`, `multiagent-aggressive-haiku`, `session-restructure` all do), the two
values resolve via `harness/local_config.py` in this order (added after the first
owner UI-trigger failed because the UI's server process had no exports):

1. **Signing key** (secret): `$RECENT_BRIEFS_SIGNING_KEY` if set, else the
   well-known local file `~/.anthropic-managed-agents/recent-briefs-signing-key.txt`
   (populate once, `chmod 600`, from the
   `daily-ai-brief/recent-briefs-read-bearer-secret` Secrets Manager secret —
   same convention as `ant-api-key.txt`; the harness itself never calls AWS).
2. **Delivery base URL** (not a secret): `$DELIVERY_BASE_URL` if set, else the
   committed default in `local_config.py`.

With neither env var nor key file present, `run.py` still fails loud with a clear
error naming both sources. The same resolution also feeds the dedup judge's
recent-priors read via `GET /recent-briefs` (`harness/dedup_priors.py`) — a missing
key there degrades to `insufficient_data` rather than failing the run. This is what
lets the Flask UI trigger runs even when the server process was started without any
exports (e.g. by a preview panel or launchd).

The `--email` flag is a **deferred stub** (ADR-0016 D3): passing it exits
immediately with an error pointing at the ADR, rather than sending anything. An
eval run never emails anyone and never touches AWS delivery, by construction.

### The captured-brief guard (2026-10-11)

Before any judging, **every** repetition (whatever criteria subset was selected)
must prove it actually captured the candidate's *own* brief —
`run._verify_candidate_brief_was_captured()` raises
`CandidateBriefNotCapturedError` when:

- **no brief artifact was captured at all** (previously a silent degrade: the
  judges got `""` and reported `insufficient_data`, which the Assess table renders
  as a blank — indistinguishable from "untested"), or
- **the selected brief is one the delivery feed already carries**, compared by
  whitespace-normalized *content* against an unfiltered
  `GET /recent-briefs` read (`dedup_priors.fetch_recent_briefs_unfiltered()`).

Why content and not dates: in a real paid batch, a `haiku-swap-hardened`
repetition wrote today's brief and then ran `cat "…" | head -100` at the capture
step — a piped `cat` the retrieval layer does not recognize — so the only briefs
on hand were the two **priors** the hardened prompt makes the agent read, and
Friday's real production brief got scored as the candidate's work and recorded as
a success. A date rule cannot catch that (on a weekday the feed legitimately holds
a same-dated brief).

The error flows into the existing per-repetition containment:
`final_status="judging_failed"` with the error and the captured filenames named,
no `scores.json`, and the paid `artifacts/`, `events.json`, `cost.json` and
`threads-usage.json` all still written (cost is mined **before** the guard, on
purpose, so the spend is always recorded). If the feed cannot be read at all
(no signing key, transport failure) the check prints
`BRIEF_PRIOR_CHECK_SKIPPED` on stderr and the repetition proceeds.

## The UI (`ui.py`, D5)

```bash
source .venv/bin/activate
python3 ui.py
# -> http://127.0.0.1:5151 (binds to 127.0.0.1 explicitly; no auth, localhost-only)
```

### Running it permanently (macOS LaunchAgent — owner's setup, 2026-07-07)

The owner runs the UI as a login-persistent, auto-restarting launchd service so
`http://127.0.0.1:5151` is always up on the operator Mac (localhost-only —
deliberately NOT hosted publicly; the trigger endpoint spends real money and the
records are written into this git working tree, per ADR-0016 D1/D5). The agent
definition lives OUTSIDE the repo at
`~/Library/LaunchAgents/com.mschweier.eval-harness-ui.plist` (ProgramArguments =
this package's `.venv/bin/python ui.py`, `RunAtLoad` + `KeepAlive` on non-zero
exit, logs to `~/Library/Logs/eval-harness-ui.log`). Manage it with:

```bash
launchctl bootout gui/$UID/com.mschweier.eval-harness-ui     # stop
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.mschweier.eval-harness-ui.plist  # start
launchctl kickstart -k gui/$UID/com.mschweier.eval-harness-ui  # restart (e.g. after a ui.py change)
tail -f ~/Library/Logs/eval-harness-ui.log                    # logs
```

Note the service serves whatever branch this working tree has checked out — the
same live-repo semantics as the CLI. After pulling/switching branches, `kickstart -k`
it so template/code changes take effect.

- **Conduct** (`/conduct`) — pick a candidate (or the production-marked one),
  name the run, set repetitions, pick a criteria subset, and trigger. Triggering
  launches `run.py` as a **background subprocess** (a real run takes ~15 minutes;
  the request returns immediately) and redirects to that run's Deep Dive page,
  which auto-refreshes every 5s while `configured`/`running`.
- **Assess** (`/assess`) — one-page table of every eval run: name, model(s),
  agent-vs-multi-agent, repetitions, production-config marker, all four criteria
  (blank where untested), mean cost, the **judge model** that scored the run
  (so a July record judged by Opus 4.8 is distinguishable from an October one
  judged by Opus 5.5), mean judge cost, whether a human eval exists.
- **Deep dive** (`/runs/<slug>/<eval-run-id>`) — per-repetition explorer (scores,
  cost by thread, artifacts, the rendered brief), plus the candidate's full
  configuration (main + every sub-agent's prompts) read via `git show <git_ref>:<path>`
  at the EXACT commit the run was triggered against — no repo checkout/rollback.

#### `git` resolution for the Deep Dive page (review-fix, 2026-10-11)

`harness/git_show.py` does **not** trust the process's `PATH` to hold a working
`git`, because under launchd it does not: launchd's default `PATH` resolves `git`
to `/usr/bin/git`, Apple's command-line-tools shim, which on a Mac whose Xcode
license has not been accepted prints *"You have not agreed to the Xcode license
agreements"* instead of doing anything — which made **every** Deep Dive page
return HTTP 500 while `/opt/homebrew/bin/git` worked fine. The module now probes
for a git that actually works (PATH's `git` first, then `/opt/homebrew/bin/git`,
`/usr/local/bin/git`, `/usr/bin/git`), accepting a candidate only if `git
--version` both exits 0 **and** prints a real `git version ...` line **and** a
`git rev-parse --show-toplevel` call succeeds; the result is cached per process.
If nothing works — or a `git show` fails, or the recorded ref is unknown to this
clone — the page still renders everything else and shows a visible note in the
candidate-configuration section. Fixing this needed no change to the plist.

## Run records (ADR-0016 D4)

Every eval run writes a git-tracked directory:

```
runs/<candidate-slug>/<eval-run-id>/
  eval-run.json          name; slug + agent_id + git ref; composition; model(s) +
                          parameters; repetitions; criteria; state
                          (configured|running|completed|failed); email_sent;
                          is_production_config; created_at
  repetitions/
    01/
      artifacts/          AI Brief - <date>.md, listening-script.txt,
                          candidates.json, source-usage.json (whichever were cat'd)
      threads-usage.json / cost.json   per-thread usage + cost (harness.cost)
      scores.json          per-criterion {score, rationale, evidence,
                            insufficient_data, model (the judge model)}
      run-meta.json         deployment_id, session_id, thread_count, final_status
      events.json           GITIGNORED (runs/**/events.json) -- the full raw trace
    02/ ...
  summary.json            aggregate across repetitions (mean/stdev/min/max per
                          criterion, mean cost) -- eval_core.record.aggregate_replicates
  human-eval.md           optional free-form owner assessment
```

Everything above is committed EXCEPT the large raw `events.json` per repetition —
see `.gitignore`'s targeted `runs/**/events.json` pattern (not a blanket `runs/`
ignore).

### `is_production_config` is HISTORICAL (review-fix, 2026-10-11)

The flag means **“this candidate *was* the production configuration at the time of
this run”** — resolved once by `run.py` when the record is written, from the single
shared source `harness/production_config.py`, and then never recomputed or
rewritten (the Assess table renders the stored value). Production ran
`production-baseline`’s Sonnet 5 configuration until **2026-07-13**, when the owner
moved production onto **`haiku-swap-hardened`** (Haiku 4.5 + hardened prompt — see
`docs/prd/cost-optimization-candidates.md` §6), so:

- `production-baseline`’s July records legitimately keep `true`;
- new `production-baseline` runs record `false`, and new `haiku-swap-hardened`
  runs record `true`;
- a candidate can also claim the marker itself via `candidate.json`’s optional
  `is_production_config` field, which wins over the slug — so the next production
  promotion is a declaration edit, not a code change.

## Calibration (parked, not wired in)

`eval_core/calibration.py` (reader-feedback correlation) is ported but
deliberately NOT part of the trigger/retrieve/record loop or the UI — it is the
one legacy piece that reads an AWS resource (`brief-feedback`, DynamoDB) and isn't
in the PRD §4.1 UI requirements. Run it as a separate, manual, read-only script
with AWS creds when wanted.
