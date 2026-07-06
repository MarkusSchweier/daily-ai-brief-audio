# 0008. Skill-content lockstep + live Skills-API version push for skill-content changes

- Status: Accepted (originally a **three-way** lockstep; **reconciled to TWO-WAY** by the
  agent-system-redesign epic — see the 2026-07-06 amendment below)
- Date: 2026-07-03
- Deciders: architect (Claude)
- **Amended 2026-07-06 (agent-system-redesign epic; reconciling with ADR-0014 Decision 1's ratified
  hybrid + the owner's dead-Desktop-fallback decision):** two independent reconciliations, per
  ADR-0014's "Reconciling ADR-0008" section:
  1. **The local Desktop fallback is DEAD — the lockstep collapses to TWO-WAY, unconditionally.** The
     owner has stated the local Desktop copy (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`) is
     dead: it will not run and will not be reactivated, in any form. So the original **three-way**
     lockstep (in-repo ↔ local Desktop ↔ live Skills-API) collapses to a **two-way lockstep: in-repo
     source-of-truth (`deploy/managed-agent/skills/daily-ai-brief/`) ↔ live Skills-API resource
     (`skill_01H2qu83NwnJ5zqcbrqsCcJ6`)**. The Desktop copy is **no longer a lockstep member — full
     stop, not "for now," not "unless reactivated"; there is no reactivation hedge.** Wherever the
     text below (Context item 2, Decision step 2, the "three artifacts" follow-up) still says
     "three-way" or "mirror to the local Desktop copy," it is **superseded by this amendment** —
     drop the Desktop member; only steps 1 and 3–6 remain, unchanged.
  2. **The 2026-07-04 image-rebuild amendment (below) REMAINS IN FORCE for production** under the
     ratified hybrid. Decision 1 keeps **production on `self_hosted`** with the current custom worker,
     which still bakes the skill into the microVM image — so a Skills-API push alone still does not
     reach the live scheduled brief without the image rebuild (README §5). The "no image rebuild
     needed" relief applies **only to the candidate/`cloud` path** (skills download dynamically per
     session there — FR-5), **not** to production. This amendment is **not** superseded for production.
- **Amended 2026-07-04 (eval-harness epic):** step 4's Skills-API version push, while worth doing
  for the record and for any future non-self-hosted environment type, is **NOT sufficient by
  itself** to make a skill-content change reach a running session on this self-hosted microVM
  deployment — `microvm/Dockerfile` bakes `skills/daily-ai-brief/` into the container **image** at
  build time, and the agent reads it from that baked-in path (`/opt/skills/daily-ai-brief/`) via a
  plain bash `cat`, not via any runtime Skills-API fetch. Confirmed live: a pushed version's
  `latest_version` updated and a new session's resolved `skills[].version` showed the new id, yet
  that session's own tool-call transcript showed it reading the **old** file content. The step 4
  push must be paired with `deploy/managed-agent/README.md` §5 (rebuild + push the microVM image)
  for the change to actually take effect — see that README section's own correction for the full
  incident and reasoning. Steps 1–3 and 6 below are unaffected.

## Context

The `daily-ai-brief` research/writing skill originally existed in **three** places that had to stay
consistent for **any** content edit (PRD `neutral-briefing-pipeline.md`, §7 "Whether an ADR is
needed", FR-7…FR-9, AC-6/AC-7). **As of the 2026-07-06 amendment above, item 2 (the local Desktop
copy) is DEAD and no longer a lockstep member — only items 1 and 3 remain (two-way):**

1. **In-repo source-of-truth** — `deploy/managed-agent/skills/daily-ai-brief/SKILL.md` and
   `sources.md` (committed per ADR-0007).
2. **~~Local Desktop fallback copy~~ — DEAD (2026-07-06 amendment).**
   `~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md` (outside this repo) is retired: the local
   scheduled task will not run and will not be reactivated. It is **no longer part of the lockstep**
   and must not be mirrored to. (Historically it was the monitored fallback through the Managed
   Agents migration's parallel-run window; that window is over and the fallback is dead.)
3. **Live Skills API resource** — `skill_01H2qu83NwnJ5zqcbrqsCcJ6`, referenced by agent
   `agent_01EswBTose8dnTAUDbGvzdLq` (with `skills[].version: "latest"`) and live scheduled
   deployment `depl_0132ARBCdsSRh6hxocbbW7ac`. The running Managed Agents deployment produces
   the current brief **from this resource**, so a repo/local edit has **no effect on live
   output** until a **new skill version is pushed** to it (POST a new version under the beta
   `managed-agents-2026-04-01` header). The API push is the mechanism used to create the skill
   in the first place (commit `606330f`), but that commit **left no runbook or script** — the
   procedure is currently undocumented and unrepeatable from git.

This is precisely the kind of decision an ADR exists to capture: a **cross-cutting, repeatable
procedure** whose *failure modes are silent and consequential*. A half-applied change (repo and
local updated but the live version not pushed, or vice-versa) leaves a stale/skewed brief in
play on one path with **no error** — exactly the failure the neutral-briefing PRD's §7 "lockstep
drift" and "live version push during beta" risks call out. The single skill-content edit itself
is trivial; the **procedure that keeps three artifacts consistent and confirms the live push**
is not, is reusable by every future skill edit, and is worth fixing once here rather than
re-deriving each time.

## Decision

**We will treat every `daily-ai-brief` skill-content change as a single atomic unit of work
that updates all three artifacts and ends with a *confirmed* live-version push, and we will
record the version-push mechanism (command + resulting version id) in the repo so it is
repeatable from git.** Concretely, the standing procedure is:

1. **Edit the in-repo source-of-truth first** — `deploy/managed-agent/skills/daily-ai-brief/`
   `SKILL.md` (+ `sources.md`). This is canonical; the other two derive from it.
2. **~~Mirror to the local Desktop copy~~ — REMOVED (2026-07-06 amendment).** The local Desktop
   fallback is dead and is no longer a lockstep member; there is nothing to mirror to. The lockstep
   is now **two-way** (in-repo ↔ live Skills-API), so a skill-content change goes straight from
   step 1 (edit the in-repo source-of-truth) to step 3 (validate) to step 4 (push the live version).
3. **Validate before the live push** — run whatever pre-schedule validation the change's PRD
   requires (for the neutral-briefing change: the structural prompt-text checks + one human
   before/after read, PRD AC-8). The live push happens **only after** validation passes, so the
   live schedule never runs an unvalidated skill.
4. **Push a new version of the live Skills API resource** from the updated in-repo copy —
   create a new version of `skill_01H2qu83NwnJ5zqcbrqsCcJ6` via the beta Skills API
   (`POST /v1/beta/skills/{skill_id}/versions`, header `anthropic-beta: managed-agents-2026-04-01`,
   using the SDK already vendored in the microVM image: `client.beta.skills.versions.create(...)`).
   The agent's `skills[].version: "latest"` resolves to the new version with **no `agent.json`
   change** (confirm this holds; only re-pin if Anthropic changes `latest` semantics).
5. **Confirm the push landed** — retrieve/list the skill's versions and verify the new version
   is present and is what `latest` resolves to (do not assume; the beta surface can drift and a
   failed push is silent). This satisfies AC-7's "inspect the live resource."
6. **Record the mechanism in the repo** — commit a short runbook/script under
   `deploy/managed-agent/` documenting the exact command used and the resulting new skill
   **version id**, so a future maintainer repeats it from git. **No Anthropic API key or other
   secret is committed or printed** (the credential is supplied at run time from the environment,
   per existing conventions).

The ADR fixes the **procedure and its guardrails** (atomicity, ordering, the confirm step, the
recorded runbook). It does **not** prescribe the content of any particular edit — that is the
Developer's job per each PRD.

## Alternatives considered

- **No ADR — leave it as a step in the neutral-briefing PRD's rollout plan.** The PRD already
  lists the five phases. Rejected because the sync-and-push procedure is **not specific to this
  one edit**: it applies to every future skill-content change, the version-push mechanism is
  currently **undocumented anywhere in the repo** (commit `606330f` created the skill via API but
  committed no runbook), and its failure mode is **silent** (a stale live version with no error).
  A one-off PRD rollout step is invisible to the next maintainer making an unrelated skill edit;
  an ADR is the durable, discoverable home for a repeatable cross-cutting procedure. The content
  *rubric* correctly stays out of scope (decided by the human, structural-only) — this ADR is
  only about the *mechanism*, which is a genuinely separate, reusable concern.
- **A fuller ADR that also re-decides the neutrality rubric or the content scope.** Rejected —
  both are already decided (human, 2026-07-03: structural-only rubric; content-only scope) and
  are out of this ADR's remit. This ADR is deliberately narrow: the three-way sync + push.
- **Automate the push in CDK / a CI pipeline instead of a documented manual procedure.**
  Rejected for now: the live skill has no CDK (it is a direct Skills-API resource, per the
  neutral-briefing PRD constraints), the API is in beta and may drift, edits are infrequent, and
  the change must be gated behind a **human** before/after read (AC-8) that does not fit an
  unattended pipeline. A recorded, repeatable manual runbook is the right weight; revisit
  automation if skill edits become frequent or the beta stabilizes.

## Consequences

Positive:
- The version-push mechanism becomes **documented and repeatable from git** for the first time,
  closing the gap left by commit `606330f` and satisfying FR-9/AC-7.
- A single atomic unit-of-work with an explicit **confirm** step makes the silent
  "stale live version" and "one path skewed" drift failures (PRD §7) detectable rather than
  assumed.
- The validation gate is placed **before** the live push in the standing procedure, so the real
  schedule never runs an unvalidated skill — reusable discipline for every future edit.

Negative / follow-ups:
- **~~Three~~ Two artifacts stay in manual lockstep** (in-repo ↔ live Skills-API), after the
  2026-07-06 amendment retired the local Desktop copy from the lockstep. This ADR formalizes the
  discipline but does not remove the (now-smaller) burden. The consolidation the original ADR
  gated on "retiring the local task" **has happened** — the Desktop fallback is dead (owner
  decision, agent-system-redesign epic), so the three-way burden is already reduced to two-way.
- The recorded runbook must be **kept current** if the beta Skills-API version-push surface
  changes; the runbook should note the beta header/version it was written against.
- This ADR governs **procedure**, not content: it does not by itself guarantee any given edit is
  correct or neutral — that remains each PRD's validation gate (for the neutral change,
  AC-1…AC-8).

## Verification note

Decision is about a documented operational procedure over the Anthropic Skills API, not AWS
service specifics; no `aws-docs` MCP lookup was required. The Developer must, when executing the
procedure, actually run step 5 (retrieve/list versions to confirm the push and that `latest`
resolves to the new version) rather than assuming success, and commit the step-6 runbook with no
secret.
