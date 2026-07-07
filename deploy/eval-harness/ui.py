#!/usr/bin/env python3
"""The tiny local Flask web app (ADR-0016 D5) that both TRIGGERS eval runs and
VIEWS their results -- the owner's Gate-0 pick, option (b) ("a tiny local web app
(Flask/FastAPI) that serves the same views and can trigger a run... kicking it off
as a background subprocess so the ~14-minute multi-agent long-poll doesn't block
the request").

Three pages, matching PRD §4.1 exactly:
  - Conduct  (`/conduct`, `GET`/`POST /trigger`)  -- define + trigger an eval run.
  - Assess   (`/assess`)                          -- one-page comparison table.
  - Deep dive (`/runs/<slug>/<eval_run_id>`)      -- per-repetition explorer +
                                                      candidate config + brief render.

Localhost-only by design (PRD/ADR: no auth needed) -- `main()` binds to
`127.0.0.1` EXPLICITLY, never `0.0.0.0`. No AWS, no Anthropic Platform calls in
THIS process -- triggering launches `run.py` as a background subprocess (this
process's job is to view git-tracked run records and kick off that subprocess; it
never itself calls the Anthropic API).

XSS discipline: Jinja2 templates autoescape by default (Flask's own default for
`.html` templates) -- every judge rationale/evidence string, candidate
name/description, and prompt text below is emitted through ordinary `{{ }}`
interpolation, NEVER `| safe`, mirroring `deploy/eval/site/app.js`'s
`textContent`/`createElement` discipline (this repo's established pattern for
content that ultimately traces back to LLM output about third-party web sources --
never assumed inert). The ONE exception is the brief's rendered Markdown->HTML
(`render_markdown()` below), which IS marked `| safe` in `run_detail.html` -- this
mirrors `deploy/delivery/functions/deliver/delivery_core.py`'s own accepted
approach for the exact same content (the daily brief's own body) and carries the
same scoped risk profile (LLM-authored English prose summarizing news, not
directly attacker-controlled input) -- flagged here explicitly for a future
security review, not silently assumed safe.

The Anthropic API key is NEVER read, held, rendered, or logged by this process
itself -- `_trigger_run()` reads it fresh from
`~/.anthropic-managed-agents/ant-api-key.txt` only at the moment of launching the
`run.py` subprocess, passes it via that CHILD process's own environment only, and
never includes it in any log line, template, or response.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HARNESS_DIR = Path(__file__).resolve().parent
CANDIDATES_DIR = HARNESS_DIR.parent / "candidates"
RUN_PY_PATH = HARNESS_DIR / "run.py"
ANTHROPIC_API_KEY_FILE = Path.home() / ".anthropic-managed-agents" / "ant-api-key.txt"

sys.path.insert(0, str(HARNESS_DIR))
sys.path.insert(0, str(CANDIDATES_DIR))

import markdown
from candidate_sync.loader import CandidateLoadError, load_candidate  # noqa: E402
from eval_core.record import V1_CRITERIA  # noqa: E402
from flask import Flask, redirect, render_template, request, url_for  # noqa: E402

from harness import git_show, run_store  # noqa: E402

app = Flask(__name__)


# --- Candidate listing (Conduct page) -----------------------------------------------


def list_candidate_options(*, candidates_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every loadable candidate under `deploy/candidates/` -- the Conduct page's
    "select the agent: a candidate or production" dropdown source. Skips any
    directory that isn't a valid candidate declaration (e.g. a future non-candidate
    directory added to `deploy/candidates/`) rather than erroring the whole page."""
    root = candidates_dir or CANDIDATES_DIR
    if not root.is_dir():
        return []

    options: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not (entry / "candidate.json").is_file():
            continue
        try:
            candidate = load_candidate(entry)
        except CandidateLoadError:
            continue
        options.append(
            {
                "slug": candidate.slug,
                "composition": "multi-agent" if candidate.is_multi_agent else "single-agent",
                "is_production_config": candidate.slug == "production-baseline"
                or bool(candidate.candidate_json.get("is_production_config", False)),
                "has_agent_id": candidate.agent.agent_id is not None,
            }
        )
    return options


# --- Assess page ------------------------------------------------------------------


def _format_parameters(parameters: dict[str, Any]) -> str:
    """Compact display string for `eval-run.json`'s `parameters` field (D4:
    "model(s) + thinking/effort params") -- the Assess table's "thinking
    parameters" column (PRD §4.1). `parameters` shape:
    `{"agent": {...effort/thinking params...}, "sub_agents": [{"name", "model",
    "parameters"}, ...]}` (see `run._declared_parameters()`)."""
    parts: list[str] = []
    agent_params = parameters.get("agent") or {}
    if agent_params:
        parts.append(", ".join(f"{k}={v}" for k, v in agent_params.items()))
    for sub_agent in parameters.get("sub_agents") or []:
        sub_params = sub_agent.get("parameters") or {}
        if sub_params:
            name = sub_agent.get("name", "?")
            parts.append(name + ": " + ", ".join(f"{k}={v}" for k, v in sub_params.items()))
    return " | ".join(parts) if parts else "—"


def _load_run_row(run_dir: Path) -> dict[str, Any]:
    """One `runs/<slug>/<eval-run-id>/` directory's summary row for the Assess
    table -- PRD §4.1: name, model, thinking params, agent vs multi-agent,
    repetitions, production-config marker, one column per criterion (blank if
    untested), cost, human eval."""
    meta = run_store.read_eval_run_meta(run_dir)

    criterion_scores: dict[str, float | None] = {c: None for c in V1_CRITERIA}
    mean_cost_usd: float | None = None
    mean_judge_cost_usd: float | None = None
    summary_path = run_dir / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        for criterion, aggregate in (summary.get("criterion_aggregates") or {}).items():
            if criterion in criterion_scores:
                criterion_scores[criterion] = aggregate.get("mean")
        mean_cost_usd = summary.get("mean_cost_usd")
        # Judge cost is a SEPARATE block (never folded into mean_cost_usd above --
        # review-fix: "do NOT fold it into the pipeline cost column").
        mean_judge_cost_usd = (summary.get("judge_cost") or {}).get("mean_cost_usd")

    human_eval_path = run_dir / "human-eval.md"
    human_eval_present = human_eval_path.is_file() and human_eval_path.read_text(encoding="utf-8").strip() not in (
        "",
        "<!-- Optional free-form human assessment of this eval run. -->",
    )

    return {
        "slug": meta.slug,
        "eval_run_id": run_dir.name,
        "name": meta.name,
        "models": meta.models,
        "parameters": meta.parameters,
        "parameters_display": _format_parameters(meta.parameters),
        "composition": meta.composition,
        "repetitions": meta.repetitions,
        "is_production_config": meta.is_production_config,
        "criteria": meta.criteria,
        "criterion_scores": criterion_scores,
        "mean_cost_usd": mean_cost_usd,
        "mean_judge_cost_usd": mean_judge_cost_usd,
        "state": meta.state,
        "human_eval_present": human_eval_present,
        "created_at": meta.created_at,
    }


# --- Routes -------------------------------------------------------------------------


@app.route("/")
def index():
    return redirect(url_for("conduct"))


@app.route("/conduct", methods=["GET"])
def conduct():
    return render_template(
        "conduct.html",
        candidates=list_candidate_options(),
        criteria=list(V1_CRITERIA),
    )


@app.route("/trigger", methods=["POST"])
def trigger():
    candidate_slug = request.form.get("candidate_slug", "").strip()
    run_name = request.form.get("name", "").strip() or f"{candidate_slug} run"
    criteria = request.form.getlist("criteria")
    try:
        repetitions = int(request.form.get("repetitions", "1") or "1")
    except ValueError:
        repetitions = 1
    repetitions = max(1, repetitions)

    # The email toggle is rendered DISABLED in the form (ADR-0016 D3: deferred) --
    # even if a form submission somehow carried it, this handler never reads it
    # and never passes --email to the subprocess. No path from this route ever
    # sends an eval email.

    if not candidate_slug:
        return render_template(
            "conduct.html",
            candidates=list_candidate_options(),
            criteria=list(V1_CRITERIA),
            error="Select a candidate before triggering a run.",
        ), 400

    if not criteria:
        return render_template(
            "conduct.html",
            candidates=list_candidate_options(),
            criteria=list(V1_CRITERIA),
            error="Select at least one criterion to judge.",
        ), 400

    eval_run_id = _trigger_run(candidate_slug, run_name, repetitions, criteria)
    if eval_run_id is None:
        return render_template(
            "conduct.html",
            candidates=list_candidate_options(),
            criteria=list(V1_CRITERIA),
            error=(
                f"Could not read the Anthropic API key from {ANTHROPIC_API_KEY_FILE} -- "
                "place your key there before triggering a run."
            ),
        ), 400

    return redirect(url_for("run_detail", slug=candidate_slug, eval_run_id=eval_run_id))


def _trigger_run(candidate_slug: str, run_name: str, repetitions: int, criteria: list[str]) -> str | None:
    """Launch `run.py` as a background subprocess (non-blocking -- a real
    multi-agent run takes ~15 minutes, and this request must return immediately per
    ADR-0016 D5) and return the eval-run-id it will write to, computed with the
    SAME `make_eval_run_id()` this route uses so the redirect target is known
    up front, before the subprocess has done anything. Returns None (and launches
    nothing) if the Anthropic API key file is unreadable."""
    if not ANTHROPIC_API_KEY_FILE.is_file():
        return None
    api_key = ANTHROPIC_API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not api_key:
        return None

    eval_run_id = run_store.make_eval_run_id(run_name)

    # NOTE: run.py itself also calls make_eval_run_id() internally and would
    # normally compute its OWN (slightly later, if the clock ticks over a second)
    # timestamp -- to guarantee the id this route redirects to is the SAME
    # directory run.py actually writes, pin it via an env var run.py consults in
    # preference to computing its own. See run.py's `_EVAL_RUN_ID_OVERRIDE_ENV_VAR`.
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = api_key
    env["EVAL_HARNESS_RUN_ID_OVERRIDE"] = eval_run_id

    log_dir = run_store.RUNS_ROOT / candidate_slug / eval_run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "subprocess.log"

    command = [
        sys.executable,
        str(RUN_PY_PATH),
        candidate_slug,
        "--name",
        run_name,
        "--repetitions",
        str(repetitions),
        "--criteria",
        ",".join(criteria),
    ]
    with open(log_path, "wb") as log_file:
        subprocess.Popen(command, cwd=str(HARNESS_DIR), env=env, stdout=log_file, stderr=subprocess.STDOUT)

    return eval_run_id


@app.route("/assess")
def assess():
    rows = [_load_run_row(run_dir) for run_dir in run_store.list_eval_runs()]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return render_template("assess.html", rows=rows, criteria=list(V1_CRITERIA))


@app.route("/runs/<slug>/<eval_run_id>")
def run_detail(slug: str, eval_run_id: str):
    run_dir = run_store.eval_run_dir(slug, eval_run_id)
    if not (run_dir / "eval-run.json").is_file():
        return render_template("not_found.html", slug=slug, eval_run_id=eval_run_id), 404

    meta = run_store.read_eval_run_meta(run_dir)
    declaration = git_show.read_candidate_declaration_at_ref(meta.git_ref, meta.slug)

    repetitions = []
    for index in range(1, meta.repetitions + 1):
        rep_dir = run_store.repetition_dir(run_dir, index)
        if not rep_dir.is_dir():
            continue
        run_meta_path = rep_dir / "run-meta.json"
        run_meta = json.loads(run_meta_path.read_text(encoding="utf-8")) if run_meta_path.is_file() else {}
        cost_path = rep_dir / "cost.json"
        cost_data = json.loads(cost_path.read_text(encoding="utf-8")) if cost_path.is_file() else None
        judge_cost_data = run_store.read_judge_cost(run_dir, index)
        scores = run_store.read_scores(run_dir, index)
        artifacts = run_store.read_artifacts(run_dir, index)
        brief_markdown = next((v for k, v in artifacts.items() if k.startswith("AI Brief") and k.endswith(".md")), None)
        repetitions.append(
            {
                "index": index,
                "run_meta": run_meta,
                "cost": cost_data,
                "judge_cost": judge_cost_data,
                "scores": scores,
                "artifacts": artifacts,
                "brief_html": render_markdown(brief_markdown) if brief_markdown else None,
            }
        )

    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else None

    human_eval_path = run_dir / "human-eval.md"
    human_eval_text = human_eval_path.read_text(encoding="utf-8") if human_eval_path.is_file() else ""

    return render_template(
        "run_detail.html",
        meta=meta,
        declaration=declaration,
        repetitions=repetitions,
        summary=summary,
        human_eval_text=human_eval_text,
    )


def render_markdown(text: str | None) -> str:
    """Server-side Markdown -> HTML render for the deep-dive brief view (PRD
    §4.1: "render the MD or HTML of the brief"). Plain conversion, no extensions --
    this is a READ-ONLY debugging view, not the production email template (that is
    `deploy/delivery/functions/deliver/delivery_core.py`'s job, unrelated to this
    harness)."""
    if not text:
        return ""
    return markdown.markdown(text)


def main() -> None:
    # 127.0.0.1 EXPLICITLY -- never 0.0.0.0 (PRD/ADR: "no auth needed... bind to
    # 127.0.0.1 explicitly").
    app.run(host="127.0.0.1", port=5151, debug=False)


if __name__ == "__main__":
    main()
