"""Unit tests for eval_core/judges/* (PRD docs/prd/eval-harness.md FR-6/FR-7/FR-9/FR-11).

Each judge is exercised against a fixture engineered to contain the specific defect it
is supposed to catch, using a fake Anthropic client (no real API key/network call) that
returns a canned low-score JSON response -- this is the "does the judge actually catch
what it's supposed to catch" proof the developer task calls for (analogous to
AC-6/7/9/11), proven at the plumbing level (the judge correctly builds its prompt from
the fixture and correctly parses the judge's verdict back out), not by trusting a real
LLM's judgment in a unit test.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_core.judges import (  # noqa: E402
    judge_content_selection,
    judge_dedup,
    judge_factual_accuracy,
    judge_length_format,
)
from eval_core.judges.base import JudgeResult, run_judge  # noqa: E402

from conftest import FakeUsage, make_fake_client  # noqa: E402


def _low_score_response(rationale: str, evidence: str, score: int = 1) -> str:
    return json.dumps({"score": score, "rationale": rationale, "evidence": evidence, "insufficient_data": False})


# --- FR-6 content selection ---------------------------------------------------------


def test_content_selection_flags_an_obviously_dropped_important_story():
    """Fixture: candidates.json lists a major frontier-model release explicitly
    EXCLUDED from the brief, which never mentions it -- a judge should flag this."""
    candidates = [
        {"title": "OpenAI ships GPT-6, new SOTA on every benchmark", "source": "OpenAI blog", "disposition": "excluded"},
        {"title": "Minor UI tweak to a chatbot app", "source": "Blog", "disposition": "included"},
    ]
    brief = "# Daily AI Brief\n\n## Headlines\n- Minor UI tweak to a chatbot app\n"

    client = make_fake_client(
        _low_score_response(
            "The brief omits the OpenAI GPT-6 SOTA release, a clearly major story, while including a "
            "trivial UI tweak. This is a significant selection miss.",
            "excluded: OpenAI ships GPT-6, new SOTA on every benchmark",
        )
    )

    result = judge_content_selection(client, candidates_json=candidates, brief_markdown=brief)

    assert isinstance(result, JudgeResult)
    assert result.insufficient_data is False
    assert result.score == 1
    assert "GPT-6" in result.evidence or "excluded" in result.evidence

    # Prove the prompt actually carried the candidates-vs-chosen contrast the judge needs.
    sent_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "excluded" in sent_prompt
    assert "OpenAI ships GPT-6" in sent_prompt


def test_content_selection_degrades_gracefully_with_no_candidates_artifact():
    """FR-6: a run from before Phase 1 shipped (no candidates.json) must report
    insufficient_data, not error or guess."""
    client = make_fake_client()  # no responses queued -- must never be called

    result = judge_content_selection(client, candidates_json=None, brief_markdown="# Brief")

    assert result.insufficient_data is True
    assert result.score is None
    assert client.messages.calls == []


# --- FR-7 factual accuracy / hallucination -------------------------------------------


def test_factual_accuracy_flags_an_obvious_hallucination():
    """Fixture: an oddly specific, unsourced claim with no hedging -- a classic
    fabrication pattern."""
    brief = (
        "# Daily AI Brief\n\n"
        "### AnthropicCorp releases Claude Ultra 99\n"
        "Claude Ultra 99 achieved exactly 99.987% on the SuperMegaBench-9000, according to "
        "internal sources who wished to remain unnamed and cannot be verified.\n"
    )

    client = make_fake_client(
        _low_score_response(
            "The claim cites an unverifiable internal source and an oddly precise, unfamiliar "
            "benchmark name with no corroborating outlet -- reads as fabricated.",
            "Claude Ultra 99 achieved exactly 99.987% on the SuperMegaBench-9000",
        )
    )

    result = judge_factual_accuracy(client, brief_markdown=brief)

    assert result.score == 1
    assert "SuperMegaBench" in result.evidence or "fabricated" in result.rationale


# --- FR-9 length/format compliance --------------------------------------------------


def test_length_format_flags_a_brief_that_is_three_times_the_target_length():
    """Fixture: 45 headline bullets (target is 8-15) -- an obvious over-shoot."""
    headlines = "\n".join(f"- Headline number {i}" for i in range(1, 46))
    brief = f"# Daily AI Brief\n\n## Headlines\n{headlines}\n"

    client = make_fake_client(
        _low_score_response(
            "The brief has 45 headline bullets against a stated target of 8-15 -- roughly 3x "
            "over-shoot, indicating no meaningful pruning/prioritization occurred.",
            "45 headline bullets present",
        )
    )

    result = judge_length_format(client, brief_markdown=brief)

    assert result.score == 1
    sent_prompt = client.messages.calls[0]["system"]
    assert "8-15" in sent_prompt


# --- FR-11 day-over-day dedup --------------------------------------------------------


def test_dedup_flags_a_brief_that_repeats_yesterdays_top_story_verbatim():
    """Fixture: today's brief repeats yesterday's top story with no new information."""
    prior = "# Daily AI Brief - 2026-07-03\n\n### Big Lab releases Model X\nModel X scored 90% on Bench Y.\n"
    today = "# Daily AI Brief - 2026-07-04\n\n### Big Lab releases Model X\nModel X scored 90% on Bench Y.\n"

    client = make_fake_client(
        _low_score_response(
            "Today's brief repeats yesterday's 'Big Lab releases Model X' story verbatim with no "
            "new information or follow-up framing -- a clear dedup failure.",
            "Big Lab releases Model X / Model X scored 90% on Bench Y (repeated verbatim)",
        )
    )

    result = judge_dedup(client, brief_markdown=today, prior_briefs_markdown=[prior])

    assert result.score == 1
    assert "Model X" in result.evidence


def test_dedup_degrades_gracefully_with_no_prior_briefs():
    client = make_fake_client()

    result = judge_dedup(client, brief_markdown="# Brief", prior_briefs_markdown=[])

    assert result.insufficient_data is True
    assert result.score is None
    assert client.messages.calls == []


# --- base.run_judge parsing robustness ----------------------------------------------


def test_run_judge_tolerates_a_response_wrapped_in_prose_or_code_fence():
    client = make_fake_client(
        'Sure, here is my assessment:\n```json\n{"score": 4, "rationale": "Looks fine.", '
        '"evidence": "n/a", "insufficient_data": false}\n```\nHope that helps!'
    )

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.score == 4
    assert result.rationale == "Looks fine."


def test_run_judge_degrades_to_insufficient_data_on_unparseable_response():
    client = make_fake_client("I cannot help with that request.")

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.insufficient_data is True
    assert result.score is None


# --- Usage capture (review-fix: judge cost accounting, ADR-0016 D2 cross-cutting) ----


def test_run_judge_captures_usage_from_a_well_formed_response():
    client = make_fake_client(
        (
            json.dumps({"score": 4, "rationale": "fine", "evidence": "n/a", "insufficient_data": False}),
            FakeUsage(input_tokens=123, output_tokens=45, cache_read_input_tokens=6, cache_creation_input_tokens=7),
        )
    )

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.usage == {
        "input_tokens": 123,
        "output_tokens": 45,
        "cache_read_input_tokens": 6,
        "cache_creation_5m_input_tokens": 7,
        "cache_creation_1h_input_tokens": 0,
    }


def test_run_judge_captures_usage_even_on_the_malformed_response_degrade_path():
    """The call still cost real tokens even though the response couldn't be
    parsed as JSON -- usage must not be silently dropped on this path."""
    client = make_fake_client(("not json at all", FakeUsage(input_tokens=50, output_tokens=10)))

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.insufficient_data is True
    assert result.usage["input_tokens"] == 50
    assert result.usage["output_tokens"] == 10


def test_run_judge_defaults_to_a_realistic_nonzero_usage_when_a_test_doesnt_specify_one():
    """Every EXISTING ported judge test (which passes a bare response string, no
    usage) must still get a priceable, non-zero usage for free -- proving the
    default FakeUsage() wiring doesn't silently zero out usage for the whole
    existing suite."""
    client = make_fake_client(json.dumps({"score": 3, "rationale": "ok", "evidence": "x", "insufficient_data": False}))

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.usage["input_tokens"] > 0
    assert result.usage["output_tokens"] > 0


def test_degrade_paths_that_never_call_the_api_report_zero_usage():
    """content_selection/dedup's own "no artifact" degrade paths never call
    client.messages.create() at all -- their JudgeResult.usage must be all-zero,
    not a stale/default value, since no tokens were spent."""
    result = judge_content_selection(make_fake_client(), candidates_json=None, brief_markdown="# Brief")

    assert result.usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
    }
