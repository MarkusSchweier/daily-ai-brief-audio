# daily-ai-brief skill (placeholder — content port is a separate developer task)

**Status: NOT YET PORTED.** This file is a placeholder marking where the research/
writing half of the pipeline (today's steps 1–4) will live once ported, per
`docs/adr/0007-porting-the-research-writing-half-into-the-managed-agent.md`.

This placeholder was created by the devops-aws infrastructure build
(`docs/adr/0006`) only so `deploy/managed-agent/deployment.json`'s
`agent.skills: ["daily-ai-brief"]` reference and the Dockerfile's image-skeleton
TODO have a concrete file to point at. **It contains no pipeline logic and must
not be treated as the ported skill.**

## What goes here (Developer task, per ADR-0007)

- A **faithful, verbatim copy** of the existing external `daily-ai-brief` skill's
  content (source-tier list, research method, dollar/benchmark validation rules,
  Markdown writing/validation logic, HTML + speech-optimized listening-script
  derivation) — no paraphrasing or "improvements" during the port (PRD non-goal).
- Any supporting files that skill needs (e.g. a source-tier list document),
  alongside this `SKILL.md`.
- The **one deliberate behavior change** ADR-0007 calls out: the ported skill's
  "read yesterday's brief" step must read from the S3 `briefs/` prefix
  (`docs/adr/0005-cross-run-persistence-store-for-brief-history.md`) — listing
  `briefs/` and taking the most recent dated object strictly before today — not
  from the local `Daily AI Briefs/` folder, which does not exist inside the
  microVM sandbox (no shared filesystem across runs).

## Validation (Developer task)

Per ADR-0007: the parallel-run diff. Compare a same-day brief produced by this
ported skill (archived under `briefs/YYYY-MM-DD/brief.md` in S3) against the
local Desktop task's output for the same day. Equivalence "in intent" — same
source tiers, same validation rules, same structure and listening-script
optimization — is the bar, not byte-for-byte identity (the research is
inherently non-deterministic day to day).

## Not this task

Porting this content is explicitly **out of scope** for the devops-aws
infrastructure build (`docs/adr/0006`) that created this placeholder — see that
build's handoff notes. This file exists so the CDK stack, the microVM image
skeleton, and `deployment.json` all have a concrete, versioned target to
reference; it is the Developer's job to fill it in per ADR-0007 before this
deployment can actually run a real brief end-to-end.
