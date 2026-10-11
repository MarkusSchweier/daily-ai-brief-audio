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


def _aggregate_on_card(tier: cost.PriceTier, usage: cost.ThreadUsage, *, long_card: bool) -> float:
    """What this thread's AGGREGATE usage would cost if every one of its requests
    had landed on one specific card -- the two bounds a correct per-request total
    must lie between (REVIEWER-ADDED 2026-10-11; see
    `test_every_committed_run_record_still_loads_and_reprices_to_the_same_numbers`'s
    rate-carded branch for why a bound, rather than an exact re-price, is the
    strongest check available from a COMMITTED record alone)."""
    input_per_million = tier.long_prompt_input_per_million_usd if long_card else tier.input_per_million_usd
    output_per_million = tier.long_prompt_output_per_million_usd if long_card else tier.output_per_million_usd
    return tier._cost_on_card(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_creation_5m_input_tokens=usage.cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=usage.cache_creation_1h_input_tokens,
        input_per_million_usd=float(input_per_million or 0.0),
        output_per_million_usd=float(output_per_million or 0.0),
    )


def _sibling_request_usages(record_path: Path) -> list[cost.RequestUsage] | None:
    """The record's own `events.json`, if it is still on disk. It is GITIGNORED
    (`runs/**/events.json`), so a freshly-produced record has it and a record
    cloned from git does NOT -- hence `None` rather than an assertion. Note the
    on-disk wrapper: `run_store.write_events()` stores `{"events": [...]}`, not the
    bare list `mine_session_cost(events=...)` is handed in-process."""
    events_path = record_path.parent / "events.json"
    if not events_path.is_file():
        return None
    payload = json.loads(events_path.read_text(encoding="utf-8"))
    events = payload.get("events", payload) if isinstance(payload, dict) else payload
    return cost.extract_request_usages(events)


def test_every_committed_run_record_still_loads_and_reprices_to_the_same_numbers():
    records = _committed_cost_records()
    assert records, "no committed cost records found -- this guarantee would be vacuous"

    pricing_table = cost.load_pricing_table()
    aggregate_threads = 0
    rate_carded_threads = 0

    for record_path in records:
        stored = json.loads(record_path.read_text(encoding="utf-8"))
        breakdown = cost.SessionCostBreakdown.from_dict(stored)

        raw_costs = []
        for thread in breakdown.threads:
            tier = cost.resolve_price_tier(pricing_table, thread.model, on_date=_run_date_of(record_path))

            if not tier.has_long_prompt_card:
                # AGGREGATE-PRICED (every record written before 2026-10-11, and every
                # Sonnet/Opus/Haiku-4.5 thread after it): re-price exactly.
                aggregate_threads += 1
                raw = tier.cost_usd(thread.usage)
                raw_costs.append(raw)
                assert round(raw, 4) == thread.cost_usd, (
                    f"{record_path} thread {thread.thread_id} re-priced differently"
                )
                assert thread.request_card_counts is None, (
                    f"{record_path} thread {thread.thread_id} is aggregate-priced but carries "
                    "request_card_counts"
                )
                continue

            # RATE-CARDED (Claude Haiku 5.5 -- REVIEWER-ADDED branch, 2026-10-11).
            # Aggregate usage cannot reproduce this number by construction, so the
            # original `tier.cost_usd(thread.usage)` call RAISED here the moment the
            # first Haiku 5.5 record landed under runs/ (which is exactly what this
            # branch of the repo exists to produce). Two checks instead:
            #   1. always: the record must carry a per-card breakdown, and its stored
            #      cost must lie between the two single-card aggregates -- a bound
            #      that needs no events and is still tight enough to catch a thread
            #      priced wholly on the wrong card (for the first real Haiku 5.5 run
            #      those bounds are $0.1267 and $0.6334 around a stored $0.5920);
            #   2. when the gitignored events.json is still on disk: the exact
            #      per-request re-price, to the cent, plus matching card counts.
            rate_carded_threads += 1
            assert thread.request_card_counts is not None, (
                f"{record_path} thread {thread.thread_id} is rate-carded but records no "
                "request_card_counts -- the card split that produced its cost is unauditable"
            )
            assert sum(thread.request_card_counts.values()) > 0, f"{record_path} records zero priced requests"

            cheap_bound = _aggregate_on_card(tier, thread.usage, long_card=False)
            expensive_bound = _aggregate_on_card(tier, thread.usage, long_card=True)
            assert cheap_bound - 1e-9 <= thread.cost_usd <= expensive_bound + 1e-9, (
                f"{record_path} thread {thread.thread_id} stored cost {thread.cost_usd} is outside the "
                f"two single-card bounds [{cheap_bound:.4f}, {expensive_bound:.4f}]"
            )

            requests = _sibling_request_usages(record_path)
            if requests is None:
                # No events on disk (a git clone): fall back to the stored cost so the
                # total-vs-threads check below still runs.
                raw_costs.append(thread.cost_usd)
                continue

            cost.verify_request_usages_match_thread(
                requests, thread.usage, model_id=thread.model, thread_id=thread.thread_id
            )
            raw, counts = cost.price_requests(requests, price_tier=tier)
            raw_costs.append(raw)
            assert round(raw, 4) == thread.cost_usd, (
                f"{record_path} thread {thread.thread_id} re-priced differently per request"
            )
            assert counts == thread.request_card_counts, (
                f"{record_path} thread {thread.thread_id} card split changed: {counts} != "
                f"{thread.request_card_counts}"
            )

        assert round(sum(raw_costs), 4) == breakdown.total_cost_usd, f"{record_path} total re-priced differently"
        # (d) round-tripping adds no key -- a record's on-disk shape is byte-for-byte
        # what this code still writes (aggregate records carry no
        # `request_card_counts`; rate-carded ones carry exactly one).
        assert breakdown.to_dict() == stored, f"{record_path} did not round-trip unchanged"

    # Guard against the aggregate branch quietly becoming vacuous (it holds the
    # "pre-2026-10-11 records must not re-price" guarantee).
    assert aggregate_threads >= 12, f"only {aggregate_threads} aggregate-priced threads walked"
