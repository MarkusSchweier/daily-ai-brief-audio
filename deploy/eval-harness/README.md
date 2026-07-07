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
  eval_core/             PORTED, unchanged, from deploy/eval/eval_core/ (judges,
                          record.py, calibration.py) -- see eval_core/__init__.py
  harness/                NEW code this ADR built: cost.py (D2), run_store.py (D4),
                          dedup_priors.py, run.py (the trigger/retrieve/record CLI, D4)
  ui/                     NEW: the Flask local web app (D5)
  runs/                   Eval run records -- GIT-TRACKED (see "Run records" below)
  pricing.json             The git-pinned, model-aware price table (D2)
  tests/                  Ported + new tests, incl. tests/fixtures/ (golden fixtures)
```

## Setup

```bash
cd deploy/eval-harness
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Tests

```bash
source .venv/bin/activate
python -m pytest -q
```

(Sections below are filled in as each phase of the ADR's implementation plan lands:
the cost model, the trigger/retrieve/record CLI, and the UI.)
