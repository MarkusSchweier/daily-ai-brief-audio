"""Tests for run.py's CLI `main()` -- the trigger/retrieve/record eval-run flow
(ADR-0016 D4). Mirrors `deploy/candidates/tests/test_trigger_cli.py`'s pattern:
monkeypatch `candidate_sync.trigger.run_candidate` itself (rather than faking HTTP)
so no real network call or wall-clock wait is ever made, and additionally
monkeypatch `run._build_anthropic_client` to inject a fake judge client (this
repo's established `FakeAnthropicClient`/`make_fake_client` double, via conftest).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import run as run_cli
from candidate_sync import trigger as trigger_module
from candidate_sync.loader import AgentDeclaration, CandidateDeclaration

from conftest import git_init_and_commit, make_fake_client


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _build_candidate_dir(candidates_dir: Path, slug: str, *, agent_id: str | None = "agent_test") -> Path:
    candidate_dir = candidates_dir / slug
    candidate_dir.mkdir(parents=True)
    candidate_json = {"slug": slug, "composition": "single-agent"}
    if agent_id is not None:
        candidate_json["agent_id"] = agent_id
    _write_json(candidate_dir / "candidate.json", candidate_json)
    _write_json(candidate_dir / "agent.json", {"name": f"{slug}-agent", "description": "a test candidate"})
    (candidate_dir / "model.txt").write_text("claude-sonnet-5", encoding="utf-8")
    (candidate_dir / "system-prompt.md").write_text("You are a test agent.", encoding="utf-8")
    (candidate_dir / "task-prompt.md").write_text("Write the brief and cat it out.", encoding="utf-8")
    _write_json(candidate_dir / "skills.json", [])
    _write_json(candidate_dir / "parameters.json", {})
    return candidate_dir


def _setup_env(monkeypatch, tmp_path: Path, *, slug: str, agent_id: str | None = "agent_test", commit: bool = True) -> Path:
    """`commit=True` (the default) makes `candidates_dir` a real, freshly-committed
    git repo -- required for `run.py`'s dirty-working-tree guard
    (`harness.run_store.candidate_declaration_is_dirty()`) to behave correctly
    against this synthetic fixture, exactly as it would against the real
    `deploy/candidates/` tree (always inside THIS repo's own git history).
    `commit=False` is for tests that specifically want an uncommitted/dirty state."""
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    _write_json(candidates_dir / "environment.json", {"environment_id": "env_fake"})
    _build_candidate_dir(candidates_dir, slug, agent_id=agent_id)
    if commit:
        git_init_and_commit(candidates_dir)

    monkeypatch.setattr(run_cli, "CANDIDATES_DIR", candidates_dir)
    monkeypatch.setattr(run_cli, "_ENVIRONMENT_JSON_PATH", candidates_dir / "environment.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-cli-test")
    return candidates_dir


def _cat_events(tool_use_id: str, path: str, content: str) -> list[dict]:
    """Synthesize the two events `fetch_catted_file_contents()` looks for -- a
    `bash` tool_use running `cat "<path>"`, and the matching tool_result echoing
    `content` back, per the confirmed live event shape."""
    return [
        {"type": "agent.tool_use", "id": tool_use_id, "name": "bash", "input": {"command": f'cat "{path}"'}},
        {"type": "agent.tool_result", "tool_use_id": tool_use_id, "content": [{"type": "text", "text": content}]},
    ]


def _fake_run_result(brief_markdown: str, listening_script: str) -> trigger_module.CandidateRunResult:
    events = [
        *_cat_events("t1", "/workspace/AI Brief - 2026-07-07.md", brief_markdown),
        *_cat_events("t2", "/workspace/listening-script.txt", listening_script),
    ]
    return trigger_module.CandidateRunResult(
        deployment_id="depl_fake", session_id="sesn_fake", final_status="idle", events=events
    )


def _fake_threads() -> list[dict]:
    return [
        {
            "id": "sthr_1",
            "parent_thread_id": None,
            "created_at": "2026-07-07T00:00:00Z",
            "agent": {"id": "agent_test", "name": "test-candidate-agent", "description": "", "model": {"id": "claude-sonnet-5"}},
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            },
        }
    ]


def _score_response(score: int = 4) -> str:
    return json.dumps({"score": score, "rationale": "fine", "evidence": "n/a", "insufficient_data": False})


def test_run_completes_a_single_repetition_and_writes_the_full_directory(tmp_path, monkeypatch):
    _setup_env(monkeypatch, tmp_path, slug="test-candidate")
    runs_root = tmp_path / "runs"

    monkeypatch.setattr(
        trigger_module,
        "run_candidate",
        lambda client, **kwargs: _fake_run_result("# Daily AI Brief\n\nSome content.", "Hello, listeners."),
    )
    monkeypatch.setattr(run_cli.cost, "fetch_threads", lambda client, session_id: _fake_threads())
    monkeypatch.setattr(run_cli, "_build_anthropic_client", lambda api_key: make_fake_client(_score_response(4)))

    exit_code = run_cli.main(
        [
            "test-candidate",
            "--name",
            "a real test run",
            "--repetitions",
            "1",
            "--criteria",
            "factual_accuracy",
            "--runs-root",
            str(runs_root),
        ]
    )

    assert exit_code == 0

    run_dirs = list((runs_root / "test-candidate").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    meta = json.loads((run_dir / "eval-run.json").read_text())
    assert meta["state"] == "completed"
    assert meta["name"] == "a real test run"
    assert meta["slug"] == "test-candidate"
    assert meta["agent_id"] == "agent_test"
    assert len(meta["git_ref"]) == 40
    assert meta["composition"] == "single-agent"
    assert meta["models"] == ["claude-sonnet-5"]
    assert meta["criteria"] == ["factual_accuracy"]
    assert meta["is_production_config"] is False
    assert meta["email_sent"] is False

    rep_dir = run_dir / "repetitions" / "01"
    artifacts = {p.name for p in (rep_dir / "artifacts").iterdir()}
    assert artifacts == {"AI Brief - 2026-07-07.md", "listening-script.txt"}

    scores = json.loads((rep_dir / "scores.json").read_text())
    assert scores["factual_accuracy"]["score"] == 4
    assert "content_selection" not in scores  # not in the selected criteria subset

    cost_data = json.loads((rep_dir / "cost.json").read_text())
    assert cost_data["total_cost_usd"] > 0

    run_meta = json.loads((rep_dir / "run-meta.json").read_text())
    assert run_meta["session_id"] == "sesn_fake"
    assert run_meta["final_status"] == "idle"

    assert (rep_dir / "events.json").is_file()  # written locally; gitignored, not asserted absent here

    # Judge cost (review-fix: ADR-0016 reviewer Medium, "judge cost accounting") --
    # a SIBLING file to cost.json, never folded into pipeline cost.
    judge_cost = json.loads((rep_dir / "judge-cost.json").read_text())
    assert judge_cost["model"] == "claude-haiku-4-5"
    assert judge_cost["total_cost_usd"] > 0
    assert set(judge_cost["per_criterion"]) == {"factual_accuracy"}
    assert judge_cost["total_cost_usd"] != cost_data["total_cost_usd"]  # never confused with pipeline cost

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["criterion_aggregates"]["factual_accuracy"]["mean"] == 4.0
    assert summary["judge_cost"]["n"] == 1
    assert summary["judge_cost"]["mean_cost_usd"] == judge_cost["total_cost_usd"]
    assert summary["judge_cost"]["stdev_cost_usd"] is None  # undefined for n=1

    assert (run_dir / "human-eval.md").is_file()


def test_run_marks_the_production_baseline_slug_as_the_production_config(tmp_path, monkeypatch):
    _setup_env(monkeypatch, tmp_path, slug="production-baseline")
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(
        trigger_module, "run_candidate", lambda client, **kwargs: _fake_run_result("# Brief", "Script.")
    )
    monkeypatch.setattr(run_cli.cost, "fetch_threads", lambda client, session_id: _fake_threads())
    monkeypatch.setattr(run_cli, "_build_anthropic_client", lambda api_key: make_fake_client(_score_response(5)))

    run_cli.main(["production-baseline", "--criteria", "factual_accuracy", "--runs-root", str(runs_root)])

    run_dir = next((runs_root / "production-baseline").iterdir())
    meta = json.loads((run_dir / "eval-run.json").read_text())
    assert meta["is_production_config"] is True


def test_a_failed_repetition_marks_the_run_failed_and_records_the_error(tmp_path, monkeypatch):
    _setup_env(monkeypatch, tmp_path, slug="test-candidate")
    runs_root = tmp_path / "runs"

    def _raise(*args, **kwargs):
        raise trigger_module.CandidateRunFailedError("session sesn_x failed with status 'failed'")

    monkeypatch.setattr(trigger_module, "run_candidate", _raise)
    monkeypatch.setattr(run_cli, "_build_anthropic_client", lambda api_key: make_fake_client())

    exit_code = run_cli.main(
        ["test-candidate", "--criteria", "factual_accuracy", "--runs-root", str(runs_root)]
    )

    assert exit_code == 1
    run_dir = next((runs_root / "test-candidate").iterdir())
    meta = json.loads((run_dir / "eval-run.json").read_text())
    assert meta["state"] == "failed"

    run_meta = json.loads((run_dir / "repetitions" / "01" / "run-meta.json").read_text())
    assert "failed" in run_meta["error"]
    assert not (run_dir / "repetitions" / "01" / "scores.json").exists()
    assert not (run_dir / "summary.json").exists()


# --- Dirty-working-tree guard (review-fix: reviewer Medium) ---------------------------


def test_a_dirty_candidate_directory_fails_loud_before_any_trigger_attempt(tmp_path, monkeypatch, capsys):
    _setup_env(monkeypatch, tmp_path, slug="test-candidate")
    candidate_dir = tmp_path / "candidates" / "test-candidate"
    (candidate_dir / "task-prompt.md").write_text("An UNCOMMITTED edit.", encoding="utf-8")
    runs_root = tmp_path / "runs"

    triggered = {"called": False}
    monkeypatch.setattr(trigger_module, "run_candidate", lambda *a, **k: triggered.__setitem__("called", True))

    exit_code = run_cli.main(["test-candidate", "--criteria", "factual_accuracy", "--runs-root", str(runs_root)])

    assert exit_code == 1
    assert "uncommitted changes" in capsys.readouterr().err
    assert triggered["called"] is False
    assert not (runs_root / "test-candidate").exists()  # no eval-run.json ever written


def test_allow_dirty_proceeds_and_marks_the_record(tmp_path, monkeypatch):
    _setup_env(monkeypatch, tmp_path, slug="test-candidate")
    candidate_dir = tmp_path / "candidates" / "test-candidate"
    (candidate_dir / "task-prompt.md").write_text("An UNCOMMITTED edit, but --allow-dirty is set.", encoding="utf-8")
    runs_root = tmp_path / "runs"

    monkeypatch.setattr(
        trigger_module, "run_candidate", lambda client, **kwargs: _fake_run_result("# Brief", "Script.")
    )
    monkeypatch.setattr(run_cli.cost, "fetch_threads", lambda client, session_id: _fake_threads())
    monkeypatch.setattr(run_cli, "_build_anthropic_client", lambda api_key: make_fake_client(_score_response(4)))

    exit_code = run_cli.main(
        ["test-candidate", "--criteria", "factual_accuracy", "--allow-dirty", "--runs-root", str(runs_root)]
    )

    assert exit_code == 0
    run_dir = next((runs_root / "test-candidate").iterdir())
    meta = json.loads((run_dir / "eval-run.json").read_text())
    assert meta["declaration_dirty"] is True


def test_a_clean_candidate_directory_needs_no_allow_dirty_flag(tmp_path, monkeypatch):
    """The common case -- a freshly committed candidate -- must not require
    --allow-dirty at all."""
    _setup_env(monkeypatch, tmp_path, slug="test-candidate")
    runs_root = tmp_path / "runs"

    monkeypatch.setattr(
        trigger_module, "run_candidate", lambda client, **kwargs: _fake_run_result("# Brief", "Script.")
    )
    monkeypatch.setattr(run_cli.cost, "fetch_threads", lambda client, session_id: _fake_threads())
    monkeypatch.setattr(run_cli, "_build_anthropic_client", lambda api_key: make_fake_client(_score_response(4)))

    exit_code = run_cli.main(["test-candidate", "--criteria", "factual_accuracy", "--runs-root", str(runs_root)])

    assert exit_code == 0
    run_dir = next((runs_root / "test-candidate").iterdir())
    meta = json.loads((run_dir / "eval-run.json").read_text())
    assert meta["declaration_dirty"] is False


def test_missing_agent_id_fails_loud_before_any_trigger_attempt(tmp_path, monkeypatch, capsys):
    _setup_env(monkeypatch, tmp_path, slug="no-agent-id-candidate", agent_id=None)
    runs_root = tmp_path / "runs"

    exit_code = run_cli.main(["no-agent-id-candidate", "--runs-root", str(runs_root)])

    assert exit_code == 1
    assert "sync.py" in capsys.readouterr().err


def test_missing_api_key_fails_loud(tmp_path, monkeypatch, capsys):
    _setup_env(monkeypatch, tmp_path, slug="test-candidate")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    exit_code = run_cli.main(["test-candidate"])

    assert exit_code == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_email_flag_is_a_deferred_stub_and_never_reaches_trigger(monkeypatch, capsys):
    called = {"triggered": False}
    monkeypatch.setattr(trigger_module, "run_candidate", lambda *a, **k: called.__setitem__("triggered", True))

    exit_code = run_cli.main(["whatever-candidate", "--email"])

    assert exit_code == 1
    assert "deferred" in capsys.readouterr().err.lower()
    assert called["triggered"] is False


def test_check_pricing_drift_flag_needs_no_candidate_and_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    exit_code = run_cli.main(["--check-pricing-drift"])

    assert exit_code == 0


def test_unknown_criterion_is_rejected_before_any_trigger_attempt():
    with pytest.raises(SystemExit):
        run_cli.main(["some-candidate", "--criteria", "not-a-real-criterion"])


def test_candidate_slug_is_required_unless_check_pricing_drift_is_given():
    with pytest.raises(SystemExit):
        run_cli.main([])


# --- Small pure helpers ---------------------------------------------------------------


def _agent(name="", description="", model="claude-sonnet-5", agent_id=None, parameters=None) -> AgentDeclaration:
    return AgentDeclaration(
        name=name,
        description=description,
        model=model,
        system_prompt="",
        task_prompt="",
        tools=[],
        mcp_servers=[],
        skills=[],
        parameters=parameters or {},
        agent_id=agent_id,
    )


def test_is_production_config_true_for_the_production_baseline_slug():
    candidate = CandidateDeclaration(
        slug="production-baseline", directory=Path("."), candidate_json={}, agent=_agent()
    )
    assert run_cli._is_production_config(candidate) is True


def test_is_production_config_false_for_any_other_slug():
    candidate = CandidateDeclaration(slug="haiku-swap", directory=Path("."), candidate_json={}, agent=_agent())
    assert run_cli._is_production_config(candidate) is False


def test_is_production_config_explicit_flag_overrides_the_slug_heuristic():
    candidate = CandidateDeclaration(
        slug="haiku-swap", directory=Path("."), candidate_json={"is_production_config": True}, agent=_agent()
    )
    assert run_cli._is_production_config(candidate) is True


def test_declared_models_deduplicates_and_sorts_across_coordinator_and_sub_agents():
    candidate = CandidateDeclaration(
        slug="multi",
        directory=Path("."),
        candidate_json={},
        agent=_agent(model="claude-sonnet-5"),
        sub_agents=[_agent(model="claude-haiku-4-5-20251001"), _agent(model="claude-sonnet-5")],
    )
    assert run_cli._declared_models(candidate) == ["claude-haiku-4-5-20251001", "claude-sonnet-5"]


def test_extract_named_artifacts_picks_out_the_four_named_files():
    artifacts = {
        "AI Brief - 2026-07-07.md": "brief body",
        "listening-script.txt": "script body",
        "candidates.json": "[]",
        "source-usage.json": "{}",
        "AI Brief - 2026-07-05.md": "a PRIOR brief, never cat'd by a real task prompt but guarded anyway",
    }
    brief, script, candidates_json_raw, source_usage_raw = run_cli._extract_named_artifacts(artifacts)
    # The first match wins deterministically off dict insertion order -- production
    # task prompts only ever cat TODAY's brief (see task-prompt.md), so in real
    # usage there is never more than one "AI Brief*.md" entry to disambiguate.
    assert brief in ("brief body", "a PRIOR brief, never cat'd by a real task prompt but guarded anyway")
    assert script == "script body"
    assert candidates_json_raw == "[]"
    assert source_usage_raw == "{}"


def test_extract_named_artifacts_tolerates_missing_files():
    assert run_cli._extract_named_artifacts({}) == (None, None, None, None)


# --- _price_judge_results (review-fix: judge cost accounting) -------------------------


def test_price_judge_results_prices_every_criterion_against_the_judge_model():
    from datetime import date

    from eval_core.judges.base import JudgeResult

    judge_results = {
        "factual_accuracy": JudgeResult(
            criterion="factual_accuracy", score=4, rationale="r", evidence="e",
            usage={"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0, "cache_creation_5m_input_tokens": 0, "cache_creation_1h_input_tokens": 0},
        ),
        "content_selection": JudgeResult(
            criterion="content_selection", score=None, rationale="no artifact", evidence="", insufficient_data=True,
        ),  # never called the API -- default zero usage
    }
    pricing_table = run_cli.cost.load_pricing_table()

    result = run_cli._price_judge_results(judge_results, pricing_table=pricing_table, on_date=date(2026, 7, 7))

    assert result["model"] == "claude-haiku-4-5"
    assert result["per_criterion"]["factual_accuracy"]["cost_usd"] > 0
    assert result["per_criterion"]["content_selection"]["cost_usd"] == 0
    assert result["total_cost_usd"] == result["per_criterion"]["factual_accuracy"]["cost_usd"]
    assert result["total_usage"]["input_tokens"] == 1000


def test_price_judge_results_fails_loud_for_an_unrecognized_judge_model(monkeypatch):
    from datetime import date

    from eval_core.judges.base import JudgeResult

    monkeypatch.setattr(run_cli, "JUDGE_MODEL", "claude-some-future-judge-model-not-in-pricing-json")
    judge_results = {
        "factual_accuracy": JudgeResult(
            criterion="factual_accuracy", score=4, rationale="r", evidence="e",
            usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_5m_input_tokens": 0, "cache_creation_1h_input_tokens": 0},
        ),
    }
    pricing_table = run_cli.cost.load_pricing_table()

    with pytest.raises(run_cli.cost.UnknownModelPriceError):
        run_cli._price_judge_results(judge_results, pricing_table=pricing_table, on_date=date(2026, 7, 7))
