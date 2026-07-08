#!/usr/bin/env python3
"""Trigger + retrieve + record an eval run against a candidate (ADR-0016 D4).

This is the reusable, infra-free, delivery-free eval-run CLI: resolve a named
candidate (`candidate_sync.loader`) -> trigger it N times
(`candidate_sync.trigger.run_candidate`) -> recover its artifacts
(`candidate_sync.trigger.fetch_catted_file_contents`) -> fetch per-thread usage
(`harness.cost.fetch_threads`) -> run the SELECTED SUBSET of the four judges ->
compute cost (`harness.cost.mine_session_cost`) -> write the per-eval-run directory
(`harness.run_store`). A PLAIN LOCAL SCRIPT -- no AWS, no CDK, no Lambda; it calls
the Anthropic API directly, exactly like `deploy/candidates/sync.py`/`trigger.py`.

Usage:
    export ANTHROPIC_API_KEY=$(cat ~/.anthropic-managed-agents/ant-api-key.txt)

    # Trigger a 3-repetition eval run against a real candidate, judging two criteria:
    python3 run.py production-baseline --name "baseline sanity check" \\
        --repetitions 3 --criteria content_selection,factual_accuracy

    # Judge all four v1 criteria (the default), one repetition (the default):
    python3 run.py haiku-swap --name "haiku swap quick look"

    # Fail-loud pricing-table staleness check (no candidate/trigger involved):
    python3 run.py --check-pricing-drift

    # The optional eval email is DEFERRED (ADR-0016 D3) -- this flag exists only
    # to fail loud and explain why, not to send anything:
    python3 run.py production-baseline --email

If a candidate's task prompt uses the `__RECENT_BRIEFS_TOKEN__`/
`__DELIVERY_BASE_URL__` placeholders (ADR-0014 Decision 2d), set
`$RECENT_BRIEFS_SIGNING_KEY`/`$DELIVERY_BASE_URL` first, exactly as
`deploy/candidates/trigger.py` requires -- this script fails loud with the same
clear error if they're needed and missing.

`$EVAL_HARNESS_RUN_ID_OVERRIDE`, if set, is used as the eval-run-id instead of a
freshly computed `run_store.make_eval_run_id(...)` -- `ui.py`'s "trigger" route
sets this so it can redirect to the exact run directory this process will write,
computed BEFORE launching the subprocess (so the id is known immediately, without
waiting for or racing this process's own clock read). Unset in ordinary CLI usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

# Make `candidate_sync` (deploy/candidates/) and `harness`/`eval_core` (this
# package) importable regardless of the caller's cwd -- this repo's established
# per-script sys.path convention (see deploy/candidates/sync.py's own header).
HARNESS_DIR = Path(__file__).resolve().parent
CANDIDATES_DIR = HARNESS_DIR.parent / "candidates"
sys.path.insert(0, str(HARNESS_DIR))
sys.path.insert(0, str(CANDIDATES_DIR))

from candidate_sync import api_client, trigger  # noqa: E402
from candidate_sync.loader import CandidateDeclaration, CandidateLoadError, load_candidate  # noqa: E402

from eval_core.judges import (  # noqa: E402
    judge_content_selection,
    judge_dedup,
    judge_factual_accuracy,
    judge_length_format,
)
from eval_core.judges.base import JudgeResult  # noqa: E402
from eval_core.record import (  # noqa: E402
    V1_CRITERIA,
    CostBreakdownRecord,
    CriterionScore,
    EvalRecord,
    aggregate_replicates,
)

from harness import cost, dedup_priors, local_config, run_store  # noqa: E402

_ENVIRONMENT_JSON_PATH = CANDIDATES_DIR / "environment.json"

# The repo's lockstep copy of the daily-ai-brief skill's curated source list
# (ADR-0008 two-way lockstep: in-repo copy <-> live Skills-API resource) --
# judge methodology v2 (2026-07-07) gives this to the factual-accuracy judge so
# it knows which outlets the brief's OWN research draws from. Read fresh at
# trigger time (not cached at import time) so a live edit to sources.md is
# picked up without restarting anything -- this module does no other file I/O
# outside its own package/candidates trees, but this ONE cross-reference into
# deploy/managed-agent/ is the harness's only READ of that tree (never a write).
_SOURCES_MD_PATH = HARNESS_DIR.parent / "managed-agent" / "skills" / "daily-ai-brief" / "sources.md"

# Matches the eval brief's OWN date out of its artifact filename
# (`AI Brief - YYYY-MM-DD.md`) -- the dedup judge's feed fix
# (harness/dedup_priors.py) needs this to filter prior briefs strictly relative
# to the brief actually being evaluated, not the delivery endpoint's own
# wall-clock "today" (see that module's docstring).
_BRIEF_FILENAME_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# The candidate slug this repo treats as "the current production configuration"
# (D4: "production-config marker for production-baseline"). A candidate's own
# candidate.json MAY set an explicit "is_production_config" flag (checked first,
# so this is overridable without editing this script); the slug is the fallback
# so today's one real production-parity candidate is marked with no extra field
# needed.
_PRODUCTION_CANDIDATE_SLUG = "production-baseline"


def _load_shared_environment_id() -> str:
    """Read the ONE shared `cloud` environment's id from
    deploy/candidates/environment.json -- mirrors
    `deploy/candidates/trigger.py`'s own `_load_shared_environment_id()` exactly
    (small, deliberate duplication of a ~10-line helper rather than importing a
    non-package top-level script, consistent with this repo's existing
    hand-duplication convention for small, independently-reviewable helpers, e.g.
    `recent_briefs_token.py`)."""
    if not _ENVIRONMENT_JSON_PATH.is_file():
        raise SystemExit(
            f"error: {_ENVIRONMENT_JSON_PATH} is missing -- the shared cloud environment "
            "must be created once (see deploy/candidates/README.md) before any candidate can be triggered."
        )
    data = json.loads(_ENVIRONMENT_JSON_PATH.read_text(encoding="utf-8"))
    environment_id = data.get("environment_id")
    if not environment_id:
        raise SystemExit(f"error: {_ENVIRONMENT_JSON_PATH} has no 'environment_id' field")
    return environment_id


def _is_production_config(candidate: CandidateDeclaration) -> bool:
    if "is_production_config" in candidate.candidate_json:
        return bool(candidate.candidate_json["is_production_config"])
    return candidate.slug == _PRODUCTION_CANDIDATE_SLUG


def _declared_models(candidate: CandidateDeclaration) -> list[str]:
    models = {candidate.agent.model} | {sa.model for sa in candidate.sub_agents}
    return sorted(m for m in models if m)


def _declared_parameters(candidate: CandidateDeclaration) -> dict[str, Any]:
    return {
        "agent": candidate.agent.parameters,
        "sub_agents": [
            {"name": sa.name, "model": sa.model, "parameters": sa.parameters} for sa in candidate.sub_agents
        ],
    }


def _find_artifact(artifacts: dict[str, str], *, predicate) -> str | None:
    """Match on the BASENAME of each artifact key, never the raw key.

    `fetch_catted_file_contents()` keys its result by the path exactly as the
    agent typed it in the `cat` command -- in real runs that is the FULL sandbox
    path (`/workspace/AI Brief - 2026-07-07.md`), not a bare filename. Matching
    the raw key against basename-shaped predicates silently returned None for
    every artifact on the first two real validation runs (2026-07-07): the
    judges then scored empty input as `insufficient_data` while the on-disk
    artifact copies (saved via the basename in run_store) looked perfectly
    healthy. Regression-tested with full-path keys in test_run_cli.py."""
    for filename, content in artifacts.items():
        if predicate(PurePosixPath(filename).name):
            return content
    return None


def _extract_named_artifacts(artifacts: dict[str, str]) -> tuple[str | None, str | None, str | None, str | None]:
    """Pick out the skill's four named output files (see
    `deploy/candidates/production-baseline/task-prompt.md`'s output contract) from
    the raw `{filename: content}` map `fetch_catted_file_contents()` returns.
    Returns `(brief_markdown, listening_script, candidates_json_raw,
    source_usage_raw)`, any of which may be None if that file was never `cat`'d.

    DATE-AWARE brief disambiguation (2026-07-08, the day the reviewer-flagged
    ambiguity became REAL): the original version relied on the invariant that a
    task prompt never cats a PRIOR brief, so at most one "AI Brief - *.md" could
    appear among the artifacts and first-match-by-insertion-order was safe. The
    `haiku-swap-hardened` candidate deliberately broke that invariant (its
    Step 1 forcing function cats every prior brief so their content is in
    context), so its artifact map now carries SEVERAL dated brief files and
    first-match could silently return a PRIOR brief as "today's". The brief is
    therefore now selected as the dated `AI Brief - YYYY-MM-DD.md` entry with
    the LEXICALLY GREATEST date (zero-padded ISO dates: lexical == chronological
    -- today's brief always postdates every prior); an undated brief filename
    still matches only when no dated one exists."""
    dated: list[tuple[str, str]] = []  # (date, content)
    undated: str | None = None
    for filename, content in artifacts.items():
        basename = PurePosixPath(filename).name
        if basename.startswith("AI Brief") and basename.endswith(".md"):
            m = _BRIEF_FILENAME_DATE_RE.search(basename)
            if m:
                dated.append((m.group(1), content))
            elif undated is None:
                undated = content
    brief_markdown = max(dated, key=lambda pair: pair[0])[1] if dated else undated
    listening_script = _find_artifact(artifacts, predicate=lambda f: f == "listening-script.txt")
    candidates_json_raw = _find_artifact(artifacts, predicate=lambda f: f == "candidates.json")
    source_usage_raw = _find_artifact(artifacts, predicate=lambda f: f == "source-usage.json")
    return brief_markdown, listening_script, candidates_json_raw, source_usage_raw


def _extract_brief_date(artifacts: dict[str, str]) -> str | None:
    """Parse the eval brief's OWN date ("YYYY-MM-DD") out of its artifact
    filename (`AI Brief - YYYY-MM-DD.md`) -- matched via the SAME basename
    predicate `_extract_named_artifacts()` uses for the brief's content, but kept
    as its own small function (rather than folded into that function's return
    tuple) so `_extract_named_artifacts()`'s existing callers/tests are
    undisturbed by this addition.

    Used exclusively by the dedup judge's v2 feed fix
    (`harness.dedup_priors.fetch_recent_prior_briefs()`) to filter prior briefs
    strictly relative to the brief actually being evaluated -- see that module's
    docstring. Returns None if no brief artifact was found, or its filename
    didn't carry a recognizable date (the caller treats this as "skip the dedup
    priors fetch, let the judge degrade to insufficient_data," never a run
    failure).

    DATE-AWARE (2026-07-08, same fix as `_extract_named_artifacts()`): when a
    candidate's prompt cats PRIOR briefs too (haiku-swap-hardened's Step 1),
    several dated brief files appear here -- the eval brief's own date is the
    GREATEST one, never whichever happened to be catted first."""
    dates = []
    for filename in artifacts:
        basename = PurePosixPath(filename).name
        if basename.startswith("AI Brief") and basename.endswith(".md"):
            match = _BRIEF_FILENAME_DATE_RE.search(basename)
            if match:
                dates.append(match.group(1))
    return max(dates) if dates else None


def _parse_candidates_json(candidates_json_raw: str | None, *, repetition: int) -> list | None:
    """Parse a candidate run's `candidates.json` artifact TOLERANTLY -- an
    artifact-quality problem must degrade the affected judge, never crash the
    run (2026-07-08: a real haiku-digest-sonnet-select run's candidates.json
    carried a raw control character inside a string -- strict json.loads raised
    and took down the ENTIRE already-paid repetition at the judging step).

    Order: strict parse -> retry with strict=False (accepts control characters
    inside strings, the exact real-world failure) -> None with a loud stderr
    diagnostic (the content-selection judge already treats None as
    insufficient_data). A parse that yields anything other than a list is also
    coerced to None -- the judge's summary-building iterates a list."""
    if not candidates_json_raw:
        return None
    for kwargs in ({}, {"strict": False}):
        try:
            parsed = json.loads(candidates_json_raw, **kwargs)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            # Common wrapper shapes a real Haiku run produced ({"candidates":
            # [...]}) -- unwrap a well-known key, else a SINGLE list-valued key.
            for key in ("candidates", "stories", "items"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
            list_values = [v for v in parsed.values() if isinstance(v, list)]
            if len(list_values) == 1:
                return list_values[0]
        print(
            f"CANDIDATES_JSON_UNUSABLE: repetition {repetition}: parsed to "
            f"{type(parsed).__name__} with no unwrappable list -- content_selection will degrade",
            file=sys.stderr,
        )
        return None
    # A REAL failure mode (2026-07-08): the sandbox's bash tool truncates a cat
    # tool_result at ~30K characters and appends a "full output at /tmp/..."
    # notice -- the captured artifact is then structurally incomplete JSON that
    # no parser can honestly rescue (the full file only ever existed in the
    # now-dead sandbox). Name it precisely instead of a generic parse error.
    if "/tmp/" in candidates_json_raw[-300:] or len(candidates_json_raw) >= 29000:
        print(
            f"CANDIDATES_JSON_TRUNCATED: repetition {repetition}: the cat capture was cut at the "
            "sandbox's ~30K tool-output cap (tail carries a truncation notice) -- the artifact is "
            "structurally incomplete; content_selection will degrade. Prevention: keep the "
            "candidates.json schema compact in candidate prompts.",
            file=sys.stderr,
        )
        return None
    print(
        f"CANDIDATES_JSON_UNPARSEABLE: repetition {repetition}: not valid JSON even with "
        "strict=False -- content_selection will degrade to insufficient_data",
        file=sys.stderr,
    )
    return None


def _load_sources_md() -> str:
    """Read the daily-ai-brief skill's curated source list (the repo's in-lockstep
    copy, ADR-0008) -- judge methodology v2 gives this to the factual-accuracy
    judge so it knows which outlets the brief's own research draws from. Fails
    loud (a missing file means the judge would silently score with zero source
    context, which is worse than refusing to run) rather than degrading to an
    empty string."""
    if not _SOURCES_MD_PATH.is_file():
        raise SystemExit(
            f"error: {_SOURCES_MD_PATH} is missing -- cannot brief the factual-accuracy judge "
            "on the curated source list"
        )
    return _SOURCES_MD_PATH.read_text(encoding="utf-8")


# --- Judge dispatch ------------------------------------------------------------------


def _build_anthropic_client(api_key: str) -> Any:
    """A thin wrapper so tests can monkeypatch this one function to inject a fake
    client, exactly like `candidate_sync/trigger.py`'s tests monkeypatch
    `run_candidate` itself -- avoids importing the real `anthropic` package at
    module-import time in a test process that never needs it."""
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def _run_selected_judges(
    client: Any,
    criteria: list[str],
    *,
    brief_markdown: str,
    candidates_json: list[dict] | None,
    sources_md: str,
    prior_briefs: list[dict[str, str]],
) -> dict[str, JudgeResult]:
    """Run only the judges in `criteria` (PRD §4.1: "which eval criteria... a
    subset -- no need to test all every run"). A criterion absent from `criteria`
    is simply absent from the returned dict -- the record schema already treats a
    missing criterion as "blank/untested" (D4: "the full set of criteria... blank
    for criteria a run didn't test"), distinct from `insufficient_data=True` (which
    means "tested, but the judge couldn't score it").

    `sources_md` (judge methodology v2) is only used by factual_accuracy;
    `prior_briefs` (v2's dedup feed fix -- `{"date", "markdown"}` dicts, already
    filtered strictly relative to this brief's own date) is only used by dedup --
    both are accepted unconditionally so the caller doesn't need to conditionally
    build them per-criterion."""
    results: dict[str, JudgeResult] = {}
    if "content_selection" in criteria:
        results["content_selection"] = judge_content_selection(
            client, candidates_json=candidates_json, brief_markdown=brief_markdown
        )
    if "factual_accuracy" in criteria:
        results["factual_accuracy"] = judge_factual_accuracy(
            client, brief_markdown=brief_markdown, sources_md=sources_md
        )
    if "length_format" in criteria:
        results["length_format"] = judge_length_format(client, brief_markdown=brief_markdown)
    if "dedup" in criteria:
        results["dedup"] = judge_dedup(client, brief_markdown=brief_markdown, priors=prior_briefs)
    return results


def _scores_to_dict(judge_results: dict[str, JudgeResult]) -> dict[str, dict[str, Any]]:
    """Build one repetition's `scores.json` (ADR-0016 D4). ADDITIVE (judge
    methodology v2): the original `{score, rationale, evidence,
    insufficient_data}` shape is unchanged; `findings`/`selection_disagreements`
    are included ONLY when a judge's result actually carries one (a v1-shaped
    judge, or a v2 judge's degrade path that never called the API, simply omits
    the key -- never an empty-list placeholder that would look like "the judge
    checked nothing")."""
    out: dict[str, dict[str, Any]] = {}
    for criterion, result in judge_results.items():
        entry: dict[str, Any] = {
            "score": result.score,
            "rationale": result.rationale,
            "evidence": result.evidence,
            "insufficient_data": result.insufficient_data,
        }
        if result.findings is not None:
            entry["findings"] = result.findings
        if result.selection_disagreements is not None:
            entry["selection_disagreements"] = result.selection_disagreements
        out[criterion] = entry
    return out


def _price_judge_results(
    judge_results: dict[str, JudgeResult], *, pricing_table: dict[str, Any], on_date: date
) -> dict[str, Any]:
    """Build one repetition's `judge-cost.json` (review-fix: ADR-0016 reviewer
    Medium, "judge cost accounting"; judge methodology v2 amends this for
    per-judge model config + web-search cost).

    Prices EVERY judge call's captured TOKEN usage against ITS OWN recorded
    model (`result.model` -- judge methodology v2's per-judge config means this
    is no longer a single shared constant) via `cost.price_usage()`, kept
    entirely SEPARATE from `harness.cost.mine_session_cost()`'s pipeline cost
    (this function never touches `SessionCostBreakdown`/`cost.json`). Raises
    `cost.UnknownModelPriceError` (fails loud, per the task's explicit
    requirement) if any judge's model has no `pricing.json` entry -- this SHOULD
    never happen in practice (every `JUDGE_MODELS` entry has a corresponding
    `pricing.json` family), but is not silently tolerated if it ever drifts.

    ALSO prices each judge's `search_count` (web-search tool invocations) via
    `cost.price_web_searches()` -- a SEPARATE, flat, per-call cost axis from
    token usage (kept in its own `search_cost_usd`/`total_search_cost_usd`
    fields, never summed into `cost_usd`/`total_cost_usd`, per the task's
    "keep it clearly separated from token cost" requirement).
    `grand_total_cost_usd` is provided as the one convenience sum of both axes
    for a caller that just wants "how much did judging cost, all-in."""
    total_usage = cost.ThreadUsage()
    per_criterion: dict[str, Any] = {}
    total_token_cost_usd = 0.0
    total_search_count = 0
    models_used: set[str] = set()

    for criterion, result in judge_results.items():
        usage = result.usage
        model = result.model
        models_used.add(model)

        criterion_token_cost = cost.price_usage(usage, model=model, pricing_table=pricing_table, on_date=on_date)
        search_count = result.search_count
        # search_count is the ACTUAL number of billed searches, captured from the
        # API response's usage.server_tool_use.web_search_requests by
        # base._extract_search_count() (defaulting to 0 when the response carries
        # no server_tool_use at all -- e.g. a tool-less judge). It is NOT the
        # judge's declared max_uses cap; that cap only bounds it from above.
        # price_web_searches() returns 0.0 for search_count<=0, so a zero is
        # priced correctly, never silently dropped. (Comment corrected per the
        # 2026-07-07 security review's L-2 note -- the code was always right.)
        criterion_search_cost = cost.price_web_searches(search_count, pricing_table=pricing_table)

        total_token_cost_usd += criterion_token_cost
        total_search_count += search_count
        total_usage = total_usage + cost.ThreadUsage.from_dict(usage)
        per_criterion[criterion] = {
            "model": model,
            "usage": usage,
            "cost_usd": round(criterion_token_cost, 4),
            "search_count": search_count,
            "search_cost_usd": round(criterion_search_cost, 4),
        }

    total_search_cost_usd = cost.price_web_searches(total_search_count, pricing_table=pricing_table)
    total_token_cost_usd = round(total_token_cost_usd, 4)
    total_search_cost_usd = round(total_search_cost_usd, 4)

    return {
        "models": sorted(models_used),
        "total_cost_usd": total_token_cost_usd,
        "total_search_count": total_search_count,
        "total_search_cost_usd": total_search_cost_usd,
        "grand_total_cost_usd": round(total_token_cost_usd + total_search_cost_usd, 4),
        "total_usage": total_usage.to_dict(),
        "per_criterion": per_criterion,
    }


# --- CLI -------------------------------------------------------------------------


def _parse_criteria(raw: str) -> list[str]:
    requested = [c.strip() for c in raw.split(",") if c.strip()]
    unknown = [c for c in requested if c not in V1_CRITERIA]
    if unknown:
        raise SystemExit(f"error: unknown criteria {unknown} -- valid criteria are {list(V1_CRITERIA)}")
    return requested


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("candidate_slug", nargs="?", default=None, help="e.g. production-baseline, haiku-swap")
    parser.add_argument("--name", default=None, help="A name for this eval run (default: '<slug> ad-hoc run')")
    parser.add_argument("--repetitions", type=int, default=1, help="Number of sequential repetitions (default: 1)")
    parser.add_argument(
        "--criteria",
        default=",".join(V1_CRITERIA),
        help=f"Comma-separated subset of {list(V1_CRITERIA)} (default: all four)",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="DEFERRED (ADR-0016 D3) -- this flag exits with an error, it does not send anything.",
    )
    parser.add_argument("--check-pricing-drift", action="store_true", help="Check pricing.json for staleness and exit.")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "Proceed even if deploy/candidates/<slug> has uncommitted changes (the recorded "
            "git_ref would then NOT match what actually ran -- eval-run.json is marked "
            "declaration_dirty=true so the UI can flag it as not-reproducible-from-ref)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=trigger.DEFAULT_POLL_TIMEOUT_SECONDS,
        help="Poll timeout in seconds per repetition (default matches candidate_sync.trigger's own default).",
    )
    parser.add_argument("--runs-root", default=None, help="Override the runs/ directory (mainly for tests).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.check_pricing_drift:
        pricing_table = cost.load_pricing_table()
        issues = cost.check_pricing_drift(pricing_table, on_date=date.today())
        if issues:
            for issue in issues:
                print(f"PRICING_DRIFT: {issue}", file=sys.stderr)
            return 1
        print("PRICING_OK: every declared model family has a price tier covering today.")
        return 0

    if args.email:
        print("error: --email is deferred -- see docs/adr/0016-eval-harness-reintegration.md D3", file=sys.stderr)
        return 1

    if not args.candidate_slug:
        parser.error("candidate_slug is required unless --check-pricing-drift is given")

    if args.repetitions < 1:
        parser.error("--repetitions must be >= 1")

    criteria = _parse_criteria(args.criteria)
    if not criteria:
        parser.error("--criteria resolved to an empty set -- at least one criterion is required")

    try:
        api_key = api_client.get_anthropic_api_key()
    except api_client.AnthropicApiKeyMissingError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    candidate_dir = CANDIDATES_DIR / args.candidate_slug
    try:
        candidate = load_candidate(candidate_dir)
    except CandidateLoadError as e:
        print(f"error loading candidate: {e}", file=sys.stderr)
        return 1

    # Dirty-working-tree guard (review-fix: reviewer Medium) -- load_candidate()
    # above just read LIVE files; current_git_ref() below records HEAD. An
    # uncommitted edit under deploy/candidates/<slug> makes those two silently
    # disagree, and the recorded git_ref would then lie about what actually ran.
    # FAIL LOUD by default; --allow-dirty proceeds and marks the record instead.
    declaration_dirty = run_store.candidate_declaration_is_dirty(candidate_dir)
    if declaration_dirty and not args.allow_dirty:
        print(
            f"error: deploy/candidates/{candidate.slug} has uncommitted changes -- the recorded "
            "git ref would not match what load_candidate() actually read. Commit your changes, "
            "or pass --allow-dirty to proceed anyway (the run will be marked declaration_dirty=true "
            "and is not reproducible via `git show <ref>:<path>`).",
            file=sys.stderr,
        )
        return 1

    if candidate.agent.agent_id is None:
        print(
            f"error: candidate '{candidate.slug}' has no agent_id yet -- run "
            "deploy/candidates/sync.py against it first",
            file=sys.stderr,
        )
        return 1

    task_prompt = candidate.agent.task_prompt
    if not task_prompt:
        print(f"error: candidate '{candidate.slug}' has no task prompt (task-prompt.md is empty)", file=sys.stderr)
        return 1

    try:
        # Resolve the two recent-briefs values through local_config (env var ->
        # well-known local key file -> committed default URL) instead of relying
        # on the raw process environment alone -- a UI/server process launched
        # without the exports must still be able to trigger (owner-reported
        # failure, 2026-07-07). Passing None for a missing key lets
        # substitute_recent_briefs_placeholders() raise its usual fail-loud
        # error, which we extend with the file-source hint below.
        task_prompt = trigger.substitute_recent_briefs_placeholders(
            task_prompt,
            signing_key=local_config.resolve_recent_briefs_signing_key(),
            delivery_base_url=local_config.resolve_delivery_base_url(),
        )
    except trigger.RecentBriefsPlaceholderConfigError as e:
        print(f"error: {e} -- {local_config.signing_key_sources_hint()}", file=sys.stderr)
        return 1

    environment_id = _load_shared_environment_id()
    git_ref = run_store.current_git_ref()
    eval_run_name = args.name or f"{candidate.slug} ad-hoc run"
    eval_run_id = os.environ.get("EVAL_HARNESS_RUN_ID_OVERRIDE") or run_store.make_eval_run_id(eval_run_name)
    runs_root = Path(args.runs_root) if args.runs_root else None
    run_dir = run_store.eval_run_dir(candidate.slug, eval_run_id, runs_root=runs_root)

    meta = run_store.EvalRunMeta(
        name=eval_run_name,
        slug=candidate.slug,
        agent_id=candidate.agent.agent_id,
        git_ref=git_ref,
        composition="multi-agent" if candidate.is_multi_agent else "single-agent",
        models=_declared_models(candidate),
        parameters=_declared_parameters(candidate),
        repetitions=args.repetitions,
        criteria=criteria,
        state=run_store.STATE_CONFIGURED,
        email_sent=False,
        is_production_config=_is_production_config(candidate),
        created_at=int(time.time()),
        declaration_dirty=declaration_dirty,
    )
    run_store.write_eval_run_meta(run_dir, meta)
    if declaration_dirty:
        print(f"EVAL_RUN_DECLARATION_DIRTY: {candidate.slug} {eval_run_id} -- proceeding via --allow-dirty", file=sys.stderr)
    print(f"EVAL_RUN_CONFIGURED: {candidate.slug} {eval_run_id} ({args.repetitions} repetition(s), criteria={criteria})")

    run_store.update_state(run_dir, run_store.STATE_RUNNING)

    pricing_table = cost.load_pricing_table()
    anthropic_client = _build_anthropic_client(api_key)

    # Judge methodology v2: the curated source list is static content, read ONCE
    # per run.py invocation (not per repetition) -- only when factual_accuracy is
    # actually selected, so a run that never tests it never even requires
    # sources.md to exist.
    sources_md = _load_sources_md() if "factual_accuracy" in criteria else ""

    records: list[EvalRecord] = []
    judge_cost_totals: list[float] = []
    any_repetition_failed = False

    for index in range(1, args.repetitions + 1):
        deployment_name = f"eval-{candidate.slug}-{eval_run_id}-{index:02d}"
        print(f"EVAL_REPETITION_START: {index}/{args.repetitions} ({deployment_name})")
        try:
            with trigger.build_deployments_client(api_key) as deployments_client:
                result = trigger.run_candidate(
                    deployments_client,
                    agent_id=candidate.agent.agent_id,
                    environment_id=environment_id,
                    task_prompt=task_prompt,
                    deployment_name=deployment_name,
                    poll_timeout_seconds=args.timeout,
                )
                threads = cost.fetch_threads(deployments_client, result.session_id)
        except (
            trigger.CandidateRunFailedError,
            trigger.CandidateRunTimeoutError,
            trigger.CandidateRunEventsNotSettledError,
        ) as e:
            print(f"EVAL_REPETITION_FAILED: {index}/{args.repetitions}: {e}", file=sys.stderr)
            run_store.write_run_meta(run_dir, index, {"final_status": "failed", "error": str(e)})
            any_repetition_failed = True
            continue

        # CONTAINMENT (2026-07-08): everything below the trigger is post-payment
        # bookkeeping -- artifact extraction, judging, and record writing. Two real
        # runs crashed HERE (a str-typed candidates.json entry; a control character
        # in candidates.json) and took the whole process down with the paid events
        # already on disk. An exception in this phase now marks the repetition
        # failed (run-meta records the error; state finalizes failed below) instead
        # of killing the run -- fail loud in the record, never crash the harness.
        try:
            artifacts = trigger.fetch_catted_file_contents(result.events)
            run_store.write_artifacts(run_dir, index, artifacts)
            run_store.write_events(run_dir, index, result.events)

            breakdown = cost.mine_session_cost(
                result.session_id, threads, pricing_table=pricing_table, on_date=date.today(), candidate_declaration=candidate
            )
            run_store.write_threads_usage(run_dir, index, breakdown)
            run_store.write_cost(run_dir, index, breakdown)

            brief_markdown, listening_script, candidates_json_raw, _source_usage_raw = _extract_named_artifacts(artifacts)
            candidates_json = _parse_candidates_json(candidates_json_raw, repetition=index)

            prior_briefs: list[dict[str, str]] = []
            if "dedup" in criteria and brief_markdown:
                brief_date = _extract_brief_date(artifacts)
                if brief_date is None:
                    print(
                        f"DEDUP_PRIORS_SKIPPED: repetition {index}: could not parse a brief date from the "
                        "artifact filenames -- dedup will degrade to insufficient_data",
                        file=sys.stderr,
                    )
                else:
                    # Same local_config resolution as the task-prompt substitution above --
                    # missing key stays a soft degrade here (no priors, judge reports
                    # insufficient_data), never a run failure. brief_date drives the v2
                    # feed fix (harness/dedup_priors.py): priors are filtered strictly
                    # relative to THIS brief's own date, not the delivery endpoint's own
                    # wall-clock "today".
                    prior_briefs = dedup_priors.fetch_recent_prior_briefs(
                        brief_date=brief_date,
                        signing_key=local_config.resolve_recent_briefs_signing_key() or "",
                        delivery_base_url=local_config.resolve_delivery_base_url(),
                    )

            judge_results = _run_selected_judges(
                anthropic_client,
                criteria,
                brief_markdown=brief_markdown or "",
                candidates_json=candidates_json,
                sources_md=sources_md,
                prior_briefs=prior_briefs,
            )
            run_store.write_scores(run_dir, index, _scores_to_dict(judge_results))

            judge_cost_data = _price_judge_results(judge_results, pricing_table=pricing_table, on_date=date.today())
            run_store.write_judge_cost(run_dir, index, judge_cost_data)
            judge_cost_totals.append(judge_cost_data["total_cost_usd"])

            run_store.write_run_meta(
                run_dir,
                index,
                {
                    "deployment_id": result.deployment_id,
                    "session_id": result.session_id,
                    "thread_count": len(threads),
                    "final_status": result.final_status,
                    "timestamp": time.strftime("%Y-%m-%d-%H%M%S"),
                },
            )

            records.append(
                EvalRecord(
                    run_id=f"{eval_run_id}-{index:02d}",
                    candidate_config_id=candidate.slug,
                    session_id=result.session_id,
                    created_at=int(time.time()),
                    criterion_scores={
                        criterion: CriterionScore(
                            criterion=criterion, score=r.score, rationale=r.rationale, evidence=r.evidence, insufficient_data=r.insufficient_data
                        )
                        for criterion, r in judge_results.items()
                    },
                    cost=CostBreakdownRecord(
                        total_cost_usd=breakdown.total_cost_usd,
                        thread_costs_usd={t.role: t.cost_usd for t in breakdown.threads},
                    ),
                    brief_markdown=brief_markdown,
                    listening_script=listening_script,
                )
            )
            print(
                f"EVAL_REPETITION_COMPLETE: {index}/{args.repetitions} cost=${breakdown.total_cost_usd} "
                f"judge_cost=${judge_cost_data['total_cost_usd']}"
            )
        except Exception as e:  # noqa: BLE001 -- see containment note above
            print(
                f"EVAL_REPETITION_JUDGING_FAILED: {index}/{args.repetitions}: {e!r}",
                file=sys.stderr,
            )
            run_store.write_run_meta(
                run_dir, index, {"final_status": "judging_failed", "error": repr(e)}
            )
            any_repetition_failed = True
            continue

    if records:
        aggregate = aggregate_replicates(records)
        summary = _aggregate_to_dict(aggregate)
        # Judge cost is rolled up SEPARATELY from `aggregate_replicates()`'s own
        # `mean_cost_usd`/`cost_stdev_usd` (the PIPELINE cost) -- a distinct
        # top-level `judge_cost` block, never merged into it (review-fix: "do NOT
        # fold it into the pipeline cost column").
        summary["judge_cost"] = {
            "n": len(judge_cost_totals),
            "mean_cost_usd": statistics.mean(judge_cost_totals) if judge_cost_totals else None,
            "stdev_cost_usd": statistics.stdev(judge_cost_totals) if len(judge_cost_totals) >= 2 else None,
        }
        run_store.write_summary(run_dir, summary)

    run_store.write_human_eval_placeholder(run_dir)

    final_state = run_store.STATE_FAILED if (any_repetition_failed or not records) else run_store.STATE_COMPLETED
    run_store.update_state(run_dir, final_state)

    print(f"EVAL_RUN_{final_state.upper()}: {candidate.slug} {eval_run_id} ({len(records)}/{args.repetitions} repetition(s) recorded)")
    return 0 if final_state == run_store.STATE_COMPLETED else 1


def _aggregate_to_dict(aggregate: Any) -> dict[str, Any]:
    """`CandidateAggregate`/`CriterionAggregate` are plain frozen dataclasses with
    no `to_dict()` of their own (unlike `EvalRecord`) -- `dataclasses.asdict()`
    handles their nested-dataclass shape correctly with no custom serialization
    needed."""
    import dataclasses

    return dataclasses.asdict(aggregate)


if __name__ == "__main__":
    raise SystemExit(main())
