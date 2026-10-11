"""Tests for the 2026-10-11 model-generation comparison candidates --
`haiku-5-5-hardened`, `sonnet-5-5-hardened`, `opus-5-5-hardened`.

These three exist to compare the three current-generation models (Haiku 5.5, Sonnet
5.5, Opus 5.5) against the configuration production actually runs today
(`haiku-swap-hardened`, promoted to production 2026-07-13 per
docs/prd/cost-optimization-candidates.md §6). The comparison is only meaningful under
SINGLE-VARIABLE discipline: model + effort are the ONLY dimensions that may differ.
A reworded prompt, a different skill pin, or a changed tool config would silently turn
a model comparison into a prompt comparison -- exactly the failure the PRD's "prompt
wording is preserved verbatim from the baseline wherever possible" requirement exists
to prevent, and exactly the kind of drift a hand-maintained copy acquires over time.

So this file asserts the discipline mechanically, against the real candidate
directories (no fixtures, no mocks -- the loader makes no network calls):
  1. `system-prompt.md`, `task-prompt.md` and `skills.json` are BYTE-IDENTICAL to
     `haiku-swap-hardened`'s, and so are `agent.json`'s `tools`/`mcp_servers`.
  2. each candidate LOADS (so its `parameters.json` passes the loader's effort
     validation -- a typo'd level would otherwise only surface at sync time), and
  3. `to_agent_body()["model"]` is the expected `{"id", "effort"}` object, i.e. the
     declared effort actually reaches the request body rather than sitting inert in a
     file nothing reads.
Plus a repo-wide guard that the pre-effort candidates still send a bare-string `model`.
"""

from __future__ import annotations

import json

import pytest

from candidate_sync.loader import EFFORT_LEVELS, load_candidate

from conftest import CANDIDATES_DIR

BASELINE_SLUG = "haiku-swap-hardened"

# The three new candidates: slug -> (model id, effort level). Opus runs at `medium`
# rather than `high` by owner choice (its per-token price makes a high-effort run's
# cost the thing most likely to disqualify it before quality is even assessed).
MODEL_GENERATION_CANDIDATES = {
    "haiku-5-5-hardened": ("claude-haiku-5-5", "high"),
    "sonnet-5-5-hardened": ("claude-sonnet-5-5", "high"),
    "opus-5-5-hardened": ("claude-opus-5-5", "medium"),
}

# The files that MUST match the baseline byte-for-byte -- the single-variable contract.
BYTE_IDENTICAL_FILES = ("system-prompt.md", "task-prompt.md", "skills.json")


@pytest.mark.parametrize("slug", sorted(MODEL_GENERATION_CANDIDATES))
@pytest.mark.parametrize("filename", BYTE_IDENTICAL_FILES)
def test_prompt_and_skill_files_are_byte_identical_to_the_baseline(slug, filename):
    baseline_bytes = (CANDIDATES_DIR / BASELINE_SLUG / filename).read_bytes()
    candidate_bytes = (CANDIDATES_DIR / slug / filename).read_bytes()

    assert candidate_bytes == baseline_bytes, (
        f"{slug}/{filename} has drifted from {BASELINE_SLUG}/{filename}. These three candidates may "
        "differ from the baseline in model + effort ONLY -- any other difference turns the "
        "model-generation comparison into a comparison of something else."
    )


@pytest.mark.parametrize("slug", sorted(MODEL_GENERATION_CANDIDATES))
def test_tools_and_mcp_servers_are_identical_to_the_baseline(slug):
    """`agent.json`'s name/description legitimately differ per candidate (each is its
    own agent resource), so this compares the two behavioral fields rather than the
    whole file."""
    baseline = json.loads((CANDIDATES_DIR / BASELINE_SLUG / "agent.json").read_text())
    candidate = json.loads((CANDIDATES_DIR / slug / "agent.json").read_text())

    assert candidate["tools"] == baseline["tools"]
    assert candidate["mcp_servers"] == baseline["mcp_servers"]


@pytest.mark.parametrize("slug", sorted(MODEL_GENERATION_CANDIDATES))
def test_candidate_loads_and_declares_the_expected_model_and_effort(slug):
    expected_model, expected_effort = MODEL_GENERATION_CANDIDATES[slug]

    candidate = load_candidate(CANDIDATES_DIR / slug)

    assert candidate.slug == slug
    assert candidate.agent.name == f"daily-ai-brief-cost-{slug}-candidate"
    assert candidate.agent.model == expected_model  # the PLAIN id string, as the eval harness reads it
    assert candidate.agent.effort == expected_effort
    assert candidate.agent.parameters == {"effort": expected_effort}
    assert not candidate.is_multi_agent
    assert candidate.skill_source_dir is None  # references the shared daily-ai-brief skill, owns no skill source


@pytest.mark.parametrize("slug", sorted(MODEL_GENERATION_CANDIDATES))
def test_request_body_sends_the_model_object_with_the_declared_effort(slug):
    expected_model, expected_effort = MODEL_GENERATION_CANDIDATES[slug]

    body = load_candidate(CANDIDATES_DIR / slug).agent.to_agent_body()

    assert body["model"] == {"id": expected_model, "effort": expected_effort}
    assert "parameters" not in body  # effort travels on the model object, never top-level


@pytest.mark.parametrize("slug", sorted(MODEL_GENERATION_CANDIDATES))
def test_candidate_agent_id_is_explicit(slug):
    """`agent_id` is always PRESENT: an explicit `null` before the first sync (this
    repo's convention for "no real agent resource minted yet"), a real `agent_...` id
    after it (first synced 2026-10-11). A MISSING key would hide which of the two
    states the candidate is in."""
    raw = json.loads((CANDIDATES_DIR / slug / "candidate.json").read_text())

    assert "agent_id" in raw
    assert raw["agent_id"] is None or raw["agent_id"].startswith("agent_")
    assert load_candidate(CANDIDATES_DIR / slug).agent.agent_id == raw["agent_id"]


def _real_candidate_dirs():
    return sorted(p for p in CANDIDATES_DIR.iterdir() if (p / "candidate.json").is_file())


def test_every_real_candidate_declares_only_a_valid_effort_or_nothing():
    """A cheap repo-wide guard: every candidate directory must load, and its
    `parameters.json` must be either `{}` or exactly one valid effort level. Catches a
    typo'd level or a stray tunable in a candidate nobody happened to write a test
    for -- including the ones added after this file."""
    for candidate_dir in _real_candidate_dirs():
        candidate = load_candidate(candidate_dir)  # raises CandidateLoadError on an invalid parameters.json
        for agent in [candidate.agent, *candidate.sub_agents]:
            assert set(agent.parameters) <= {"effort"}
            if agent.effort is not None:
                assert agent.effort in EFFORT_LEVELS


def test_candidates_declaring_no_effort_still_send_a_bare_string_model():
    """THE invariant from the effort-support change: a candidate with
    `parameters.json == {}` must produce exactly the request body it produced before
    effort support existed, or its next "unchanged declaration" re-sync would issue a
    spurious update and bump its live Platform version for nothing."""
    checked = 0
    for candidate_dir in _real_candidate_dirs():
        candidate = load_candidate(candidate_dir)
        for agent in [candidate.agent, *candidate.sub_agents]:
            if agent.effort is not None:
                continue
            body = agent.to_agent_body()
            assert body["model"] == agent.model
            assert isinstance(body["model"], str)
            assert "parameters" not in body
            checked += 1

    # Sanity: the loop above must actually have checked the pre-effort candidates
    # (8 candidates, 21 agent bodies including sub-agents, at the time of writing),
    # not silently passed over an empty list.
    assert checked >= 21


# --- REVIEWER-ADDED (2026-10-11 review) --------------------------------------------


def test_every_candidate_has_its_own_distinct_agent_id():
    """`agent_id` is what `sync.py` updates IN PLACE and what `run.py`/`trigger.py`
    actually trigger -- the slug is only a label. So a copy-pasted or duplicated
    `agent_id` would make two "different candidates" the SAME live agent resource:
    syncing one would silently overwrite the other's model/effort, and an eval run
    recorded under slug X would in fact have run configuration Y. For a batch whose
    entire purpose is attributing cost/quality differences to a model, that is a
    silent wrong-answer bug, and nothing else in the suite would catch it.
    """
    seen: dict[str, str] = {}
    duplicates = []
    for candidate_dir in _real_candidate_dirs():
        raw = json.loads((candidate_dir / "candidate.json").read_text())
        agent_id = raw.get("agent_id")
        if not agent_id:  # explicit null = not synced yet; nothing to collide
            continue
        if agent_id in seen:
            duplicates.append(f"{candidate_dir.name} reuses {seen[agent_id]}'s agent_id {agent_id}")
        seen[agent_id] = candidate_dir.name

    assert not duplicates, "; ".join(duplicates)
    assert len(seen) >= 4, "expected at least the four batch candidates to carry real agent ids"


def test_sub_agent_ids_are_distinct_from_every_other_agent_id():
    """Same hazard one level down: a roster entry sharing an id with its own
    coordinator (or with another candidate's agent) would collapse two agents into
    one on the next sync."""
    all_ids: list[tuple[str, str]] = []
    for candidate_dir in _real_candidate_dirs():
        candidate = load_candidate(candidate_dir)
        for position, agent in enumerate([candidate.agent, *candidate.sub_agents]):
            if agent.agent_id:
                all_ids.append((agent.agent_id, f"{candidate_dir.name}[{position}]"))

    by_id: dict[str, list[str]] = {}
    for agent_id, where in all_ids:
        by_id.setdefault(agent_id, []).append(where)

    collisions = {k: v for k, v in by_id.items() if len(v) > 1}
    assert not collisions, f"agent ids used by more than one declared agent: {collisions}"


def test_the_batch_compares_four_distinct_models_against_one_shared_configuration():
    """The batch is only a MODEL comparison if the four members differ in model:
    three distinct 5.5 ids plus the baseline's Haiku 4.5. Guards against a
    copy-paste that leaves two candidates on the same `model.txt` (which would
    produce two runs that look like a comparison and aren't)."""
    models = {
        slug: load_candidate(CANDIDATES_DIR / slug).agent.model
        for slug in [BASELINE_SLUG, *MODEL_GENERATION_CANDIDATES]
    }

    assert len(set(models.values())) == 4, models
    assert models[BASELINE_SLUG].startswith("claude-haiku-4-5")


def test_the_baseline_declares_no_effort_so_the_batch_documents_its_own_asymmetry():
    """Honest-comparison guard, deliberately asserting the CURRENT, intentional
    state rather than an ideal one: the Haiku 4.5 baseline carries no effort knob
    at all, and Opus runs at `medium` while Haiku/Sonnet run at `high`. So the
    batch is matched on prompt/skill/tools but NOT effort-matched across all four
    members -- which is a real caveat when reading the resulting numbers, and must
    be a deliberate, visible choice rather than a copy-paste slip. If the owner
    ever decides to effort-match the batch, THIS is the test that should change.
    """
    baseline = load_candidate(CANDIDATES_DIR / BASELINE_SLUG)
    assert baseline.agent.parameters == {}
    assert baseline.agent.effort is None

    efforts = {slug: effort for slug, (_model, effort) in MODEL_GENERATION_CANDIDATES.items()}
    assert efforts == {
        "haiku-5-5-hardened": "high",
        "sonnet-5-5-hardened": "high",
        "opus-5-5-hardened": "medium",
    }
