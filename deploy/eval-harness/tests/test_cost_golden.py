"""GOLDEN TEST (ADR-0016 D2, must-pass): `harness/cost.py` must reproduce the real
2026-07-07 `multiagent-aggressive-haiku` spike run's cost numbers EXACTLY.

Fixtures (`tests/fixtures/threads.json`, `session.json`, `cost.json`) are copied
verbatim from the real captured run at
`deploy/candidates/runs/multiagent-aggressive-haiku/2026-07-07-142718/` (that
directory is gitignored -- these copies are what makes the golden numbers a
committed, reproducible regression fixture).

Deliberately self-contained: this test resolves model/role via the FALLBACK path
(no `candidate_declaration` passed) -- i.e. straight off each thread's own embedded
`agent.model.id` / `agent.name` / `agent.description`, which `threads.json` already
carries per the confirmed live shape. This keeps the golden numbers stable even if
`deploy/candidates/multiagent-aggressive-haiku/`'s live declaration is edited later
for unrelated reasons; the PRIMARY (candidate-declaration-based) resolution path is
covered separately, with synthetic fixtures, in `test_cost.py`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from harness import cost

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# The real run's own captured date (run-summary.json's "timestamp":
# "2026-07-07-142718") -- pinned explicitly so this test's outcome never depends on
# the wall-clock date it happens to run on (see resolve_price_tier's docstring: the
# introductory Sonnet 5 tier expires 2026-08-31; a moving "today" would eventually
# break this fixed historical reproduction).
RUN_DATE = date(2026, 7, 7)

# Reproduced from the real spike's cost.json ("per_thread"), keyed by role, rounded
# to 4dp exactly as harness/cost.py's ThreadCost.cost_usd is.
EXPECTED_PER_THREAD_COST_USD = {
    "coordinator": 0.6023,
    "research": 0.3570,
    "selection": 1.2105,
    "writing": 0.1029,
    "listening-script": 0.0442,
}
EXPECTED_TOTAL_COST_USD = 2.3170


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_golden_run_reproduces_the_spikes_total_and_per_thread_costs():
    threads_payload = _load_fixture("threads.json")
    session_payload = _load_fixture("session.json")
    pricing_table = cost.load_pricing_table()

    breakdown = cost.mine_session_cost(
        session_payload["id"],
        threads_payload["data"],
        pricing_table=pricing_table,
        on_date=RUN_DATE,
    )

    assert breakdown.total_cost_usd == EXPECTED_TOTAL_COST_USD

    actual_by_role = {t.role: t.cost_usd for t in breakdown.threads}
    assert actual_by_role == EXPECTED_PER_THREAD_COST_USD


def test_golden_run_sum_of_thread_usage_equals_session_level_usage():
    """ADR-0016 D2's other confirmed invariant: the session's own total `usage`
    equals the sum of its threads' usage EXACTLY -- proving cost is never double
    counted or dropped across threads."""
    threads_payload = _load_fixture("threads.json")
    session_payload = _load_fixture("session.json")
    pricing_table = cost.load_pricing_table()

    breakdown = cost.mine_session_cost(
        session_payload["id"],
        threads_payload["data"],
        pricing_table=pricing_table,
        on_date=RUN_DATE,
    )

    session_usage = cost.ThreadUsage.from_api_usage(session_payload["usage"])
    assert breakdown.total_usage == session_usage


def test_golden_run_resolves_the_correct_model_per_thread():
    """Sanity check that the fallback (thread-embedded agent object) resolution
    path picked the RIGHT model per role -- Sonnet for coordinator/selection,
    Haiku for research/writing/listening-script -- not just the right dollar
    figure by coincidence."""
    threads_payload = _load_fixture("threads.json")
    session_payload = _load_fixture("session.json")
    pricing_table = cost.load_pricing_table()

    breakdown = cost.mine_session_cost(
        session_payload["id"],
        threads_payload["data"],
        pricing_table=pricing_table,
        on_date=RUN_DATE,
    )

    model_by_role = {t.role: t.model for t in breakdown.threads}
    assert model_by_role["coordinator"] == "claude-sonnet-5"
    assert model_by_role["selection"] == "claude-sonnet-5"
    assert model_by_role["research"] == "claude-haiku-4-5-20251001"
    assert model_by_role["writing"] == "claude-haiku-4-5-20251001"
    assert model_by_role["listening-script"] == "claude-haiku-4-5-20251001"


# --- Every COMMITTED run record must still load and re-price identically ---------------
#
# The 2026-10-11 pricing/schema change (per-model cache overrides, per-tier
# long-prompt rate cards, the Sonnet 5 $3/$15 correction, the new ThreadCost
# field) must be invisible to the eval runs already committed under `runs/`:
# they were costed on the OLD table, their numbers are the comparison baseline
# for the 5.5 eval, and a silent re-pricing would corrupt that comparison. This
# walks every committed `cost.json` and `threads-usage.json` and proves (a) it
# still deserializes, (b) each thread re-prices to the SAME stored cost against
# today's table on the run's own date, (c) the per-thread sum still reproduces
# the stored total, and (d) serializing it back adds no new key.

RUNS_ROOT = Path(__file__).resolve().parent.parent / "runs"


def _committed_cost_records() -> list[Path]:
    return sorted(RUNS_ROOT.glob("*/*/repetitions/*/cost.json")) + sorted(
        RUNS_ROOT.glob("*/*/repetitions/*/threads-usage.json")
    )


def _run_date_of(record_path: Path) -> date:
    """The eval-run-id directory name starts with the run's own date
    (`2026-07-07-174129-cdafb6-...`) -- price against THAT date, not today's, so
    this test stays a fixed historical reproduction."""
    eval_run_id = record_path.parents[2].name
    return date.fromisoformat(eval_run_id[:10])


def test_every_committed_run_record_still_loads_and_reprices_to_the_same_numbers():
    records = _committed_cost_records()
    assert records, "no committed cost records found -- this guarantee would be vacuous"

    pricing_table = cost.load_pricing_table()

    for record_path in records:
        stored = json.loads(record_path.read_text(encoding="utf-8"))
        breakdown = cost.SessionCostBreakdown.from_dict(stored)

        raw_costs = []
        for thread in breakdown.threads:
            tier = cost.resolve_price_tier(pricing_table, thread.model, on_date=_run_date_of(record_path))
            raw = tier.cost_usd(thread.usage)
            raw_costs.append(raw)
            assert round(raw, 4) == thread.cost_usd, f"{record_path} thread {thread.thread_id} re-priced differently"
            assert thread.request_card_counts is None  # no committed record is rate-carded

        assert round(sum(raw_costs), 4) == breakdown.total_cost_usd, f"{record_path} total re-priced differently"
        # (d) round-tripping adds no key -- a committed record's on-disk shape is
        # byte-for-byte what this code still writes for an aggregate-priced run.
        assert breakdown.to_dict() == stored, f"{record_path} did not round-trip unchanged"
