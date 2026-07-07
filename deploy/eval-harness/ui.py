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
(`render_markdown()` below), which IS marked `| safe` in `run_detail.html` --
AMENDED (review-fix, security M1): `markdown.markdown()` on its own passes raw
HTML embedded in the source Markdown straight through (a `<script>` tag or an
`onerror=` attribute in the brief's own text would previously have rendered
live) -- `render_markdown()` now pipes the converted HTML through
`bleach.clean()` with a small explicit tag/attribute allowlist before it is ever
marked `| safe`, so the `| safe` marker is only ever applied to
ALREADY-SANITIZED output.

The Anthropic API key is NEVER read, held, rendered, or logged by this process
itself -- `_trigger_run()` reads it fresh from
`~/.anthropic-managed-agents/ant-api-key.txt` only at the moment of launching the
`run.py` subprocess, passes it via that CHILD process's own environment only, and
never includes it in any log line, template, or response.

`/trigger` security hardening (review-fix pass, 2026-07-07):
  - **Slug allowlist (security M2).** `candidate_slug` is checked against the
    REAL candidate set (`list_candidate_options()`) BEFORE any path construction
    or subprocess launch -- rejects path traversal (`../../tmp/evil`) and
    flag-looking values (`--check-pricing-drift`) alike with a 400, since neither
    is ever a real directory name. `_trigger_run()`'s subprocess argv also puts
    `candidate_slug` LAST, after a bare `--`, so `run.py`'s own argparse can never
    misinterpret it as a flag either (defense in depth on top of the allowlist).
  - **CSRF (security M3).** A per-PROCESS synchronizer token (`_CSRF_TOKEN`,
    generated once at import time) is rendered as a hidden field in the Conduct
    form and compared with `hmac.compare_digest()` on every `/trigger` POST --
    sufficient for a localhost, single-operator tool (no need for Flask-WTF/
    sessions). An `Origin`/`Referer` header, WHEN PRESENT, must match this app's
    own origin -- a forged cross-site POST (which usually DOES carry an Origin)
    is rejected; a same-origin form POST (which may omit both headers entirely,
    depending on the browser) is not penalized for their absence.
  - **Repetitions clamp (security M3).** Server-side clamp to `[1, 20]`
    regardless of what the form claims -- this route spends real money per
    repetition.
  - **Dirty-declaration guard, UI-surfaced (reviewer Medium, item 2).** The SAME
    check `run.py` itself fails loud on (`harness.run_store.candidate_declaration_is_dirty()`)
    is run here too, BEFORE launching the subprocess, so a dirty candidate
    directory gets a clean 400 on the request instead of a silent failure buried
    in `subprocess.log`. No `--allow-dirty` escape hatch is exposed from the UI
    at all (per the task's explicit instruction) -- only the CLI can override it.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
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

import bleach
import markdown
from candidate_sync.loader import CandidateLoadError, load_candidate  # noqa: E402
from eval_core.record import V1_CRITERIA  # noqa: E402
from flask import Flask, redirect, render_template, request, url_for  # noqa: E402

from harness import git_show, run_store  # noqa: E402

app = Flask(__name__)

# A per-PROCESS CSRF synchronizer token (review-fix, security M3) -- regenerated
# every time the UI process (re)starts, which is exactly the lifetime a
# localhost, single-operator tool needs: the form embeds it, /trigger checks it.
_CSRF_TOKEN = secrets.token_urlsafe(32)

# The Markdown->HTML render's post-conversion sanitization allowlist (review-fix,
# security M1) -- deliberately small: everything a daily-brief Markdown body
# plausibly needs (headings, paragraphs, lists, emphasis, links, quotes, code,
# rules) and nothing that can execute (no <script>, no event-handler attributes,
# no <iframe>/<object>/<style>).
_BLEACH_ALLOWED_TAGS = [
    "p", "h1", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "a",
    "blockquote", "code", "pre", "hr", "br",
]
_BLEACH_ALLOWED_ATTRIBUTES = {"a": ["href"]}


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


@app.route("/favicon.ico")
def favicon():
    # No favicon asset -- a bare 204 silences the browser console's inevitable
    # 404 without adding a static file for a debugging-only local tool.
    return "", 204


MAX_REPETITIONS = 20


def _render_conduct(*, error: str | None = None, status: int = 200):
    """Shared render for the Conduct page and every `/trigger` error path -- keeps
    the CSRF token wiring in ONE place rather than repeated at every call site."""
    return (
        render_template(
            "conduct.html",
            candidates=list_candidate_options(),
            criteria=list(V1_CRITERIA),
            csrf_token=_CSRF_TOKEN,
            error=error,
        ),
        status,
    )


@app.route("/conduct", methods=["GET"])
def conduct():
    return _render_conduct()


def _is_valid_csrf_token(submitted: str | None) -> bool:
    return bool(submitted) and hmac.compare_digest(submitted, _CSRF_TOKEN)


def _origin_is_local_or_absent() -> bool:
    """True unless a POST carries an `Origin`/`Referer` header that does NOT
    match this app's own origin (review-fix, security M3). A same-origin form
    POST may omit both headers entirely depending on the browser -- absence is
    NOT penalized, only a MISMATCHING value is rejected (the CSRF token check
    above is the primary defense; this is a second, independent signal)."""
    own_origin = request.host_url.rstrip("/")
    header_value = request.headers.get("Origin") or request.headers.get("Referer")
    if not header_value:
        return True
    return header_value.rstrip("/").startswith(own_origin)


@app.route("/trigger", methods=["POST"])
def trigger():
    # --- Security checks, in order, BEFORE any path construction or subprocess
    # launch (review-fix: security M2/M3) ------------------------------------
    if not _is_valid_csrf_token(request.form.get("csrf_token")):
        return _render_conduct(error="Invalid or missing CSRF token -- reload the Conduct page and try again.", status=400)

    if not _origin_is_local_or_absent():
        return _render_conduct(error="Request origin not allowed.", status=400)

    candidate_slug = request.form.get("candidate_slug", "").strip()
    run_name = request.form.get("name", "").strip() or f"{candidate_slug} run"
    criteria = request.form.getlist("criteria")
    try:
        repetitions = int(request.form.get("repetitions", "1") or "1")
    except ValueError:
        repetitions = 1
    # Server-side clamp regardless of what the form claims -- this route spends
    # real money per repetition (review-fix, security M3).
    repetitions = max(1, min(repetitions, MAX_REPETITIONS))

    # The email toggle is rendered DISABLED in the form (ADR-0016 D3: deferred) --
    # even if a form submission somehow carried it, this handler never reads it
    # and never passes --email to the subprocess. No path from this route ever
    # sends an eval email.

    if not candidate_slug:
        return _render_conduct(error="Select a candidate before triggering a run.", status=400)

    # Slug allowlist (review-fix, security M2): must be a REAL candidate --
    # rejects path traversal ("../../tmp/evil") and flag-looking values
    # ("--check-pricing-drift") alike, since neither is ever a real directory
    # name under deploy/candidates/. Checked BEFORE any path is built or any
    # subprocess is launched.
    valid_slugs = {c["slug"] for c in list_candidate_options()}
    if candidate_slug not in valid_slugs:
        return _render_conduct(error="Unknown candidate.", status=400)

    if not criteria:
        return _render_conduct(error="Select at least one criterion to judge.", status=400)

    eval_run_id, error = _trigger_run(candidate_slug, run_name, repetitions, criteria)
    if error:
        return _render_conduct(error=error, status=400)

    return redirect(url_for("run_detail", slug=candidate_slug, eval_run_id=eval_run_id))


def _trigger_run(candidate_slug: str, run_name: str, repetitions: int, criteria: list[str]) -> tuple[str | None, str | None]:
    """Launch `run.py` as a background subprocess (non-blocking -- a real
    multi-agent run takes ~15 minutes, and this request must return immediately per
    ADR-0016 D5) and return `(eval_run_id, None)`, where `eval_run_id` is
    computed with the SAME `make_eval_run_id()` this route uses so the redirect
    target is known up front, before the subprocess has done anything. Returns
    `(None, <error message>)` -- and launches NOTHING -- if the candidate
    declaration is dirty (review-fix, reviewer Medium: no `--allow-dirty` escape
    hatch is exposed from the UI, per the task's explicit instruction) or the
    Anthropic API key file is unreadable.

    `candidate_slug` has ALREADY been checked against the real candidate
    allowlist by the caller (`trigger()`) -- this function additionally puts it
    LAST in the subprocess argv, after a bare `--`, so `run.py`'s own argparse
    can never misinterpret it as a flag either (defense in depth)."""
    candidate_dir = CANDIDATES_DIR / candidate_slug
    if run_store.candidate_declaration_is_dirty(candidate_dir):
        return None, (
            f"Candidate '{candidate_slug}' has uncommitted changes under deploy/candidates/{candidate_slug} -- "
            "commit them first so the recorded git ref matches what will actually run."
        )

    if not ANTHROPIC_API_KEY_FILE.is_file():
        return None, f"Could not read the Anthropic API key from {ANTHROPIC_API_KEY_FILE} -- place your key there before triggering a run."
    api_key = ANTHROPIC_API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not api_key:
        return None, f"The Anthropic API key file at {ANTHROPIC_API_KEY_FILE} is empty."

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
        "--name",
        run_name,
        "--repetitions",
        str(repetitions),
        "--criteria",
        ",".join(criteria),
        "--",
        candidate_slug,
    ]
    _launch_run_subprocess(command, env=env, log_path=log_path)

    return eval_run_id, None


def _launch_run_subprocess(command: list[str], *, env: dict[str, str], log_path: Path) -> None:
    """The actual `subprocess.Popen(...)` call, factored out into its own
    function SPECIFICALLY so tests can monkeypatch it directly (`ui_app._launch_run_subprocess`)
    instead of the shared, process-global `subprocess.Popen` symbol -- monkeypatching
    that shared symbol would ALSO intercept `harness.run_store.candidate_declaration_is_dirty()`'s
    own internal `subprocess.run(["git", ...])` call (both modules import the
    SAME `subprocess` module object), breaking the dirty-check in any test that
    fakes out the run.py launch."""
    with open(log_path, "wb") as log_file:
        subprocess.Popen(command, cwd=str(HARNESS_DIR), env=env, stdout=log_file, stderr=subprocess.STDOUT)


@app.route("/assess")
def assess():
    rows = [_load_run_row(run_dir) for run_dir in run_store.list_eval_runs()]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return render_template("assess.html", rows=rows, criteria=list(V1_CRITERIA))


@app.route("/runs/<slug>/<eval_run_id>")
def run_detail(slug: str, eval_run_id: str):
    # Explicit containment check (review-fix, security L2, defense-in-depth):
    # Flask's default `string` route converter allows ".." as a literal path
    # SEGMENT (it only excludes "/"), so `eval_run_id=".."` is a syntactically
    # valid single segment. `eval-run.json`'s existence check below already
    # makes this practically unreachable (`RUNS_ROOT/../eval-run.json` doesn't
    # exist), but resolving and confirming containment under RUNS_ROOT first
    # makes that guarantee explicit rather than incidental.
    run_dir = run_store.eval_run_dir(slug, eval_run_id).resolve()
    runs_root = run_store.RUNS_ROOT.resolve()
    if not run_dir.is_relative_to(runs_root):
        return render_template("not_found.html", slug=slug, eval_run_id=eval_run_id), 404

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
    harness).

    Review-fix (security M1): `markdown.markdown()` alone passes raw HTML
    embedded in the source Markdown straight through -- a brief containing
    `<script>...</script>` or an `onerror=` attribute (LLM output describing
    third-party web content, never assumed inert) would otherwise render as LIVE
    HTML/JS in this page. `bleach.clean(..., strip=True)` removes anything
    outside `_BLEACH_ALLOWED_TAGS`/`_BLEACH_ALLOWED_ATTRIBUTES` -- the result is
    what `run_detail.html` marks `| safe`, never the raw `markdown.markdown()`
    output."""
    if not text:
        return ""
    html = markdown.markdown(text)
    return bleach.clean(html, tags=_BLEACH_ALLOWED_TAGS, attributes=_BLEACH_ALLOWED_ATTRIBUTES, strip=True)


def main() -> None:
    # 127.0.0.1 EXPLICITLY -- never 0.0.0.0 (PRD/ADR: "no auth needed... bind to
    # 127.0.0.1 explicitly").
    app.run(host="127.0.0.1", port=5151, debug=False)


if __name__ == "__main__":
    main()
