"""Read/write the per-eval-run directory structure (ADR-0016 D4):

```
runs/<candidate-slug>/<eval-run-id>/
  eval-run.json          run-level metadata + state (see EvalRunMeta)
  repetitions/
    01/
      artifacts/          AI Brief - <date>.md, listening-script.txt,
                          candidates.json, source-usage.json
      threads-usage.json  per-thread {role, agent_id, model, usage{...}}
      cost.json           per-thread + total (harness.cost.SessionCostBreakdown)
      scores.json         per-criterion {score, rationale, evidence, insufficient_data}
      run-meta.json       deployment_id, session_id, thread_count, final_status, timestamp
      events.json         GITIGNORED -- the full raw trace
    02/ ...
  summary.json            aggregate across repetitions (eval_core.record.aggregate_replicates)
  human-eval.md           optional free-form owner assessment
```

Every write here is a plain filesystem operation -- this module never runs `git
add`/`git commit` (mirrors `candidate_sync/writer.py`'s own explicit choice: the
operator reviews and commits the resulting diff, exactly like any other
generated-but-reviewed change in this repo).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

RUNS_ROOT = Path(__file__).resolve().parent.parent / "runs"

# The four run states (PRD §4.1 / ADR-0016 D4).
STATE_CONFIGURED = "configured"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"


def current_git_ref(cwd: Path | None = None) -> str:
    """`git rev-parse HEAD` -- the repo-wide commit this eval run was triggered
    against (D4: "git ref of the declaration at trigger time"). Run from inside
    this file's own directory by default (any path inside the working tree
    resolves the same HEAD)."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(cwd or Path(__file__).resolve().parent),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def slugify(text: str) -> str:
    """A filesystem/URL-safe slug for an eval-run NAME, used only as a readability
    aid inside the generated eval-run-id (uniqueness comes from the timestamp
    prefix, not this slug -- two runs given the same name never collide)."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "run"


def make_eval_run_id(name: str, *, now: float | None = None) -> str:
    """`<timestamp>-<slugified-name>`, mirroring
    `deploy/candidates/runs/<slug>/<timestamp>/`'s own naming convention (e.g. the
    real spike dir `2026-07-07-142718`)."""
    timestamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(now))
    return f"{timestamp}-{slugify(name)}"


def eval_run_dir(slug: str, eval_run_id: str, *, runs_root: Path | None = None) -> Path:
    return (runs_root or RUNS_ROOT) / slug / eval_run_id


def repetition_dir(run_dir: Path, index: int) -> Path:
    """`repetitions/<NN>/`, 1-indexed, zero-padded to 2 digits (matches the ADR's
    own `01/`, `02/` example layout)."""
    return run_dir / "repetitions" / f"{index:02d}"


# --- eval-run.json ---------------------------------------------------------------


@dataclass
class EvalRunMeta:
    """Run-level metadata -- `eval-run.json`'s full content (ADR-0016 D4)."""

    name: str
    slug: str
    agent_id: str | None
    git_ref: str
    composition: str  # "single-agent" | "multi-agent"
    models: list[str]
    parameters: dict[str, Any]
    repetitions: int
    criteria: list[str]
    state: str
    email_sent: bool
    is_production_config: bool
    created_at: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalRunMeta":
        return cls(**data)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def write_eval_run_meta(run_dir: Path, meta: EvalRunMeta) -> None:
    _write_json(run_dir / "eval-run.json", meta.to_dict())


def read_eval_run_meta(run_dir: Path) -> EvalRunMeta:
    data = json.loads((run_dir / "eval-run.json").read_text(encoding="utf-8"))
    return EvalRunMeta.from_dict(data)


def update_state(run_dir: Path, state: str) -> None:
    """Read-modify-write `eval-run.json`'s `state` field only -- used by `run.py`
    to progress configured -> running -> completed/failed as the CLI advances, so
    the Flask UI (polling the same file) can show live state without the CLI
    process needing any IPC beyond the filesystem."""
    meta = read_eval_run_meta(run_dir)
    meta.state = state
    write_eval_run_meta(run_dir, meta)


# --- per-repetition artifacts / cost / scores / run-meta --------------------------


def write_artifacts(run_dir: Path, index: int, artifacts: dict[str, str]) -> None:
    """`artifacts` maps a recovered file's PATH (as it was `cat`'d inside the
    sandbox, e.g. `/workspace/AI Brief - 2026-07-07.md`) to its content -- written
    under `repetitions/<NN>/artifacts/` using just the basename."""
    artifacts_dir = repetition_dir(run_dir, index) / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for path, content in artifacts.items():
        filename = Path(path).name
        (artifacts_dir / filename).write_text(content, encoding="utf-8")


def read_artifacts(run_dir: Path, index: int) -> dict[str, str]:
    artifacts_dir = repetition_dir(run_dir, index) / "artifacts"
    if not artifacts_dir.is_dir():
        return {}
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(artifacts_dir.iterdir()) if p.is_file()}


def write_threads_usage(run_dir: Path, index: int, breakdown: Any) -> None:
    """`breakdown` is a `harness.cost.SessionCostBreakdown` -- written twice, once
    as `threads-usage.json` (the D4-named artifact) and once as `cost.json` (this
    module keeps them as separate files per the ADR's layout, even though today
    they share the same source object, so a future change to one need not disturb
    the other)."""
    _write_json(repetition_dir(run_dir, index) / "threads-usage.json", breakdown.to_dict())


def write_cost(run_dir: Path, index: int, breakdown: Any) -> None:
    _write_json(repetition_dir(run_dir, index) / "cost.json", breakdown.to_dict())


def write_scores(run_dir: Path, index: int, scores: dict[str, dict[str, Any]]) -> None:
    _write_json(repetition_dir(run_dir, index) / "scores.json", scores)


def read_scores(run_dir: Path, index: int) -> dict[str, dict[str, Any]]:
    path = repetition_dir(run_dir, index) / "scores.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_run_meta(run_dir: Path, index: int, meta: dict[str, Any]) -> None:
    _write_json(repetition_dir(run_dir, index) / "run-meta.json", meta)


def write_events(run_dir: Path, index: int, events: list[dict[str, Any]]) -> None:
    """GITIGNORED (`runs/**/events.json`, see .gitignore) -- the full raw trace,
    written anyway so a local deep dive can still read it before Platform garbage-
    collects the session (ADR-0016 open item 3)."""
    _write_json(repetition_dir(run_dir, index) / "events.json", {"events": events})


def write_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    _write_json(run_dir / "summary.json", summary)


def write_human_eval_placeholder(run_dir: Path) -> None:
    """`human-eval.md` -- an empty, git-tracked placeholder the owner can fill in
    by hand later (D4: "optional free-form owner assessment"). Only written if it
    doesn't already exist, so re-running never clobbers a human's notes."""
    path = run_dir / "human-eval.md"
    if not path.exists():
        path.write_text("<!-- Optional free-form human assessment of this eval run. -->\n", encoding="utf-8")


def list_eval_runs(*, runs_root: Path | None = None) -> list[Path]:
    """Every `runs/<slug>/<eval-run-id>/` directory that has an `eval-run.json` --
    the Flask UI's "Assess" table iterates this."""
    root = runs_root or RUNS_ROOT
    if not root.is_dir():
        return []
    return sorted(
        p.parent for p in root.glob("*/*/eval-run.json")
    )


__all__ = [
    "RUNS_ROOT",
    "STATE_CONFIGURED",
    "STATE_RUNNING",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "current_git_ref",
    "slugify",
    "make_eval_run_id",
    "eval_run_dir",
    "repetition_dir",
    "EvalRunMeta",
    "write_eval_run_meta",
    "read_eval_run_meta",
    "update_state",
    "write_artifacts",
    "read_artifacts",
    "write_threads_usage",
    "write_cost",
    "write_scores",
    "read_scores",
    "write_run_meta",
    "write_events",
    "write_summary",
    "write_human_eval_placeholder",
    "list_eval_runs",
]
