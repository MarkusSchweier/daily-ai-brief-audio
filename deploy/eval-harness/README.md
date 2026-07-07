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
itself is left untouched by this work — it is retired later, as a separate,
owner-gated step (ADR-0016 Phase 5), only after this package is validated.

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

`pricing.json` pins, per model family, a base input/output price per tier (each
with `effective_from`/`effective_until` and a `source_url`) plus fixed cache-bucket
multipliers (5m write ×1.25, 1h write ×2, read ×0.1 — all relative to that model's
own base input rate). `harness/cost.py` consumes a session's per-thread `usage`
(`GET /v1/sessions/{id}/threads`) and multiplies it out per thread, resolving each
thread's model PRIMARILY from the triggered candidate's own declaration
(`multiagent.json` roster / `model.txt`), falling back to the thread's own embedded
`agent` object (the same shape `GET /v1/agents/{id}` returns) when no declaration is
given or a thread's `agent_id` isn't in it.

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
`haiku-swap`, `multiagent-aggressive-haiku`, `session-restructure` all do), set
`$RECENT_BRIEFS_SIGNING_KEY`/`$DELIVERY_BASE_URL` first, exactly as
`deploy/candidates/trigger.py` requires — `run.py` fails loud with the same clear
error if they're needed and missing. The same two env vars, if set, also let the
dedup judge (when selected) read real recent priors via `GET /recent-briefs`
(`harness/dedup_priors.py`) — if unset, dedup degrades to `insufficient_data`
rather than failing the run.

The `--email` flag is a **deferred stub** (ADR-0016 D3): passing it exits
immediately with an error pointing at the ADR, rather than sending anything. An
eval run never emails anyone and never touches AWS delivery, by construction.

## The UI (`ui.py`, D5)

```bash
source .venv/bin/activate
python3 ui.py
# -> http://127.0.0.1:5151 (binds to 127.0.0.1 explicitly; no auth, localhost-only)
```

- **Conduct** (`/conduct`) — pick a candidate (or the production-marked one),
  name the run, set repetitions, pick a criteria subset, and trigger. Triggering
  launches `run.py` as a **background subprocess** (a real run takes ~15 minutes;
  the request returns immediately) and redirects to that run's Deep Dive page,
  which auto-refreshes every 5s while `configured`/`running`.
- **Assess** (`/assess`) — one-page table of every eval run: name, model(s),
  agent-vs-multi-agent, repetitions, production-config marker, all four criteria
  (blank where untested), mean cost, whether a human eval exists.
- **Deep dive** (`/runs/<slug>/<eval-run-id>`) — per-repetition explorer (scores,
  cost by thread, artifacts, the rendered brief), plus the candidate's full
  configuration (main + every sub-agent's prompts) read via `git show <git_ref>:<path>`
  at the EXACT commit the run was triggered against — no repo checkout/rollback.

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
      scores.json          per-criterion {score, rationale, evidence, insufficient_data}
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

## Calibration (parked, not wired in)

`eval_core/calibration.py` (reader-feedback correlation) is ported but
deliberately NOT part of the trigger/retrieve/record loop or the UI — it is the
one legacy piece that reads an AWS resource (`brief-feedback`, DynamoDB) and isn't
in the PRD §4.1 UI requirements. Run it as a separate, manual, read-only script
with AWS creds when wanted.
