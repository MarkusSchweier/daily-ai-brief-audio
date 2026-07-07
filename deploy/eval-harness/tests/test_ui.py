"""Tests for ui.py's Flask routes (ADR-0016 D5) -- uses Flask's own test client
(no real server bind, no real subprocess launch: `subprocess.Popen` is
monkeypatched so no `run.py` process is ever actually started by this suite)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import ui as ui_app
from harness import run_store

from conftest import git_init_and_commit


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


def _setup_candidates_dir(monkeypatch, tmp_path: Path, *, commit: bool = True) -> Path:
    """`commit=True` (the default) makes `candidates_dir` a real, freshly-committed
    git repo -- required for `harness.run_store.candidate_declaration_is_dirty()`
    (used by `ui._trigger_run()`) to behave correctly against this synthetic
    fixture, exactly as it would against the real `deploy/candidates/` tree."""
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    _build_candidate_dir(candidates_dir, "test-candidate")
    if commit:
        git_init_and_commit(candidates_dir)
    monkeypatch.setattr(ui_app, "CANDIDATES_DIR", candidates_dir)
    return candidates_dir


def _post_trigger(client, **form_fields):
    """POST /trigger with a valid CSRF token attached -- the shared helper every
    /trigger test uses so the token wiring lives in ONE place."""
    data = dict(form_fields)
    data.setdefault("csrf_token", ui_app._CSRF_TOKEN)
    return client.post("/trigger", data=data)


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


def test_run_detail_containment_check_rejects_a_traversal_even_when_a_real_file_exists_outside_runs_root(
    client, monkeypatch, tmp_path
):
    """Review-fix, security L2: build a REAL eval-run.json OUTSIDE runs_root and
    confirm a slug/eval_run_id combination that traverses OUT to it via ".." is
    still rejected (404) -- proving the explicit resolve()+is_relative_to()
    containment check does real work, not just the (incidental) file-existence
    check that happens to also fail in the common case."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(run_store, "RUNS_ROOT", runs_root)

    outside_dir = tmp_path / "outside-runs-root"
    outside_dir.mkdir()
    (outside_dir / "eval-run.json").write_text("{}", encoding="utf-8")

    response = client.get("/runs/../outside-runs-root")

    assert response.status_code == 404


def test_trigger_without_a_candidate_slug_is_a_400(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)

    response = _post_trigger(client, repetitions="1", criteria=["factual_accuracy"])

    assert response.status_code == 400
    assert b"Select a candidate" in response.data


def test_trigger_without_an_api_key_file_is_a_400(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(ui_app, "ANTHROPIC_API_KEY_FILE", tmp_path / "does-not-exist.txt")

    response = _post_trigger(client, candidate_slug="test-candidate", repetitions="1", criteria=["factual_accuracy"])

    assert response.status_code == 400
    assert b"Anthropic API key" in response.data


def test_trigger_launches_a_subprocess_and_redirects_to_the_run_detail_page(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(run_store, "RUNS_ROOT", tmp_path / "runs")

    api_key_file = tmp_path / "ant-api-key.txt"
    api_key_file.write_text("sk-ant-fake\n", encoding="utf-8")
    monkeypatch.setattr(ui_app, "ANTHROPIC_API_KEY_FILE", api_key_file)

    captured = {}

    def _fake_launch(command, *, env, log_path):
        captured["command"] = command
        captured["env"] = env

    monkeypatch.setattr(ui_app, "_launch_run_subprocess", _fake_launch)

    response = _post_trigger(
        client,
        candidate_slug="test-candidate",
        name="a ui-triggered run",
        repetitions="2",
        criteria=["factual_accuracy", "dedup"],
    )

    assert response.status_code == 302
    assert "/runs/test-candidate/" in response.headers["Location"]

    # The Anthropic API key never leaks into the redirect location or response body.
    assert "sk-ant-fake" not in response.headers["Location"]

    command = captured["command"]
    assert "test-candidate" in command
    assert command[-1] == "test-candidate"  # LAST, after "--" (defense in depth, security M2)
    assert command[-2] == "--"
    assert "--repetitions" in command and "2" in command
    assert "--email" not in command  # the deferred flag is never forwarded from the UI
    assert "--allow-dirty" not in command  # no UI escape hatch for a dirty declaration
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-ant-fake"


# --- Security hardening: CSRF, Origin, slug allowlist, repetitions clamp,
# dirty-declaration guard (review-fix pass) --------------------------------------------


def test_trigger_without_a_csrf_token_is_a_400(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)

    response = client.post("/trigger", data={"candidate_slug": "test-candidate", "criteria": ["factual_accuracy"]})

    assert response.status_code == 400
    assert b"CSRF" in response.data


def test_trigger_with_a_wrong_csrf_token_is_a_400(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)

    response = client.post(
        "/trigger",
        data={"candidate_slug": "test-candidate", "criteria": ["factual_accuracy"], "csrf_token": "forged-token"},
    )

    assert response.status_code == 400
    assert b"CSRF" in response.data


def test_trigger_with_a_forged_cross_site_origin_is_a_400(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)

    response = client.post(
        "/trigger",
        data={"candidate_slug": "test-candidate", "criteria": ["factual_accuracy"], "csrf_token": ui_app._CSRF_TOKEN},
        headers={"Origin": "https://evil.example.com"},
    )

    assert response.status_code == 400
    assert b"origin" in response.data.lower()


def test_trigger_with_a_matching_origin_is_accepted(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(run_store, "RUNS_ROOT", tmp_path / "runs")
    api_key_file = tmp_path / "ant-api-key.txt"
    api_key_file.write_text("sk-ant-fake\n", encoding="utf-8")
    monkeypatch.setattr(ui_app, "ANTHROPIC_API_KEY_FILE", api_key_file)
    monkeypatch.setattr(ui_app, "_launch_run_subprocess", lambda command, *, env, log_path: None)

    response = client.post(
        "/trigger",
        data={"candidate_slug": "test-candidate", "criteria": ["factual_accuracy"], "csrf_token": ui_app._CSRF_TOKEN},
        headers={"Origin": "http://localhost/"},
    )

    assert response.status_code == 302


def _fail_if_launched(command, *, env, log_path):
    raise AssertionError("must not launch a subprocess")


def test_trigger_rejects_a_path_traversal_slug(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)
    candidates_dir_before = sorted(p.name for p in (tmp_path / "candidates").iterdir())
    monkeypatch.setattr(ui_app, "_launch_run_subprocess", _fail_if_launched)

    response = _post_trigger(client, candidate_slug="../../tmp/evil", criteria=["factual_accuracy"])

    assert response.status_code == 400
    assert b"Unknown candidate" in response.data
    # No directory was created anywhere as a side effect of the attempted traversal.
    assert sorted(p.name for p in (tmp_path / "candidates").iterdir()) == candidates_dir_before


def test_trigger_rejects_a_flag_looking_slug(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(ui_app, "_launch_run_subprocess", _fail_if_launched)

    response = _post_trigger(client, candidate_slug="--check-pricing-drift", criteria=["factual_accuracy"])

    assert response.status_code == 400
    assert b"Unknown candidate" in response.data


def test_trigger_clamps_repetitions_to_the_server_side_maximum(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(run_store, "RUNS_ROOT", tmp_path / "runs")
    api_key_file = tmp_path / "ant-api-key.txt"
    api_key_file.write_text("sk-ant-fake\n", encoding="utf-8")
    monkeypatch.setattr(ui_app, "ANTHROPIC_API_KEY_FILE", api_key_file)

    captured = {}

    def _fake_launch(command, *, env, log_path):
        captured["command"] = command

    monkeypatch.setattr(ui_app, "_launch_run_subprocess", _fake_launch)

    response = _post_trigger(client, candidate_slug="test-candidate", repetitions="99999", criteria=["factual_accuracy"])

    assert response.status_code == 302
    command = captured["command"]
    idx = command.index("--repetitions")
    assert command[idx + 1] == str(ui_app.MAX_REPETITIONS)


def test_trigger_on_a_dirty_candidate_directory_is_a_400_and_launches_nothing(client, monkeypatch, tmp_path):
    _setup_candidates_dir(monkeypatch, tmp_path)
    (tmp_path / "candidates" / "test-candidate" / "task-prompt.md").write_text("An UNCOMMITTED edit.", encoding="utf-8")
    api_key_file = tmp_path / "ant-api-key.txt"
    api_key_file.write_text("sk-ant-fake\n", encoding="utf-8")
    monkeypatch.setattr(ui_app, "ANTHROPIC_API_KEY_FILE", api_key_file)
    monkeypatch.setattr(ui_app, "_launch_run_subprocess", _fail_if_launched)

    response = _post_trigger(client, candidate_slug="test-candidate", criteria=["factual_accuracy"])

    assert response.status_code == 400
    assert b"uncommitted changes" in response.data


def test_format_parameters_renders_a_dash_when_everything_is_empty():
    assert ui_app._format_parameters({"agent": {}, "sub_agents": []}) == "—"


def test_format_parameters_renders_the_agents_own_effort_setting():
    display = ui_app._format_parameters({"agent": {"effort": "high"}, "sub_agents": []})
    assert display == "effort=high"


def test_format_parameters_includes_named_sub_agent_parameters():
    display = ui_app._format_parameters(
        {
            "agent": {},
            "sub_agents": [
                {"name": "research-sub-agent", "model": "claude-haiku-4-5-20251001", "parameters": {"effort": "low"}},
                {"name": "selection-sub-agent", "model": "claude-sonnet-5", "parameters": {}},
            ],
        }
    )
    assert display == "research-sub-agent: effort=low"


def test_render_markdown_converts_headings_and_returns_empty_string_for_none():
    html = ui_app.render_markdown("# Title\n\nBody text.")
    assert "<h1>Title</h1>" in html
    assert ui_app.render_markdown(None) == ""


# --- Stored XSS via markdown render (review-fix, security M1) -------------------------


def test_render_markdown_strips_a_raw_script_tag():
    """bleach's `strip=True` removes the disallowed `<script>` TAG (the actually
    executable part) but -- like any HTML sanitizer -- leaves its former text
    content as inert, non-executing plain text, exactly like removing the `<b>`
    tags from `<b>hello</b>` leaves `hello`. The security property that matters
    is verified here: no `<script` tag survives, so nothing executes."""
    html = ui_app.render_markdown("# Daily AI Brief\n\n<script>alert('xss')</script>\n\nSome real content.")
    assert "<script" not in html
    assert "</script>" not in html
    assert "Some real content." in html


def test_render_markdown_strips_an_onerror_attribute():
    html = ui_app.render_markdown('# Brief\n\n<img src="x" onerror="alert(1)">\n\nBody.')
    assert "onerror" not in html
    assert "Body." in html


def test_render_markdown_keeps_allowlisted_tags_and_a_safe_link():
    html = ui_app.render_markdown("# Title\n\n- one\n- two\n\n[a link](https://example.test)\n\n**bold**")
    assert "<h1>Title</h1>" in html
    assert "<li>one</li>" in html
    assert '<a href="https://example.test">a link</a>' in html
    assert "<strong>bold</strong>" in html


def test_render_markdown_strips_a_javascript_href():
    html = ui_app.render_markdown("[click me](javascript:alert(1))")
    assert "javascript:" not in html


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
    run_store.write_judge_cost(
        run_dir,
        1,
        {"model": "claude-haiku-4-5", "total_cost_usd": 0.0031, "total_usage": {}, "per_criterion": {"factual_accuracy": {"cost_usd": 0.0031, "usage": {}}}},
    )

    response = client.get(f"/runs/multiagent-aggressive-haiku/{run_dir.name}")

    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "end to end render test" in body
    assert "<h1>Daily AI Brief</h1>" in body  # rendered from Markdown
    assert "research" in body.lower()  # a real sub-agent name surfaced from git_show
    assert "0.0031" in body  # the judge cost, rendered separately from pipeline cost
    assert "declaration_dirty" not in body  # a clean run -- no dirty badge


def test_run_detail_shows_a_dirty_badge_when_declaration_dirty_is_true(client, monkeypatch, tmp_path):
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(run_store, "RUNS_ROOT", runs_root)

    run_dir = run_store.eval_run_dir("test-candidate", "2026-07-07-999999-dirty", runs_root=runs_root)
    meta = run_store.EvalRunMeta(
        name="a dirty run",
        slug="test-candidate",
        agent_id="agent_x",
        git_ref="a" * 40,
        composition="single-agent",
        models=["claude-sonnet-5"],
        parameters={"agent": {}, "sub_agents": []},
        repetitions=1,
        criteria=["factual_accuracy"],
        state=run_store.STATE_COMPLETED,
        email_sent=False,
        is_production_config=False,
        created_at=1000,
        declaration_dirty=True,
    )
    run_store.write_eval_run_meta(run_dir, meta)

    response = client.get(f"/runs/test-candidate/{run_dir.name}")

    assert response.status_code == 200
    assert b"declaration_dirty" in response.data
