"""Unit tests for harness/run_store.py (ADR-0016 D4's per-eval-run directory)."""

from __future__ import annotations

from pathlib import Path

from harness import run_store


def test_current_git_ref_returns_a_real_commit_sha(tmp_path):
    """Read-only local `git rev-parse HEAD` against this actual repo -- no network,
    no mutation, safe to run for real."""
    ref = run_store.current_git_ref()
    assert len(ref) == 40
    assert all(c in "0123456789abcdef" for c in ref)


def test_slugify_lowercases_and_hyphenates():
    assert run_store.slugify("Baseline Sanity Check!") == "baseline-sanity-check"


def test_slugify_falls_back_to_run_for_an_empty_or_symbol_only_name():
    assert run_store.slugify("...") == "run"
    assert run_store.slugify("") == "run"


def test_make_eval_run_id_embeds_a_timestamp_and_the_slug():
    eval_run_id = run_store.make_eval_run_id("Haiku Swap Quick Look", now=1783427240)
    assert eval_run_id.endswith("haiku-swap-quick-look")
    # timestamp prefix is 17 chars: YYYY-MM-DD-HHMMSS
    assert eval_run_id[17] == "-"


def test_eval_run_dir_and_repetition_dir_layout(tmp_path):
    run_dir = run_store.eval_run_dir("production-baseline", "2026-07-07-142718-test", runs_root=tmp_path)
    assert run_dir == tmp_path / "production-baseline" / "2026-07-07-142718-test"
    assert run_store.repetition_dir(run_dir, 1) == run_dir / "repetitions" / "01"
    assert run_store.repetition_dir(run_dir, 12) == run_dir / "repetitions" / "12"


def _make_meta(**overrides) -> run_store.EvalRunMeta:
    base = dict(
        name="a run",
        slug="production-baseline",
        agent_id="agent_x",
        git_ref="a" * 40,
        composition="single-agent",
        models=["claude-sonnet-5"],
        parameters={"agent": {}, "sub_agents": []},
        repetitions=1,
        criteria=["content_selection"],
        state=run_store.STATE_CONFIGURED,
        email_sent=False,
        is_production_config=True,
        created_at=1000,
    )
    base.update(overrides)
    return run_store.EvalRunMeta(**base)


def test_eval_run_meta_round_trips_through_write_read(tmp_path):
    run_dir = tmp_path / "run"
    meta = _make_meta()

    run_store.write_eval_run_meta(run_dir, meta)
    restored = run_store.read_eval_run_meta(run_dir)

    assert restored == meta
    assert (run_dir / "eval-run.json").is_file()


def test_update_state_only_changes_the_state_field(tmp_path):
    run_dir = tmp_path / "run"
    run_store.write_eval_run_meta(run_dir, _make_meta(state=run_store.STATE_CONFIGURED))

    run_store.update_state(run_dir, run_store.STATE_RUNNING)

    restored = run_store.read_eval_run_meta(run_dir)
    assert restored.state == run_store.STATE_RUNNING
    assert restored.name == "a run"  # everything else untouched


def test_write_and_read_artifacts_uses_the_basename_of_the_catted_path(tmp_path):
    run_dir = tmp_path / "run"
    artifacts = {
        "/workspace/AI Brief - 2026-07-07.md": "# Daily AI Brief\n",
        "/workspace/listening-script.txt": "Hello.",
    }

    run_store.write_artifacts(run_dir, 1, artifacts)
    restored = run_store.read_artifacts(run_dir, 1)

    assert restored == {
        "AI Brief - 2026-07-07.md": "# Daily AI Brief\n",
        "listening-script.txt": "Hello.",
    }


def test_read_artifacts_returns_empty_dict_when_no_repetition_exists(tmp_path):
    assert run_store.read_artifacts(tmp_path / "nonexistent", 1) == {}


def test_write_and_read_scores_round_trip(tmp_path):
    run_dir = tmp_path / "run"
    scores = {"content_selection": {"score": 4, "rationale": "r", "evidence": "e", "insufficient_data": False}}

    run_store.write_scores(run_dir, 1, scores)

    assert run_store.read_scores(run_dir, 1) == scores


def test_read_scores_returns_empty_dict_when_missing(tmp_path):
    assert run_store.read_scores(tmp_path / "nonexistent", 1) == {}


def test_write_human_eval_placeholder_never_clobbers_an_existing_file(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "human-eval.md").write_text("the owner's real notes", encoding="utf-8")

    run_store.write_human_eval_placeholder(run_dir)

    assert (run_dir / "human-eval.md").read_text(encoding="utf-8") == "the owner's real notes"


def test_list_eval_runs_finds_every_run_with_an_eval_run_json(tmp_path):
    run_store.write_eval_run_meta(tmp_path / "slug-a" / "run-1", _make_meta(slug="slug-a"))
    run_store.write_eval_run_meta(tmp_path / "slug-b" / "run-1", _make_meta(slug="slug-b"))
    # A stray directory with no eval-run.json must be ignored.
    (tmp_path / "slug-c" / "not-a-run").mkdir(parents=True)

    runs = run_store.list_eval_runs(runs_root=tmp_path)

    assert set(runs) == {tmp_path / "slug-a" / "run-1", tmp_path / "slug-b" / "run-1"}


def test_list_eval_runs_returns_empty_list_for_a_missing_runs_root(tmp_path):
    assert run_store.list_eval_runs(runs_root=tmp_path / "does-not-exist") == []
