"""Reviewer-added guards for the `haiku-5-5-source-urls` eval arm (ADR-0018 D6).

`test_source_urls_candidate.py` already pins the candidate-vs-baseline relationship.
What it does not assert are the two invariants that make this candidate SAFE to run as
an eval, both of which the appended block is now the first thing in years to put at
risk, because it is the first text ever added to an eval prompt that names external
URLs:

  1. **An eval run never delivers.** The block adds 62 URLs and four fetch rules; it must
     add no delivery capability and must not displace the baseline prompt's explicit
     "no narration, email, or delivery" clause.
  2. **The eval arm is handed exactly the skill's curated URLs** -- no extra URL smuggled
     into the prompt alongside the block, which would make the comparison measure
     something other than "the curated list, now fetchable".
"""

from __future__ import annotations

import sys
from pathlib import Path

from conftest import CANDIDATES_DIR

REPO_ROOT = CANDIDATES_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "deploy" / "managed-agent" / "tools"))

import source_url_block as sub  # noqa: E402

SLUG = "haiku-5-5-source-urls"
TASK_PROMPT_PATH = CANDIDATES_DIR / SLUG / "task-prompt.md"

# The delivery-side strings the PRODUCTION prompt carries and an eval prompt must not:
# the POST /deliver client, its fan-out switch, and the bearer secret it reads.
PRODUCTION_ONLY_DELIVERY_STRINGS = (
    "ENABLE_SUBSCRIBER_FANOUT",
    "delivery_client.py",
    "DELIVERY_BEARER_SECRET_ARN",
    "audio_email.py",
)


def test_the_eval_arm_carries_no_delivery_capability():
    """Evals are content-only, always (PRD §6, ADR-0016). The read-only recent-briefs
    call is the candidate's ONLY permitted delivery-side contact."""
    prompt = TASK_PROMPT_PATH.read_text(encoding="utf-8")

    for forbidden in PRODUCTION_ONLY_DELIVERY_STRINGS:
        assert forbidden not in prompt, (
            f"{SLUG}/task-prompt.md mentions {forbidden!r}. An eval candidate must never be able to "
            "deliver: no POST /deliver, no fan-out, no narration."
        )
    # Only the read-only route may appear.
    assert "/recent-briefs" in prompt
    assert prompt.count("/deliver") == 0


def test_the_eval_arms_no_delivery_clause_survived_the_append():
    """The block is appended AFTER this clause, so a careless paste could have landed in
    the middle of it. It is the prompt's own guard against an accidental send."""
    prompt = TASK_PROMPT_PATH.read_text(encoding="utf-8")

    assert (
        "Do NOT convert the brief to HTML and do NOT attempt any narration, email, or\n"
        "delivery of any kind" in prompt
    )


def test_the_appended_block_introduces_no_url_the_skill_does_not_list():
    """Every URL the eval arm is handed comes from the skill's `sources.md`. A stray
    extra URL in the block would make the arm measure a different list than the one
    production's `initial_prompt` carries, and the drift test -- which compares the block
    against the generator, not against the skill -- would not notice a URL added to
    BOTH carriers at once."""
    prompt = TASK_PROMPT_PATH.read_text(encoding="utf-8")
    begin = prompt.index(sub.BLOCK_BEGIN_MARKER)
    block = prompt[begin : prompt.index(sub.BLOCK_END_MARKER) + len(sub.BLOCK_END_MARKER)]

    curated = set(sub.extract_source_urls(sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8")))
    in_block = {
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith(("http://", "https://"))
    }

    assert in_block == curated, {
        "in the eval arm's block but not in the skill's sources.md": sorted(in_block - curated),
        "in sources.md but missing from the eval arm's block": sorted(curated - in_block),
    }


def test_the_block_is_the_only_thing_the_eval_arm_adds_to_the_production_wording():
    """Byte arithmetic, independent of the `startswith` check in the sibling module: the
    candidate prompt is exactly the baseline prompt, one separator, the block, one
    newline -- nothing else was slipped in with the paste."""
    baseline = (CANDIDATES_DIR / "haiku-5-5-hardened" / "task-prompt.md").read_bytes()
    candidate = TASK_PROMPT_PATH.read_bytes()
    block = sub.render_source_url_block(
        sub.DEFAULT_SOURCES_PATH.read_text(encoding="utf-8")
    ).encode("utf-8")

    appended = candidate[len(baseline) :]
    extra = len(appended) - len(block)

    assert appended.strip(b"\n") == block
    # ...and the only padding used is newlines, not stray spaces or an invisible
    # character that would make the two carriers' blocks differ in the prompt.
    assert appended.strip() == appended.strip(b"\n")
    assert 0 <= extra <= 2, (
        f"the eval arm adds {extra} byte(s) beyond the baseline prompt and the block itself -- "
        "expected only the newline(s) separating them"
    )
