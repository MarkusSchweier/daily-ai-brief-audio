# 0007. Faithfully porting the research/writing half into the Managed Agent

- Status: Accepted
- Date: 2026-07-03
- Deciders: architect (Claude)

## Context

Today's pipeline has two halves. The **audio/email half** (steps 5–8) is already versioned in
this repo (`deploy/audio_email.py`, mirrored in the local `SKILL.md`). The **research/writing
half** (steps 1–4 — research across ~9 source tiers, write and validate the Markdown brief,
derive the HTML and the speech-optimized listening script) lives **entirely outside this repo**,
in a separate `daily-ai-brief` skill that the local Desktop scheduled task's `SKILL.md` invokes.

The migration must carry that research/writing logic into the Managed Agent's own
prompt/skill/system-prompt configuration **without drift** (PRD Goal, §7 "Reproducing the
research half faithfully", FR-3). The risk is that moving *where* the logic runs silently
changes *what* it does (source list, dollar/benchmark validation rules, listening-script
optimization, style). The PRD explicitly says the Architect designs the **mechanism** of
faithful carry-over; the **actual content port** and its validation (a parallel-run diff) are
the Developer's job (PRD §8). This ADR fixes the mechanism, not the content.

## Decision

**We will commit the research/writing logic into THIS repo for the first time as a versioned
skill file, and have the Managed Agent's `agent` definition reference that committed skill —
rather than embedding the logic inline in the deployment's initial prompt.**

Concretely:

- **New file `deploy/managed-agent/skills/daily-ai-brief/SKILL.md`** (plus any supporting files
  the skill needs, e.g. a source-tier list) — a **faithful copy of the existing `daily-ai-brief`
  skill's content**, brought into the repo so it becomes source-of-truth and reviewable in git.
  This closes the current gap where steps 1–4 live only outside the repo. (This ADR does **not**
  reproduce that content; the Developer ports it verbatim.)
- **The Managed Agent `agent` definition loads this skill** (as a skill/system-prompt input in
  the Deployments-API payload from ADR-0006), and the deployment's **initial prompt is thin** —
  it orchestrates ("run today's brief pipeline: research per the skill, write + validate, derive
  HTML + listening script, then run the audio/email step"), delegating the *substance* to the
  committed skill. Rationale: a thin orchestration prompt + a versioned skill file keeps the
  large, drift-prone research logic under normal code review and git history, instead of buried
  in a JSON prompt string where changes are invisible and unreviewable.
- **The audio/email half is carried by the SAME `deploy/audio_email.py` logic** (steps 5–8),
  invoked from within the agent run. The existing lockstep-copy convention (ADR-0006, PRD FR-17)
  is preserved: `deploy/audio_email.py` and the local `SKILL.md` inline copy stay identical while
  the local task remains the fallback.
- **Single source-of-truth going forward:** once ported, the repo's
  `deploy/managed-agent/skills/daily-ai-brief/SKILL.md` is the canonical research/writing logic
  for the Managed Agents runtime. The **external `daily-ai-brief` skill remains** as the local
  fallback's source during the parallel-run window (both must stay consistent while the fallback
  runs, same discipline as the STEP 6 lockstep); retiring the external copy is gated on the local
  task's retirement (a separate follow-up, PRD non-goal).

### How faithfulness is guaranteed (mechanism, not content)

- **Verbatim port + git review:** the Developer copies the external skill's content in, and the
  diff is reviewed — nothing is paraphrased or "improved" during the move (PRD non-goal: no
  content changes).
- **Parallel-run diff as the validation method** (PRD §8): during the ~1–2 week parallel run,
  the Managed Agents brief and the local task's brief for the same day are compared. Because
  ADR-0005 archives each day's Markdown under `briefs/YYYY-MM-DD/`, and the local task writes to
  its folder, a same-day text diff is straightforward. Equivalence "in intent" (not
  byte-identical — the research is inherently non-deterministic day to day) is the bar: same
  source tiers consulted, same validation rules applied, same listening-script optimization,
  same structure.
- **Cross-run persistence dependency:** the ported research step reads "yesterday's brief" from
  the S3 store (ADR-0005), not the local folder — the one deliberate behavior change, already
  designed to reproduce the local read semantics (including weekend/holiday/missed-run cases).

## Alternatives considered

- **Embed the research/writing logic inline in the deployment's initial prompt** (a big prompt
  string in `deployment.json`). Rejected: it buries large, important logic in an unreviewable
  JSON blob, invites drift, and makes diffs unreadable — the opposite of the source-of-truth
  goal (AC-15). A committed skill file under version control is reviewable and diffable.
- **Keep the research skill external and have the Managed Agent reference it by URL/reference
  out of the repo.** Rejected: it perpetuates the current gap (steps 1–4 not in the repo),
  leaves the Managed Agents runtime depending on a file outside version control, and gives no
  reviewable history. The migration is the right moment to bring it in-repo.
- **Rewrite/refactor the research logic during the port** (clean it up as we move it). Rejected:
  the PRD forbids content changes in this epic (non-goal); any change would confound the
  parallel-run equivalence check. Port verbatim first; improve later as a separate change.
- **Byte-for-byte output equivalence as the validation bar.** Rejected as impossible/wrong: the
  research is non-deterministic (news changes daily; the model's phrasing varies). Equivalence
  *in intent* — same sources, rules, structure, and script optimization — is the correct,
  achievable bar (PRD "byte-for-byte in intent").

## Consequences

Positive:
- The research/writing logic becomes **versioned in this repo for the first time**, reviewable
  and diffable, satisfying the source-of-truth requirement (FR-16, AC-15) and reducing long-term
  drift risk.
- A thin orchestration prompt + a committed skill keeps the deployment definition legible and
  the substance under code review.
- The parallel-run diff has a concrete, mechanical basis (S3-archived Markdown vs. local-folder
  Markdown for the same day), making the faithfulness check tractable.

Negative / follow-ups:
- **Two copies of the research logic exist during the parallel run** (the new in-repo skill and
  the external `daily-ai-brief` skill the local fallback uses); they must be kept consistent for
  the window, same lockstep discipline as STEP 6. Consolidation is gated on retiring the local
  task (separate follow-up, not this epic).
- The **verbatim port is the Developer's task and its fidelity is the key risk** this epic
  carries — the ADR only fixes the mechanism; the parallel-run diff is the safety net and must
  actually be run and inspected, not assumed.
- If the external skill references machine-local resources (paths, local tools) that don't exist
  in the microVM sandbox (ADR-0004/0006 — self-hosted, ephemeral, no shared filesystem across
  runs), those must be adapted to the AWS-backed equivalents (notably the "yesterday's brief"
  read → S3 per ADR-0005). The Developer must surface any such local assumptions during the port.

## Verification note

This decision is about mechanism and does not depend on AWS service specifics; no `aws-docs` MCP
lookup was required. The Developer must confirm, when porting, that the external skill contains
no hidden dependency on the local filesystem beyond the "yesterday's brief" read already handled
by ADR-0005, and must run the parallel-run diff (PRD §8) as the acceptance evidence for faithful
reproduction.

## Amendment (2026-07-03) — the real skill source was found and the port corrected

The Developer's first pass at the verbatim port (docs/adr/0007, Negative/follow-ups above) could
not locate the external `daily-ai-brief` skill's own file on disk and reconstructed it from the
production `SKILL.md`'s inline description instead — a reasonable fallback given what was
searchable at the time, but it only captured tier *names*, not the real skill's actual named
sources/URLs, paywall-handling procedure, ranking rubric, or quality guardrails.

The real skill was subsequently found (at
`~/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/.../skills/
daily-ai-brief/`, identical to `~/Claude Working Folder/Daily AI Briefs/
daily-ai-brief-SKILL-updated.md` — a location the first pass's search did not check). The port
has been corrected to a genuine verbatim copy, including the real `sources.md` (now committed
alongside `SKILL.md`) — closing the "key risk" this ADR flagged, ahead of the parallel-run diff
rather than relying on that diff to catch it.

**One structural correction this also surfaced:** the real skill explicitly states delivery
mechanics are "the caller's job... not this skill's" — it produces only the Markdown brief and
(optionally) a listening script, nothing about HTML conversion, Polly, or SES. The first port
had folded STEP 6 (audio/email) directly into the skill file, which both diverged from the real
skill's own design and muddied the mechanism this ADR specifies (a thin orchestration prompt +
substance in the skill). Corrected: `skills/daily-ai-brief/SKILL.md` now contains only the
research/write/validate/listening-script logic (verbatim), and `deployment.json`'s
`initial_prompt` carries the wrapping delivery steps (HTML derivation, the `audio_email.py`
invocation, env var wiring) — matching how the local Desktop task's own `SKILL.md` already
separates these (its STEP 1 invokes the skill; STEPs 5–8 are the wrapping task, external to the
skill). This is a truer match to ADR-0007's original mechanism, not a new decision.
