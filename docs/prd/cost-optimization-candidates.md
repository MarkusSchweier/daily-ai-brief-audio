# PRD: Cost-optimization candidate set for the daily AI brief pipeline

- Status: **Documented, deferred.** This epic's scope is captured now so the candidate list
  survives between sessions, but it is **not** being built next. Epic 2 (`agent-system-redesign.md`,
  not yet written) — decoupling content generation from AWS delivery and redesigning how agent
  systems are versioned/deployed — is being built first, since it changes the very mechanism this
  epic would use to build and evaluate candidates. Building this epic against the current
  architecture would mean rebuilding it again once epic 2 lands.
- Author: product-manager (Claude), drafted directly in conversation with the owner — Date: 2026-07-05
- Source: follow-on from the eval-harness epic (shipped, merged). That epic built the
  *measurement infrastructure*; this epic is *what gets measured* — the actual candidate pipeline
  configurations to compare once evaluation is possible again (post-redesign).

## 1. Problem

The daily brief pipeline costs ~$2.60–2.65/run in Claude Sonnet 5 usage (real transcript-mined
analysis, see the eval-harness epic's origin), dominated by cache-read tokens, with the
post-research writing/delivery phase costing *more* than research (~4.2M vs ~1.2M cache-read
tokens in the original analysis) because every subsequent turn in one long agentic session
re-sends the accumulated research context. The owner wants to explore whether a cheaper
configuration — a different model, a different session/task structure, or a different
architecture entirely — can hold quality roughly constant while cutting cost.

This PRD does not decide which candidate is best. It exists so the candidate list itself —
contributed by both the owner and Claude during a 2026-07-05 discussion — is a durable,
git-tracked artifact, not something reconstructed from a chat transcript later.

## 2. Candidate set

Each candidate is a distinct pipeline **configuration** to be run through the eval harness (once
epic 2's redesign makes that possible again) and compared against the others on the harness's v1
criteria (content selection, factual accuracy, length/format, dedup) and full cost breakdown.

### Owner-contributed candidates

1. **Baseline — keep as-is.** The current single-agent, Sonnet 5, one-long-session design.
   Always the reference point every other candidate is compared against.
2. **Same single-agent structure, Haiku 4.5 backing model.** Simplest possible lever: swap the
   model, change nothing else. Cheapest to try, highest risk to quality/judgment (research
   source selection, hallucination avoidance) since Haiku is a materially less capable model on
   exactly the tasks this pipeline leans on most.
3. **Multi-agent, split by phase, model-per-task.** Decompose into: orchestration, research +
   content selection, story writing, final Markdown review, HTML conversion, listening-script
   generation, and triggering delivery — each phase gets the model appropriate to its judgment
   requirements (e.g., Sonnet for research/selection/writing, a cheaper model for mechanical
   conversion steps). The most structurally different candidate; also the one most directly
   enabled by epic 2's multi-agent support.

### Claude-contributed candidates

4. **Same model throughout, but restructure the session instead of the model.** Break the one
   long agentic session into phases that don't each replay the full accumulated history (e.g.,
   research writes findings to a file; a fresh turn/session picks up a digest, not the whole
   transcript). Isolates "did splitting the session help" from "did a cheaper model help" — the
   root cause identified in the original cost analysis (repeated full-context replay) is
   structural, not model-dependent, so this candidate tests whether fixing the structure alone
   recovers most of the savings without touching quality-sensitive model choice at all.
5. **Hybrid model split (a more conservative version of #3).** Keep Sonnet for research,
   selection, and writing — where judgment and hallucination risk matter most — and use Haiku
   only for the narrow, mechanical, low-risk subtasks: HTML conversion, listening-script rewrite.
   A middle ground between "all Sonnet" (#1) and "fully decomposed, model-per-task" (#3).
6. **Pull mechanical subtasks out of the agentic session entirely**, into direct, stateless
   Messages API calls (no tool use, no accumulated context) rather than more turns in the same
   session — HTML conversion and listening-script generation don't need an agent, just one
   prompt in, one output out. This eliminates their share of the cache-replay problem rather
   than just cheapening it. Since the PRD for the original migration already established that
   latency doesn't matter for this unattended overnight batch job, these calls could also go
   through the Message Batches API for an additional discount, stacked with prompt caching.
7. **Effort / thinking-budget as its own axis**, crossed with any of the above — e.g. capping or
   disabling extended-thinking budget for the low-judgment subtasks, independent of which model
   runs them. Not a standalone candidate so much as a parameter every other candidate should be
   swept over once the harness can vary it.

### Related, complementary, but explicitly out of scope for this epic

- **Source-list trimming** (tracked separately as GitHub issue #28): pruning `sources.md` entries
  that are never featured in a real generated brief, to cut research-phase input volume. Orthogonal
  to which candidate above is chosen — applies underneath any of them.

## 3. Non-goals

- **No candidate is built, deployed, or evaluated in this epic.** This document exists to capture
  the candidate list, not to run it.
- **No decision about which candidate is "best."** That is the eval harness's job, once epic 2
  makes running these candidates possible again.
- **No re-litigation of the eval harness's v1 criteria set** (content selection, factual accuracy,
  length/format, dedup) — those are inherited as-is from the eval-harness epic.

## 4. Dependencies

- **Epic 2 (agent-system redesign)**, not yet written as a PRD: decouples content generation from
  AWS delivery and redesigns how candidate agent systems are versioned, deployed, and run against
  the eval harness. This epic's candidates are the first real thing epic 2's new mechanism needs
  to support — multi-agent (candidate #3), model-per-task (#3, #5), and session/context
  restructuring (#4) all need to be expressible in whatever versioned-candidate format epic 2
  introduces.
- **The eval harness** (`deploy/eval/`, shipped): will need re-integration once epic 2 changes how
  candidate configurations are triggered — explicitly flagged as a known follow-up, not addressed
  here (see the owner's own framing: "we'll likely re-design the deployment of the production
  agent on Claude platform, which will require a re-design of the evals later").

## 5. Rollout

Not applicable yet — this epic starts once epic 2 ships and the eval harness is re-integrated
against its new candidate-deployment mechanism. At that point: build each candidate above in
epic 2's format, run 3 replicates of each through the eval harness against the same frozen
research (where applicable), and compare on the harness's comparison/leaderboard view (FR-24).
