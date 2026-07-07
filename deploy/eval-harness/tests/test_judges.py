"""Unit tests for eval_core/judges/* (PRD docs/prd/eval-harness.md FR-6/FR-7/FR-9/FR-11;
judge methodology v2, 2026-07-07, owner-directed, docs/adr/0016 amendment).

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
from eval_core.judges.base import JUDGE_MODELS, JudgeResult, run_judge  # noqa: E402

from conftest import FakeContentBlock, FakeMixedMessage, FakeServerToolUse, FakeUsage, make_fake_client  # noqa: E402

_SOURCES_MD = "# Daily AI Brief - Source List\n\n## Tier 1\n- Anthropic - News: https://www.anthropic.com/news\n"


def _low_score_response(rationale: str, evidence: str, score: int = 1, **extra) -> str:
    body = {"score": score, "rationale": rationale, "evidence": evidence, "insufficient_data": False}
    body.update(extra)
    return json.dumps(body)


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
            selection_disagreements=[
                {
                    "story": "OpenAI ships GPT-6, new SOTA on every benchmark",
                    "judge_view": "should have been included",
                    "rationale": "Confirmed via web_search this was widely covered as a major release.",
                }
            ],
        )
    )

    result = judge_content_selection(client, candidates_json=candidates, brief_markdown=brief)

    assert isinstance(result, JudgeResult)
    assert result.insufficient_data is False
    assert result.score == 1
    assert "GPT-6" in result.evidence or "excluded" in result.evidence
    assert result.model == JUDGE_MODELS["content_selection"] == "claude-opus-4-8"
    assert result.selection_disagreements == [
        {
            "story": "OpenAI ships GPT-6, new SOTA on every benchmark",
            "judge_view": "should have been included",
            "rationale": "Confirmed via web_search this was widely covered as a major release.",
        }
    ]

    # Prove the prompt actually carried the candidates-vs-chosen contrast the judge needs.
    sent_call = client.messages.calls[0]
    sent_prompt = sent_call["messages"][0]["content"]
    assert "excluded" in sent_prompt
    assert "OpenAI ships GPT-6" in sent_prompt

    # v2: web_search/web_fetch tools passed with the owner-specified max_uses=5.
    tool_types = {t["type"]: t for t in sent_call["tools"]}
    assert tool_types["web_search_20250305"]["max_uses"] == 5
    assert tool_types["web_fetch_20250910"]["max_uses"] == 5
    assert sent_call["model"] == "claude-opus-4-8"


def test_content_selection_degrades_gracefully_with_no_candidates_artifact():
    """FR-6: a run from before Phase 1 shipped (no candidates.json) must report
    insufficient_data, not error or guess."""
    client = make_fake_client()  # no responses queued -- must never be called

    result = judge_content_selection(client, candidates_json=None, brief_markdown="# Brief")

    assert result.insufficient_data is True
    assert result.score is None
    assert client.messages.calls == []


# --- factual accuracy (v2 full rework) ------------------------------------------------


def test_factual_accuracy_flags_a_contradicted_claim_via_research_findings():
    """Fixture: a claim the judge's OWN research contradicts (not merely
    'unfamiliar') -- proves the v2 judge scores off documented findings, not
    training-data plausibility alone."""
    brief = (
        "# Daily AI Brief\n\n"
        "### AnthropicCorp releases Claude Ultra 99\n"
        "Claude Ultra 99 achieved exactly 99.987% on the SuperMegaBench-9000, according to "
        "internal sources who wished to remain unnamed and cannot be verified.\n"
    )

    client = make_fake_client(
        _low_score_response(
            "My own research found no corroborating source for this benchmark claim after checking "
            "multiple outlets -- the brief's figure could not be substantiated.",
            "Claude Ultra 99 achieved exactly 99.987% on the SuperMegaBench-9000",
            findings=[
                {
                    "claim": "Claude Ultra 99 achieved 99.987% on SuperMegaBench-9000",
                    "verdict": "unverifiable",
                    "source_checked": "https://example.test/search-results",
                    "note": "No outlet reports this benchmark or score; likely fabricated.",
                }
            ],
        )
    )

    result = judge_factual_accuracy(client, brief_markdown=brief, sources_md=_SOURCES_MD)

    assert result.score == 1
    assert result.model == JUDGE_MODELS["factual_accuracy"] == "claude-opus-4-8"
    assert result.findings == [
        {
            "claim": "Claude Ultra 99 achieved 99.987% on SuperMegaBench-9000",
            "verdict": "unverifiable",
            "source_checked": "https://example.test/search-results",
            "note": "No outlet reports this benchmark or score; likely fabricated.",
        }
    ]


def test_factual_accuracy_prompt_instructs_against_knowledge_cutoff_bias():
    """Regression for the real live-run finding (2026-07-07 committed
    production-baseline scores.json): the judge previously penalized a brief for
    being 'dated July 7, 2026 -- a future date' and treated unfamiliar product
    names as fabrication evidence. The v2 system prompt must explicitly forbid
    that reasoning."""
    client = make_fake_client(_low_score_response("fine", "n/a", score=5, findings=[]))

    judge_factual_accuracy(client, brief_markdown="# Brief", sources_md=_SOURCES_MD)

    system_prompt = client.messages.calls[0]["system"]
    assert "knowledge cutoff" in system_prompt.lower() or "training-data" in system_prompt.lower()
    assert "not evidence of" in system_prompt.lower() or "is not evidence" in system_prompt.lower()


def test_factual_accuracy_prompt_contains_the_sources_md_content():
    client = make_fake_client(_low_score_response("fine", "n/a", score=5, findings=[]))

    judge_factual_accuracy(client, brief_markdown="# Brief", sources_md=_SOURCES_MD)

    system_prompt = client.messages.calls[0]["system"]
    assert "Anthropic - News: https://www.anthropic.com/news" in system_prompt


def test_factual_accuracy_prompt_contains_the_focus_set():
    client = make_fake_client(_low_score_response("fine", "n/a", score=5, findings=[]))

    judge_factual_accuracy(client, brief_markdown="# Brief", sources_md=_SOURCES_MD)

    system_prompt = client.messages.calls[0]["system"]
    for term in ("headlines", "numbers", "dates", "dollar amounts", "benchmark scores", "direct quotes", "named products"):
        assert term in system_prompt.lower()


def test_factual_accuracy_passes_web_search_and_web_fetch_tools_with_max_uses_eight():
    client = make_fake_client(_low_score_response("fine", "n/a", score=5, findings=[]))

    judge_factual_accuracy(client, brief_markdown="# Brief", sources_md=_SOURCES_MD)

    sent_call = client.messages.calls[0]
    tool_types = {t["type"]: t for t in sent_call["tools"]}
    assert tool_types["web_search_20250305"] == {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}
    assert tool_types["web_fetch_20250910"] == {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 8}
    assert sent_call["model"] == "claude-opus-4-8"


# --- FR-9 length/format compliance (v2: model only, prompt unchanged) ---------------


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
    sent_call = client.messages.calls[0]
    assert "8-15" in sent_call["system"]
    # v2: model moves to Opus 4.8; the prompt itself is otherwise untouched, and
    # no tools are passed for this judge.
    assert sent_call["model"] == "claude-opus-4-8"
    assert "tools" not in sent_call


# --- dedup (v2: feed fix + richer findings) -------------------------------------------


def test_dedup_flags_a_brief_that_repeats_yesterdays_top_story_verbatim():
    """Fixture: today's brief repeats yesterday's top story with no new information."""
    prior_markdown = "# Daily AI Brief - 2026-07-03\n\n### Big Lab releases Model X\nModel X scored 90% on Bench Y.\n"
    today = "# Daily AI Brief - 2026-07-04\n\n### Big Lab releases Model X\nModel X scored 90% on Bench Y.\n"

    client = make_fake_client(
        _low_score_response(
            "Today's brief repeats yesterday's 'Big Lab releases Model X' story verbatim with no "
            "new information or follow-up framing -- a clear dedup failure.",
            "Big Lab releases Model X / Model X scored 90% on Bench Y (repeated verbatim)",
            findings=[
                {
                    "story": "Big Lab releases Model X",
                    "duplicate_of_date": "2026-07-03",
                    "labelled_as_followup": False,
                    "justified": False,
                    "note": "Identical claim, no new data, no follow-up label.",
                }
            ],
        )
    )

    result = judge_dedup(client, brief_markdown=today, priors=[{"date": "2026-07-03", "markdown": prior_markdown}])

    assert result.score == 1
    assert "Model X" in result.evidence
    assert result.model == JUDGE_MODELS["dedup"] == "claude-opus-4-8"
    assert result.findings == [
        {
            "story": "Big Lab releases Model X",
            "duplicate_of_date": "2026-07-03",
            "labelled_as_followup": False,
            "justified": False,
            "note": "Identical claim, no new data, no follow-up label.",
        }
    ]
    # No web tools for dedup (owner spec: "No web tools needed here").
    assert "tools" not in client.messages.calls[0]


def test_dedup_degrades_gracefully_with_no_prior_briefs():
    client = make_fake_client()

    result = judge_dedup(client, brief_markdown="# Brief", priors=[])

    assert result.insufficient_data is True
    assert result.score is None
    assert client.messages.calls == []


def test_dedup_prompt_tells_the_judge_each_priors_date_explicitly():
    """Regression for the real live-run finding (2026-07-07 committed
    multiagent-aggressive-haiku scores.json): the judge was previously handed
    undated prior markdown and could not itself tell a genuine prior apart from
    same-day contamination. The prompt must name each prior's date."""
    client = make_fake_client(_low_score_response("fine", "n/a", score=5, findings=[]))

    judge_dedup(
        client,
        brief_markdown="# Today",
        priors=[
            {"date": "2026-07-06", "markdown": "# Yesterday's brief"},
            {"date": "2026-07-05", "markdown": "# Day before's brief"},
        ],
    )

    user_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "2026-07-06" in user_prompt
    assert "2026-07-05" in user_prompt


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


def test_run_judge_uses_the_default_model_when_none_is_passed():
    client = make_fake_client(json.dumps({"score": 3, "rationale": "ok", "evidence": "x", "insufficient_data": False}))

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.model == "claude-opus-4-8"
    assert client.messages.calls[0]["model"] == "claude-opus-4-8"


def test_run_judge_passes_through_an_explicit_model():
    client = make_fake_client(json.dumps({"score": 3, "rationale": "ok", "evidence": "x", "insufficient_data": False}))

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user", model="claude-sonnet-5")

    assert result.model == "claude-sonnet-5"
    assert client.messages.calls[0]["model"] == "claude-sonnet-5"


def test_run_judge_omits_tools_kwarg_when_none_given():
    client = make_fake_client(json.dumps({"score": 3, "rationale": "ok", "evidence": "x", "insufficient_data": False}))

    run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert "tools" not in client.messages.calls[0]


def test_run_judge_passes_through_a_given_tools_list_unmodified():
    client = make_fake_client(json.dumps({"score": 3, "rationale": "ok", "evidence": "x", "insufficient_data": False}))
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]

    run_judge(client, criterion="test", system_prompt="sys", user_prompt="user", tools=tools)

    assert client.messages.calls[0]["tools"] == tools


# --- v2: mixed-content responses (server-side tool interleaving) --------------------


def test_run_judge_finds_the_last_text_block_among_interleaved_tool_blocks():
    """A server-side-tool response carries narration text, tool_use/result
    blocks, then a FINAL text block with the real JSON verdict -- run_judge()
    must parse ONLY the last text block, not join every text block (an earlier
    narration block could itself contain stray braces) and not grab the first."""
    mixed = FakeMixedMessage(
        [
            FakeContentBlock("text", text="Let me search for this claim. { not json, just narration }"),
            FakeContentBlock("server_tool_use"),
            FakeContentBlock("web_search_tool_result"),
            FakeContentBlock(
                "text",
                text=json.dumps(
                    {"score": 4, "rationale": "Confirmed via search.", "evidence": "n/a", "insufficient_data": False}
                ),
            ),
        ]
    )
    client = make_fake_client(mixed)

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user", tools=[{"type": "web_search_20250305", "name": "web_search"}])

    assert result.score == 4
    assert result.rationale == "Confirmed via search."


def test_run_judge_mixed_content_captures_search_count():
    mixed = FakeMixedMessage(
        [
            FakeContentBlock("server_tool_use"),
            FakeContentBlock("web_search_tool_result"),
            FakeContentBlock(
                "text",
                text=json.dumps({"score": 5, "rationale": "ok", "evidence": "n/a", "insufficient_data": False}),
            ),
        ],
        usage=FakeUsage(server_tool_use=FakeServerToolUse(web_search_requests=3)),
    )
    client = make_fake_client(mixed)

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.search_count == 3


def test_run_judge_returns_zero_search_count_when_no_server_tool_use_reported():
    client = make_fake_client(json.dumps({"score": 3, "rationale": "ok", "evidence": "x", "insufficient_data": False}))

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.search_count == 0


# --- v2: findings/selection_disagreements parsing ------------------------------------


def test_run_judge_parses_a_findings_array_when_present():
    client = make_fake_client(
        json.dumps(
            {
                "score": 2,
                "rationale": "issues found",
                "evidence": "n/a",
                "insufficient_data": False,
                "findings": [{"claim": "X", "verdict": "contradicted", "source_checked": "y", "note": "z"}],
            }
        )
    )

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.findings == [{"claim": "X", "verdict": "contradicted", "source_checked": "y", "note": "z"}]
    assert result.selection_disagreements is None


def test_run_judge_parses_a_selection_disagreements_array_when_present():
    client = make_fake_client(
        json.dumps(
            {
                "score": 4,
                "rationale": "mostly fine",
                "evidence": "n/a",
                "insufficient_data": False,
                "selection_disagreements": [{"story": "A", "judge_view": "should have been excluded", "rationale": "b"}],
            }
        )
    )

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.selection_disagreements == [{"story": "A", "judge_view": "should have been excluded", "rationale": "b"}]
    assert result.findings is None


def test_run_judge_leaves_findings_and_selection_disagreements_none_when_absent():
    client = make_fake_client(json.dumps({"score": 3, "rationale": "ok", "evidence": "x", "insufficient_data": False}))

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.findings is None
    assert result.selection_disagreements is None


def test_run_judge_ignores_a_findings_field_that_isnt_a_list():
    """A malformed/off-shape 'findings' value (e.g. the judge emitted a string
    instead of an array) must not be trusted as-is -- degrade to None rather than
    storing garbage a caller would then try to iterate."""
    client = make_fake_client(
        json.dumps({"score": 3, "rationale": "ok", "evidence": "x", "insufficient_data": False, "findings": "not a list"})
    )

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.findings is None


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


def test_run_judge_captures_search_count_even_on_the_malformed_response_degrade_path():
    mixed = FakeMixedMessage(
        [FakeContentBlock("text", text="not json at all")],
        usage=FakeUsage(server_tool_use=FakeServerToolUse(web_search_requests=2)),
    )
    client = make_fake_client(mixed)

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.insufficient_data is True
    assert result.search_count == 2


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
    assert result.search_count == 0


def test_run_judge_enables_automatic_prompt_caching():
    """Judge calls must carry the top-level automatic-caching field (2026-07-07
    cost fix): without it, a web-tool judge's server-side loop re-sends the
    whole accumulated context UNCACHED every iteration -- the first live v2
    accuracy smoke burned 281,543 uncached input tokens ($1.52) with
    cache_read=0. The fake records kwargs, so this pins the field's presence
    and exact shape."""
    from eval_core.judges.base import run_judge

    client = make_fake_client('{"score": 4, "rationale": "r", "evidence": "e", "insufficient_data": false}')
    run_judge(client, criterion="length_format", system_prompt="s", user_prompt="u", model="claude-opus-4-8")
    assert client.messages.calls[0]["cache_control"] == {"type": "ephemeral"}
