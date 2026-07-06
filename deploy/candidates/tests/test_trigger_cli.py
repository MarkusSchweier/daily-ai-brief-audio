"""Tests for trigger.py's CLI `main()` -- specifically the `--timeout` flag added
during the agent-system-redesign epic's Phase 5 (a real content-generation
candidate's research/writing run takes meaningfully longer than Phase 3's trivial
smoke test, so the CLI needed a way to raise the poll timeout above
`candidate_sync.trigger.DEFAULT_POLL_TIMEOUT_SECONDS` without editing the module).

`trigger.py`'s `main()` builds its own real `httpx.Client` via
`trigger.build_deployments_client()` and calls `trigger.run_candidate()` for the
actual network work -- rather than reach for this repo's `FakeHttpxClient` (which
would still leave `run_candidate()`'s real poll loop running), these tests
monkeypatch `trigger.run_candidate` itself and assert on the `poll_timeout_seconds`
it was actually called with. This is the standard, minimal way to test "did the CLI
correctly thread this argument through" without any real network call or wall-clock
wait."""

from __future__ import annotations

import json
import shutil

import trigger as trigger_cli
from candidate_sync import trigger as trigger_module

from conftest import FIXTURES_DIR


def _copy_fixture_with_agent_id(tmp_path, fixture_name: str, agent_id: str):
    """Copy a synthetic fixture candidate into `tmp_path` and give it a fake
    `agent_id` -- `trigger.py`'s `main()` refuses to run against a candidate with no
    `agent_id` yet (it would otherwise print 'run sync.py against it first' and
    exit 1), so a CLI-level test needs one present, exactly like a real
    already-synced candidate would have."""
    candidate_dir = tmp_path / fixture_name
    shutil.copytree(FIXTURES_DIR / fixture_name, candidate_dir)
    candidate_json_path = candidate_dir / "candidate.json"
    data = json.loads(candidate_json_path.read_text())
    data["agent_id"] = agent_id
    candidate_json_path.write_text(json.dumps(data))
    return candidate_dir


def test_timeout_flag_defaults_to_the_module_constant(tmp_path, monkeypatch):
    """With no `--timeout` given, the CLI must pass the SAME default
    `candidate_sync.trigger.DEFAULT_POLL_TIMEOUT_SECONDS` `run_candidate()` itself
    already defaults to -- i.e. adding the flag must not silently change existing
    (no-flag) CLI behavior."""
    candidate_dir = _copy_fixture_with_agent_id(tmp_path, "example-single-agent", "agent_FAKE_FOR_CLI_TEST")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-cli-test")

    captured: dict = {}

    def fake_run_candidate(client, **kwargs):
        captured.update(kwargs)
        return trigger_module.CandidateRunResult(
            deployment_id="depl_fake", session_id="sess_fake", final_status="idle", events=[]
        )

    monkeypatch.setattr(trigger_module, "run_candidate", fake_run_candidate)

    exit_code = trigger_cli.main([str(candidate_dir)])

    assert exit_code == 0
    assert captured["poll_timeout_seconds"] == trigger_module.DEFAULT_POLL_TIMEOUT_SECONDS


def test_timeout_flag_overrides_the_default(tmp_path, monkeypatch):
    """`--timeout 1200` (the value this phase actually used for a real
    research/writing candidate run, see README.md's Phase 5 validation section) must
    reach `run_candidate()` as `poll_timeout_seconds=1200.0`, not the module's
    600s default."""
    candidate_dir = _copy_fixture_with_agent_id(tmp_path, "example-single-agent", "agent_FAKE_FOR_CLI_TEST")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-cli-test")

    captured: dict = {}

    def fake_run_candidate(client, **kwargs):
        captured.update(kwargs)
        return trigger_module.CandidateRunResult(
            deployment_id="depl_fake", session_id="sess_fake", final_status="idle", events=[]
        )

    monkeypatch.setattr(trigger_module, "run_candidate", fake_run_candidate)

    exit_code = trigger_cli.main([str(candidate_dir), "--timeout", "1200"])

    assert exit_code == 0
    assert captured["poll_timeout_seconds"] == 1200.0


def test_timeout_flag_works_alongside_a_task_prompt_override(tmp_path, monkeypatch):
    """The positional task-prompt-override argument and the new `--timeout` flag
    must compose correctly (argparse ordering) -- a real risk when adding a new flag
    next to an existing positional `nargs="?"` argument."""
    candidate_dir = _copy_fixture_with_agent_id(tmp_path, "example-single-agent", "agent_FAKE_FOR_CLI_TEST")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-cli-test")

    captured: dict = {}

    def fake_run_candidate(client, **kwargs):
        captured.update(kwargs)
        return trigger_module.CandidateRunResult(
            deployment_id="depl_fake", session_id="sess_fake", final_status="idle", events=[]
        )

    monkeypatch.setattr(trigger_module, "run_candidate", fake_run_candidate)

    exit_code = trigger_cli.main([str(candidate_dir), "an override task prompt", "--timeout", "900"])

    assert exit_code == 0
    assert captured["task_prompt"] == "an override task prompt"
    assert captured["poll_timeout_seconds"] == 900.0
