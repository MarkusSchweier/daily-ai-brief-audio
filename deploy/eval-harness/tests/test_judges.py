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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_core.judges import (  # noqa: E402
    judge_content_selection,
    judge_dedup,
    judge_factual_accuracy,
    judge_length_format,
)
from eval_core.judges.base import (  # noqa: E402
    JUDGE_EFFORT,
    JUDGE_MODELS,
    MAX_PAUSE_TURN_CONTINUATIONS,
    JudgeCallError,
    JudgePauseLimitError,
    JudgeRefusedError,
    JudgeResult,
    JudgeTruncatedError,
    run_judge,
)

from conftest import (  # noqa: E402
    FakeContentBlock,
    FakeMixedMessage,
    FakeServerToolUse,
    FakeStopDetails,
    FakeUsage,
    make_fake_client,
)

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
    assert result.model == JUDGE_MODELS["content_selection"] == "claude-opus-5-5"
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
    assert sent_call["model"] == "claude-opus-5-5"


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
    assert result.model == JUDGE_MODELS["factual_accuracy"] == "claude-opus-5-5"
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
    assert sent_call["model"] == "claude-opus-5-5"


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
    assert sent_call["model"] == "claude-opus-5-5"
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
    assert result.model == JUDGE_MODELS["dedup"] == "claude-opus-5-5"
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

    assert result.model == "claude-opus-5-5"
    assert client.messages.calls[0]["model"] == "claude-opus-5-5"


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
    run_judge(client, criterion="length_format", system_prompt="s", user_prompt="u", model="claude-opus-5-5")
    assert client.messages.calls[0]["cache_control"] == {"type": "ephemeral"}


# --- Claude Opus 5.5 request surface (2026-10-11, owner-directed model move) ---------


_OK_RESPONSE = '{"score": 4, "rationale": "r", "evidence": "e", "insufficient_data": false}'


def test_all_four_judges_resolve_to_opus_5_5():
    assert set(JUDGE_MODELS.values()) == {"claude-opus-5-5"}


def test_every_judge_call_sends_explicit_high_effort():
    """Opus 5.5's API default effort is `medium` where Opus 4.8's was `high`, so
    without this field the model move would quietly drop the judges one
    deliberation level below every previously-committed run -- a change to the
    measuring instrument. Asserted per judge, not just on run_judge()."""
    assert JUDGE_EFFORT == "high"

    calls = []
    for invoke in (
        lambda c: judge_factual_accuracy(c, brief_markdown="# B", sources_md=_SOURCES_MD),
        lambda c: judge_content_selection(
            c, candidates_json=[{"title": "t", "source": "s", "disposition": "included"}], brief_markdown="# B"
        ),
        lambda c: judge_length_format(c, brief_markdown="# B"),
        lambda c: judge_dedup(c, brief_markdown="# B", priors=[{"date": "2026-10-10", "markdown": "# prior"}]),
    ):
        client = make_fake_client(_OK_RESPONSE)
        invoke(client)
        calls.append(client.messages.calls[0])

    assert len(calls) == 4
    for call in calls:
        assert call["output_config"] == {"effort": "high"}
        assert call["model"] == "claude-opus-5-5"


def test_no_judge_call_sends_a_field_opus_5_5_rejects():
    """Opus 5.5 400s on `thinking: {"type": "disabled"}`, `budget_tokens`,
    non-default sampling params, and forced `tool_choice` -- none of which
    run_judge() has ever sent. This pins that it never starts (a future edit that
    adds e.g. temperature would fail HERE instead of live, mid-batch)."""
    client = make_fake_client(_OK_RESPONSE, _OK_RESPONSE)
    judge_factual_accuracy(client, brief_markdown="# B", sources_md=_SOURCES_MD)
    judge_length_format(client, brief_markdown="# B")

    for call in client.messages.calls:
        for forbidden in ("thinking", "temperature", "top_p", "top_k", "tool_choice"):
            assert forbidden not in call


def test_each_judges_max_tokens_leaves_room_for_always_on_thinking():
    """Thinking tokens count against max_tokens on Opus 5.5, so the v2 caps
    (1024-4096, sized for a no-thinking Opus 4.8) would truncate: the three
    reasoning judges use 16000 and length_format 8000."""
    expected = {
        "factual_accuracy": (16000, lambda c: judge_factual_accuracy(c, brief_markdown="# B", sources_md=_SOURCES_MD)),
        "content_selection": (
            16000,
            lambda c: judge_content_selection(
                c, candidates_json=[{"title": "t", "source": "s", "disposition": "included"}], brief_markdown="# B"
            ),
        ),
        "dedup": (
            16000,
            lambda c: judge_dedup(c, brief_markdown="# B", priors=[{"date": "2026-10-10", "markdown": "# prior"}]),
        ),
        "length_format": (8000, lambda c: judge_length_format(c, brief_markdown="# B")),
    }
    for criterion, (max_tokens, invoke) in expected.items():
        client = make_fake_client(_OK_RESPONSE)
        invoke(client)
        assert client.messages.calls[0]["max_tokens"] == max_tokens, criterion


def test_run_judge_ignores_thinking_blocks_when_extracting_the_verdict():
    """Opus 5.5 always thinks, so a judge response routinely LEADS with one or
    more `thinking` blocks (empty text under the default display, or progress
    updates between tool calls). The verdict is still the last TEXT block --
    thinking blocks must never be mistaken for it, nor break the extraction."""
    mixed = FakeMixedMessage(
        [
            FakeContentBlock("thinking"),  # no .text at all, like a real omitted-display block
            FakeContentBlock("text", text="Let me verify this. { stray braces in narration }"),
            FakeContentBlock("server_tool_use"),
            FakeContentBlock("web_search_tool_result"),
            FakeContentBlock("thinking"),  # a progress-update block before the final answer
            FakeContentBlock("text", text=json.dumps({"score": 5, "rationale": "verified", "evidence": "n/a", "insufficient_data": False})),
        ]
    )
    client = make_fake_client(mixed)

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="user")

    assert result.score == 5
    assert result.rationale == "verified"


def test_run_judge_raises_on_a_refusal_carrying_the_stop_details_category():
    """A classifier decline is an HTTP 200 with `stop_reason: "refusal"` and
    (possibly) a category. It must raise -- NOT fall through to the JSON parser
    (which would record a bland insufficient_data degrade) and NOT silently retry
    on another model (which would change the measuring instrument mid-batch)."""
    refused = FakeMixedMessage([], stop_reason="refusal", stop_details=FakeStopDetails(category="bio", explanation="declined"))
    client = make_fake_client(refused)

    with pytest.raises(JudgeRefusedError) as excinfo:
        run_judge(client, criterion="factual_accuracy", system_prompt="sys", user_prompt="user")

    error = excinfo.value
    assert error.category == "bio"
    assert error.explanation == "declined"
    assert error.criterion == "factual_accuracy"
    assert "bio" in str(error)
    # Exactly ONE call -- no fallback-model retry.
    assert len(client.messages.calls) == 1


def test_run_judge_raises_on_a_refusal_with_null_stop_details():
    """`stop_details` can be null even on a real refusal -- branch on
    `stop_reason`, treat `stop_details` as informational."""
    client = make_fake_client(FakeMixedMessage([], stop_reason="refusal"))

    with pytest.raises(JudgeRefusedError) as excinfo:
        run_judge(client, criterion="dedup", system_prompt="sys", user_prompt="user")

    assert excinfo.value.category is None


def test_run_judge_raises_on_a_truncated_response():
    """`stop_reason: "max_tokens"` means the verdict was cut off -- raising names
    the real, one-line-fixable cause instead of recording a formatting degrade
    (note the fake's text IS parseable JSON here: the raise must be driven by the
    stop reason, not by a parse failure)."""
    client = make_fake_client(FakeMixedMessage([FakeContentBlock("text", text=_OK_RESPONSE)], stop_reason="max_tokens"))

    with pytest.raises(JudgeTruncatedError) as excinfo:
        run_judge(client, criterion="content_selection", system_prompt="sys", user_prompt="user", max_tokens=12345)

    assert excinfo.value.max_tokens == 12345
    assert "12345" in str(excinfo.value)


def test_run_judge_resumes_a_paused_turn_and_sums_usage_and_searches():
    """The server-side tool loop hitting its 10-iteration limit returns
    `pause_turn`. run_judge() resumes by sending the paused assistant turn back
    UNCHANGED (no "Continue." message), and the returned usage/search_count must
    be the SUM over every iteration -- otherwise a resumed judge call is
    under-billed in judge-cost.json."""
    paused = FakeMixedMessage(
        [FakeContentBlock("text", text="searching..."), FakeContentBlock("server_tool_use")],
        usage=FakeUsage(
            input_tokens=100,
            output_tokens=10,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=7,
            server_tool_use=FakeServerToolUse(web_search_requests=8),
        ),
        stop_reason="pause_turn",
    )
    finished = FakeMixedMessage(
        [FakeContentBlock("text", text=_OK_RESPONSE)],
        usage=FakeUsage(
            input_tokens=1,
            output_tokens=20,
            cache_read_input_tokens=50,
            cache_creation_input_tokens=3,
            server_tool_use=FakeServerToolUse(web_search_requests=2),
        ),
        stop_reason="end_turn",
    )
    client = make_fake_client(paused, finished)

    result = run_judge(
        client,
        criterion="factual_accuracy",
        system_prompt="sys",
        user_prompt="the brief",
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
    )

    assert result.score == 4
    assert result.search_count == 10  # 8 + 2
    assert result.usage == {
        "input_tokens": 101,
        "output_tokens": 30,
        "cache_read_input_tokens": 55,
        "cache_creation_5m_input_tokens": 10,
        "cache_creation_1h_input_tokens": 0,
    }

    # The continuation re-sends the original user message plus the paused
    # assistant turn's blocks VERBATIM, and carries the same tools/effort/cache
    # settings as the first call.
    first, second = client.messages.calls
    assert first["messages"] == [{"role": "user", "content": "the brief"}]
    assert second["messages"][0] == {"role": "user", "content": "the brief"}
    assert second["messages"][1]["role"] == "assistant"
    assert second["messages"][1]["content"] == paused.content
    assert not any(m["content"] == "Continue." for m in second["messages"])
    assert second["tools"] == first["tools"]
    assert second["output_config"] == first["output_config"]
    assert second["cache_control"] == first["cache_control"]


def test_run_judge_accumulates_blocks_across_several_pauses():
    """A second pause appends to the SAME assistant turn (the server is resuming
    one turn, and every block is still passed back unchanged) rather than adding
    another assistant message."""
    pause_one = FakeMixedMessage([FakeContentBlock("server_tool_use")], stop_reason="pause_turn")
    pause_two = FakeMixedMessage([FakeContentBlock("web_search_tool_result")], stop_reason="pause_turn")
    finished = FakeMixedMessage([FakeContentBlock("text", text=_OK_RESPONSE)], stop_reason="end_turn")
    client = make_fake_client(pause_one, pause_two, finished)

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="u")

    assert result.score == 4
    third_call_messages = client.messages.calls[2]["messages"]
    assert [m["role"] for m in third_call_messages] == ["user", "assistant"]
    assert third_call_messages[1]["content"] == [*pause_one.content, *pause_two.content]


def test_run_judge_gives_up_loudly_after_the_pause_turn_bound():
    """Each continuation is a real billed call, so the bound is on spend as well
    as time: 1 initial call + MAX_PAUSE_TURN_CONTINUATIONS resumes, then raise."""
    responses = [
        FakeMixedMessage([FakeContentBlock("server_tool_use")], stop_reason="pause_turn")
        for _ in range(MAX_PAUSE_TURN_CONTINUATIONS + 1)
    ]
    client = make_fake_client(*responses)

    with pytest.raises(JudgePauseLimitError) as excinfo:
        run_judge(client, criterion="factual_accuracy", system_prompt="sys", user_prompt="u")

    assert excinfo.value.continuations == MAX_PAUSE_TURN_CONTINUATIONS
    assert len(client.messages.calls) == MAX_PAUSE_TURN_CONTINUATIONS + 1


def test_run_judge_still_parses_a_response_with_no_stop_reason_at_all():
    """Defensive: a response object without `stop_reason` (an older shape, or a
    minimal test double) must behave exactly as before -- parsed, not raised."""
    client = make_fake_client(_OK_RESPONSE)

    result = run_judge(client, criterion="test", system_prompt="sys", user_prompt="u")

    assert result.score == 4


def test_run_judge_leaves_the_web_tool_versions_and_caps_untouched():
    """The model move changes model/effort/max_tokens/stop-reason handling ONLY --
    the tool versions and max_uses caps (verified live against Opus 5.5) stay
    exactly as judge methodology v2 set them."""
    accuracy_client = make_fake_client(_OK_RESPONSE)
    judge_factual_accuracy(accuracy_client, brief_markdown="# B", sources_md=_SOURCES_MD)
    assert accuracy_client.messages.calls[0]["tools"] == [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 8},
    ]

    selection_client = make_fake_client(_OK_RESPONSE)
    judge_content_selection(
        selection_client,
        candidates_json=[{"title": "t", "source": "s", "disposition": "included"}],
        brief_markdown="# B",
    )
    assert selection_client.messages.calls[0]["tools"] == [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 5},
    ]


# --- REVIEWER-ADDED (2026-10-11 review): stop_reason x continuation interactions ------


def test_run_judge_raises_when_a_resumed_continuation_is_truncated():
    """The two new behaviors (pause-turn resume, stop_reason handling) INTERACT:
    the accuracy judge's 8+8 tool loop is both the most likely to pause AND the
    most likely to truncate, and its continuation re-sends a context that has
    grown by every tool result so far. A truncated CONTINUATION must raise just
    like a truncated first call -- never fall through to the parser and be
    recorded as a score."""
    paused = FakeMixedMessage([FakeContentBlock("server_tool_use")], stop_reason="pause_turn")
    truncated = FakeMixedMessage(
        [FakeContentBlock("text", text=_OK_RESPONSE)], stop_reason="max_tokens"  # parseable text on purpose
    )
    client = make_fake_client(paused, truncated)

    with pytest.raises(JudgeTruncatedError):
        run_judge(client, criterion="factual_accuracy", system_prompt="s", user_prompt="u", max_tokens=16000)

    assert len(client.messages.calls) == 2


def test_run_judge_raises_when_a_resumed_continuation_is_refused():
    paused = FakeMixedMessage([FakeContentBlock("server_tool_use")], stop_reason="pause_turn")
    refused = FakeMixedMessage([], stop_reason="refusal", stop_details=FakeStopDetails(category="bio"))
    client = make_fake_client(paused, refused)

    with pytest.raises(JudgeRefusedError) as excinfo:
        run_judge(client, criterion="factual_accuracy", system_prompt="s", user_prompt="u")

    assert excinfo.value.category == "bio"
    assert len(client.messages.calls) == 2


def test_run_judge_treats_an_unrecognized_stop_reason_as_a_finished_turn():
    """Only three stop reasons are special-cased. Anything else (`end_turn`,
    `stop_sequence`, `tool_use`, or a stop reason this SDK version has never seen)
    must behave exactly as before: read the last text block and parse it. A future
    stop reason must not silently become an unhandled crash in the judging
    phase -- which would fail the whole repetition."""
    for stop_reason in ("end_turn", "stop_sequence", "tool_use", "some_future_reason"):
        client = make_fake_client(
            FakeMixedMessage([FakeContentBlock("text", text=_OK_RESPONSE)], stop_reason=stop_reason)
        )

        result = run_judge(client, criterion="test", system_prompt="s", user_prompt="u")

        assert result.score == 4, stop_reason
        assert len(client.messages.calls) == 1, stop_reason


def test_a_truncated_response_never_degrades_into_a_score_or_insufficient_data():
    """The specific failure this guards: a truncated verdict usually still looks
    like JSON (`{"score": 3, "rationale": "the brief claims...`), so without
    stop_reason handling it would have been recorded either as a REAL score or as
    a bland `insufficient_data` -- both of which silently corrupt a comparison the
    owner is about to pick a production model from."""
    half_written = '{"score": 3, "rationale": "The brief claims that the new model'
    client = make_fake_client(
        FakeMixedMessage([FakeContentBlock("text", text=half_written)], stop_reason="max_tokens")
    )

    with pytest.raises(JudgeTruncatedError):
        run_judge(client, criterion="dedup", system_prompt="s", user_prompt="u", max_tokens=2048)


def test_judge_models_are_recorded_on_every_result_including_the_degrade_path():
    """`scores.json`'s new `model` key is only meaningful if it is populated on
    EVERY path -- including the malformed-response degrade, which is exactly the
    path whose records a reader is most likely to want to attribute to a specific
    judge model."""
    client = make_fake_client("this is not JSON at all")

    result = run_judge(client, criterion="test", system_prompt="s", user_prompt="u", model="claude-opus-5-5")

    assert result.insufficient_data is True
    assert result.score is None
    assert result.model == "claude-opus-5-5"


# --- A failed judge call still reports what it billed (review-fix, reviewer L3) -------


def test_a_refusal_carries_the_usage_it_already_billed():
    """The refused call itself was a real, billed request -- its usage and search
    count used to be discarded the instant the error was raised, making a refused
    judge call look free."""
    refused = FakeMixedMessage(
        [],
        usage=FakeUsage(input_tokens=1_234, output_tokens=56, server_tool_use=FakeServerToolUse(web_search_requests=2)),
        stop_reason="refusal",
        stop_details=FakeStopDetails(category="bio"),
    )
    client = make_fake_client(refused)

    with pytest.raises(JudgeRefusedError) as excinfo:
        run_judge(client, criterion="factual_accuracy", system_prompt="s", user_prompt="u")

    error = excinfo.value
    assert error.usage["input_tokens"] == 1_234
    assert error.usage["output_tokens"] == 56
    assert error.search_count == 2


def test_a_truncated_call_carries_the_usage_it_already_billed():
    client = make_fake_client(
        FakeMixedMessage(
            [FakeContentBlock("text", text="{half")],
            usage=FakeUsage(input_tokens=99, output_tokens=16_000),
            stop_reason="max_tokens",
        )
    )

    with pytest.raises(JudgeTruncatedError) as excinfo:
        run_judge(client, criterion="dedup", system_prompt="s", user_prompt="u", max_tokens=16000)

    assert excinfo.value.usage["output_tokens"] == 16_000
    assert excinfo.value.search_count == 0


def test_the_pause_limit_error_carries_the_usage_of_every_billed_iteration():
    """The expensive case: exhausting the bound means 1 + MAX_PAUSE_TURN_CONTINUATIONS
    real billed calls, each with its own searches."""
    responses = [
        FakeMixedMessage(
            [FakeContentBlock("server_tool_use")],
            usage=FakeUsage(input_tokens=1_000, output_tokens=100, server_tool_use=FakeServerToolUse(web_search_requests=3)),
            stop_reason="pause_turn",
        )
        for _ in range(MAX_PAUSE_TURN_CONTINUATIONS + 1)
    ]
    client = make_fake_client(*responses)

    with pytest.raises(JudgePauseLimitError) as excinfo:
        run_judge(client, criterion="content_selection", system_prompt="s", user_prompt="u")

    iterations = MAX_PAUSE_TURN_CONTINUATIONS + 1
    assert excinfo.value.usage["input_tokens"] == 1_000 * iterations
    assert excinfo.value.usage["output_tokens"] == 100 * iterations
    assert excinfo.value.search_count == 3 * iterations


def test_a_judge_call_error_defaults_to_zero_usage_when_none_is_supplied():
    """A caller constructing one by hand (or an older call site) must still get a
    valid, priceable zero-usage dict, never a missing attribute."""
    error = JudgeCallError("boom", criterion="dedup", model="claude-opus-5-5")

    assert error.search_count == 0
    assert error.usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
    }


def test_the_errors_usage_shape_is_priceable_by_the_cost_module():
    """The attached usage is the SAME flat shape `JudgeResult.usage` uses, so it
    needs no translation to be priced -- which is the only reason to attach it."""
    from datetime import date

    from harness import cost

    client = make_fake_client(
        FakeMixedMessage([], usage=FakeUsage(input_tokens=10_000, output_tokens=2_000), stop_reason="refusal")
    )

    with pytest.raises(JudgeRefusedError) as excinfo:
        run_judge(client, criterion="dedup", system_prompt="s", user_prompt="u", model="claude-opus-5-5")

    priced = cost.price_usage(
        excinfo.value.usage,
        model="claude-opus-5-5",
        pricing_table=cost.load_pricing_table(),
        on_date=date(2026, 10, 11),
    )
    assert priced > 0
