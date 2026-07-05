"""The structured, versioned, per-run evaluation record (PRD docs/prd/eval-harness.md
FR-16/FR-17, AC-16/AC-17) plus replicate aggregation (FR-3/FR-17) and freeze-and-replay
(FR-5).

Schema versioned so a deferred criterion (FR-8/FR-10/FR-12/FR-13) can be added later
without breaking existing records or their consumers (FR-16) -- `SCHEMA_VERSION` is
the single thing a later migration bumps; every dataclass below serializes to a plain
JSON-safe dict via `to_dict()`/`from_dict()` so it's storable in DynamoDB (as a JSON
string attribute or a native Map) or S3 (as a `.json` object) per whichever the CDK
stack (Phase 6) picks for a given field.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# The v1 criteria set this epic implements (PRD §4.B) -- FR-8/FR-10/FR-12/FR-13 are
# deferred, not built, and therefore never appear as keys in `criterion_scores` today;
# adding them later is purely additive (new keys), not a schema break.
V1_CRITERIA = ("content_selection", "factual_accuracy", "length_format", "dedup")


@dataclass(frozen=True)
class HumanOverride:
    """A reviewer's agree/override + optional comment for one criterion (PRD FR-19).

    `overridden_score` is None when the reviewer agreed with the judge outright
    (still recorded, so "a human looked at this and agreed" is distinguishable from
    "no human has reviewed this yet" -- see EvalRecord.human_overrides being an empty
    dict in the latter case)."""

    criterion: str
    agreed: bool
    overridden_score: int | None
    comment: str
    reviewer: str = ""
    reviewed_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HumanOverride":
        return cls(**data)


@dataclass(frozen=True)
class CriterionScore:
    """One criterion's judge output, embedded in a run record (mirrors
    `eval_core.judges.base.JudgeResult` but is the RECORD's own serializable shape --
    kept as a separate type so the record schema doesn't couple to the judges'
    internal representation)."""

    criterion: str
    score: int | None
    rationale: str
    evidence: str
    insufficient_data: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CriterionScore":
        return cls(**data)


@dataclass(frozen=True)
class CostBreakdownRecord:
    """The run's cost breakdown (Phase 2's `cost_miner.SessionCostBreakdown`),
    flattened to a JSON-safe shape for storage. Kept separate from
    `cost_miner.SessionCostBreakdown` (which uses nested dataclasses better suited to
    in-process computation) so a change to the miner's internal representation
    doesn't automatically become a record-schema break."""

    total_cost_usd: float
    phase_costs_usd: dict[str, float] = field(default_factory=dict)
    thread_costs_usd: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CostBreakdownRecord":
        return cls(**data)


@dataclass(frozen=True)
class EvalRecord:
    """One evaluation run's full structured record (PRD FR-16/AC-16).

    `candidate_config_id` is the named candidate configuration under test (FR-2) --
    in this epic, the current production configuration is the only real candidate;
    the field exists now so results are comparable across candidates once the
    follow-up cost-optimization epic introduces others. `research_frozen_id`, when
    set, means this run's writing phase consumed a FROZEN research output (FR-5)
    rather than running its own research -- see `FrozenResearchOutput` below.
    """

    run_id: str
    candidate_config_id: str
    session_id: str
    created_at: int
    criterion_scores: dict[str, CriterionScore] = field(default_factory=dict)
    cost: CostBreakdownRecord | None = None
    human_overrides: dict[str, HumanOverride] = field(default_factory=dict)
    research_frozen_id: str | None = None
    schema_version: int = SCHEMA_VERSION
    # AC-18: the review UI's detail view must show the brief content and its
    # listening script side by side with the judge scores. Inlined directly (not an
    # S3-key pointer) -- the daily-ai-brief skill targets 8-15 headlines / 5-10 deep
    # dives (see deploy/managed-agent/skills/daily-ai-brief/SKILL.md), which is
    # comfortably tens of KB, nowhere near DynamoDB's 400KB item limit -- inlining is
    # simpler and needs no additional S3 IAM grant on the `read` Lambda.
    brief_markdown: str | None = None
    listening_script: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "candidate_config_id": self.candidate_config_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "criterion_scores": {k: v.to_dict() for k, v in self.criterion_scores.items()},
            "cost": self.cost.to_dict() if self.cost is not None else None,
            "human_overrides": {k: v.to_dict() for k, v in self.human_overrides.items()},
            "research_frozen_id": self.research_frozen_id,
            "schema_version": self.schema_version,
            "brief_markdown": self.brief_markdown,
            "listening_script": self.listening_script,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalRecord":
        return cls(
            run_id=data["run_id"],
            candidate_config_id=data["candidate_config_id"],
            session_id=data["session_id"],
            created_at=data["created_at"],
            criterion_scores={k: CriterionScore.from_dict(v) for k, v in (data.get("criterion_scores") or {}).items()},
            cost=CostBreakdownRecord.from_dict(data["cost"]) if data.get("cost") else None,
            human_overrides={k: HumanOverride.from_dict(v) for k, v in (data.get("human_overrides") or {}).items()},
            research_frozen_id=data.get("research_frozen_id"),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            brief_markdown=data.get("brief_markdown"),
            listening_script=data.get("listening_script"),
        )

    def effective_score(self, criterion: str) -> int | None:
        """The score a downstream consumer should treat as authoritative for
        `criterion`: a human override's score when present (FR-19: "a human signal
        distinct from the automated judge score" -- but still the one that should win
        when deciding whether to ship), else the judge's own score."""
        override = self.human_overrides.get(criterion)
        if override is not None and override.overridden_score is not None:
            return override.overridden_score
        judge_result = self.criterion_scores.get(criterion)
        return judge_result.score if judge_result else None


# --- Replicate aggregation (FR-3/FR-17) ---------------------------------------------


@dataclass(frozen=True)
class CriterionAggregate:
    """Per-criterion central tendency + variance across a candidate's replicate set
    (PRD FR-17/AC-17). `n` is how many replicates contributed a real (non-insufficient)
    score for this criterion -- may be less than the total replicate count when some
    runs degraded to insufficient_data (e.g. content_selection with no candidates
    artifact on some runs)."""

    criterion: str
    n: int
    mean: float | None
    stdev: float | None  # None when n < 2 (stdev is undefined for fewer than 2 samples)
    min: int | None
    max: int | None


@dataclass(frozen=True)
class CandidateAggregate:
    """The aggregate record for one candidate configuration's replicate set (PRD
    FR-17/AC-17): per-criterion central tendency + variance, plus aggregate cost."""

    candidate_config_id: str
    replicate_count: int
    criterion_aggregates: dict[str, CriterionAggregate]
    mean_cost_usd: float | None
    cost_stdev_usd: float | None


def aggregate_replicates(records: list[EvalRecord]) -> CandidateAggregate:
    """Aggregate N replicate `EvalRecord`s for the SAME candidate configuration into a
    `CandidateAggregate` (PRD FR-3 default n=3, FR-17). Uses each record's
    `effective_score()` (human override wins over the raw judge score, if present) so
    the aggregate reflects the best-known signal, not stale pre-review judge output.

    Degrades gracefully per-criterion: a criterion that was insufficient_data (or
    simply absent) on some replicates is aggregated only over the replicates that DID
    produce a real score for it -- never silently coerced to 0, and never raises for
    an empty input list (returns n=0/mean=None for every criterion found across all
    records, or an empty aggregate if `records` itself is empty).
    """
    if not records:
        return CandidateAggregate(
            candidate_config_id="",
            replicate_count=0,
            criterion_aggregates={},
            mean_cost_usd=None,
            cost_stdev_usd=None,
        )

    candidate_config_id = records[0].candidate_config_id

    all_criteria: set[str] = set()
    for record in records:
        all_criteria.update(record.criterion_scores.keys())

    criterion_aggregates: dict[str, CriterionAggregate] = {}
    for criterion in sorted(all_criteria):
        scores = [
            score
            for record in records
            if (score := record.effective_score(criterion)) is not None
        ]
        criterion_aggregates[criterion] = CriterionAggregate(
            criterion=criterion,
            n=len(scores),
            mean=statistics.mean(scores) if scores else None,
            stdev=statistics.stdev(scores) if len(scores) >= 2 else None,
            min=min(scores) if scores else None,
            max=max(scores) if scores else None,
        )

    costs = [record.cost.total_cost_usd for record in records if record.cost is not None]

    return CandidateAggregate(
        candidate_config_id=candidate_config_id,
        replicate_count=len(records),
        criterion_aggregates=criterion_aggregates,
        mean_cost_usd=statistics.mean(costs) if costs else None,
        cost_stdev_usd=statistics.stdev(costs) if len(costs) >= 2 else None,
    )


# --- Freeze-and-replay (FR-5) --------------------------------------------------------


@dataclass(frozen=True)
class FrozenResearchOutput:
    """A completed research phase's output, marked frozen and independently
    referenceable so it can be replayed through one or more writing-phase
    configurations without re-running (and re-paying for) research (PRD FR-5/AC-4).

    For v1 -- since there is currently only ever the single production
    configuration -- "freezing research" means capturing one full run's research
    artifacts (the candidates.json + whatever research-phase context/brief-draft
    exists at that point) once, under a `frozen_id` any number of later writing-phase
    runs can reference via `EvalRecord.research_frozen_id`. This deliberately does
    NOT build a multi-config replay orchestrator with nothing yet to replay through
    (per this task's instructions) -- it only makes a frozen research output an
    identifiable, referenceable unit so that mechanism has something to point at once
    the follow-up cost-optimization epic introduces distinct writing-phase configs.
    """

    frozen_id: str
    session_id: str
    candidates_json: list[dict] | None
    research_markdown: str | None
    created_at: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FrozenResearchOutput":
        return cls(**data)


__all__ = [
    "SCHEMA_VERSION",
    "V1_CRITERIA",
    "HumanOverride",
    "CriterionScore",
    "CostBreakdownRecord",
    "EvalRecord",
    "CriterionAggregate",
    "CandidateAggregate",
    "aggregate_replicates",
    "FrozenResearchOutput",
]
