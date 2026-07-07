"""Tests for ui.py's Flask routes (ADR-0016 D5) -- uses Flask's own test client
(no real server bind, no real subprocess launch: `subprocess.Popen` is
monkeypatched so no `run.py` process is ever actually started by this suite)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import ui as ui_app
from harness import run_store


@pytest.fixture
def client():
    ui_app.app.config["TESTING"] = True
    return ui_app.app.test_client()


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _build_candidate_dir(candidates_dir: Path, slug: str, *, agent_id: str | None = "agent_test", multi=False) -> None:
    candidate_dir = candidates_dir / slug
    candidate_dir.mkdir(parents=True)
    candidate_json = {"slug": slug, "composition": "multi-agent" if multi else "single-agent"}
    if agent_id is not None:
        candidate_json["agent_id"] = agent_id
    _write_json(candidate_dir / "candidate.json", candidate_json)
    _write_json(candidate_dir / "agent.json", {"name": f"{slug}-agent", "description": "a test candidate"})
    (candidate_dir / "model.txt").write_text("claude-sonnet-5", encoding="utf-8")
    (candidate_dir / "system-prompt.md").write_text("You are a test agent.", encoding="utf-8")
    (candidate_dir / "task-prompt.md").write_text("Write the brief.", encoding="utf-8")
    _write_json(candidate_dir / "skills.json", [])
    _write_json(candidate_dir / "parameters.json", {})
    if multi:
        _write_json(
            candidate_dir / "multiagent.json",
            {
                "type": "coordinator",
                "agents": [
                    {
                        "entry": {"type": "custom"},
                        "name": f"{slug}-sub",
                        "description": "sub",
                        "model": "claude-haiku-4-5-20251001",
                        "system_prompt": "sub system prompt",
                        "task_prompt": "",
                        "tools": [],
                        "mcp_servers": [],
                        "skills": [],
                        "parameters": {},
                        "agent_id": "agent_sub",
                    }
                ],
            },
        )


def _setup_candidates_dir(monkeypatch, tmp_path: Path) -> Path:
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    _build_candidate_dir(candidates_dir, "test-candidate")
    monkeypatch.setattr(ui_app, "CANDIDATES_DIR", candidates_dir)
    return candidates_dir


def test_index_redirects_to_conduct(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/conduct")


def test_conduct_page_lists_candidates(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)

    response = client.get("/conduct")

    assert response.status_code == 200
    assert b"test-candidate" in response.data


def test_assess_page_renders_with_no_runs(client, monkeypatch, tmp_path):
    monkeypatch.setattr(run_store, "RUNS_ROOT", tmp_path / "runs")

    response = client.get("/assess")

    assert response.status_code == 200
    assert b"No eval runs yet" in response.data


def test_run_detail_404s_for_an_unknown_run(client, monkeypatch, tmp_path):
    monkeypatch.setattr(run_store, "RUNS_ROOT", tmp_path / "runs")

    response = client.get("/runs/some-slug/some-id")

    assert response.status_code == 404


def test_trigger_without_a_candidate_slug_is_a_400(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)

    response = client.post("/trigger", data={"repetitions": "1", "criteria": ["factual_accuracy"]})

    assert response.status_code == 400
    assert b"Select a candidate" in response.data


def test_trigger_without_an_api_key_file_is_a_400(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(ui_app, "ANTHROPIC_API_KEY_FILE", tmp_path / "does-not-exist.txt")

    response = client.post(
        "/trigger",
        data={"candidate_slug": "test-candidate", "repetitions": "1", "criteria": ["factual_accuracy"]},
    )

    assert response.status_code == 400
    assert b"Anthropic API key" in response.data


def test_trigger_launches_a_subprocess_and_redirects_to_the_run_detail_page(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(run_store, "RUNS_ROOT", tmp_path / "runs")

    api_key_file = tmp_path / "ant-api-key.txt"
    api_key_file.write_text("sk-ant-fake\n", encoding="utf-8")
    monkeypatch.setattr(ui_app, "ANTHROPIC_API_KEY_FILE", api_key_file)

    captured = {}

    class _FakePopen:
        def __init__(self, command, cwd=None, env=None, stdout=None, stderr=None):
            captured["command"] = command
            captured["env"] = env

    monkeypatch.setattr(ui_app.subprocess, "Popen", _FakePopen)

    response = client.post(
        "/trigger",
        data={"candidate_slug": "test-candidate", "name": "a ui-triggered run", "repetitions": "2", "criteria": ["factual_accuracy", "dedup"]},
    )

    assert response.status_code == 302
    assert "/runs/test-candidate/" in response.headers["Location"]

    # The Anthropic API key never leaks into the redirect location or response body.
    assert "sk-ant-fake" not in response.headers["Location"]

    command = captured["command"]
    assert "test-candidate" in command
    assert "--repetitions" in command and "2" in command
    assert "--email" not in command  # the deferred flag is never forwarded from the UI
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-ant-fake"


def test_render_markdown_converts_headings_and_returns_empty_string_for_none():
    html = ui_app.render_markdown("# Title\n\nBody text.")
    assert "<h1>Title</h1>" in html
    assert ui_app.render_markdown(None) == ""


def test_run_detail_renders_a_completed_run_end_to_end(client, monkeypatch, tmp_path):
    """Real end-to-end render of a fully-populated run directory, including a
    multi-agent candidate's git-historical declaration (read via the CURRENT repo's
    real HEAD, since deploy/candidates/multiagent-aggressive-haiku is a real,
    already-committed candidate -- no synthetic git fixture needed)."""
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(run_store, "RUNS_ROOT", runs_root)

    import subprocess

    real_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parent), capture_output=True, text=True, check=True
    ).stdout.strip()

    run_dir = run_store.eval_run_dir("multiagent-aggressive-haiku", "2026-07-07-999999-e2e", runs_root=runs_root)
    meta = run_store.EvalRunMeta(
        name="end to end render test",
        slug="multiagent-aggressive-haiku",
        agent_id="agent_012quxziEPohKT3uPkt5Nzit",
        git_ref=real_head,
        composition="multi-agent",
        models=["claude-sonnet-5", "claude-haiku-4-5-20251001"],
        parameters={"agent": {}, "sub_agents": []},
        repetitions=1,
        criteria=["factual_accuracy"],
        state=run_store.STATE_COMPLETED,
        email_sent=False,
        is_production_config=False,
        created_at=1000,
    )
    run_store.write_eval_run_meta(run_dir, meta)
    run_store.write_artifacts(run_dir, 1, {"/workspace/AI Brief - 2026-07-07.md": "# Daily AI Brief\n\nSome content."})
    run_store.write_scores(run_dir, 1, {"factual_accuracy": {"score": 4, "rationale": "ok", "evidence": "n/a", "insufficient_data": False}})
    run_store.write_run_meta(run_dir, 1, {"deployment_id": "depl_x", "session_id": "sesn_x", "thread_count": 5, "final_status": "idle", "timestamp": "x"})
    run_store.write_cost(
        run_dir,
        1,
        type("B", (), {"to_dict": lambda self: {"session_id": "sesn_x", "total_cost_usd": 2.317, "total_usage": {}, "threads": [{"thread_id": "t1", "role": "coordinator", "agent_id": "a1", "model": "claude-sonnet-5", "usage": {}, "cost_usd": 0.6}]}})(),
    )

    response = client.get(f"/runs/multiagent-aggressive-haiku/{run_dir.name}")

    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "end to end render test" in body
    assert "<h1>Daily AI Brief</h1>" in body  # rendered from Markdown
    assert "research" in body.lower()  # a real sub-agent name surfaced from git_show
